from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .stats import TradeStatsService

if TYPE_CHECKING:
    from .binance_client import BinanceClient
    from .config import Settings
    from .runtime_control import RuntimeControl
    from .storage import TradeJournalStore


def verify_dashboard_token(
    settings: Settings,
    *,
    query_token: str | None,
    header_token: str | None,
) -> None:
    if not settings.dashboard_token:
        raise HTTPException(status_code=403, detail="Dashboard Token 未配置")
    provided = header_token or query_token
    if not provided or provided != settings.dashboard_token:
        raise HTTPException(status_code=401, detail="Dashboard Token 无效")


def require_api_token_if_protected(
    settings: Settings,
    *,
    protect: bool,
    query_token: str | None,
    header_token: str | None,
) -> None:
    if not protect:
        return
    verify_dashboard_token(settings, query_token=query_token, header_token=header_token)


def _guard_runtime_control_dashboard_read(
    settings: Settings,
    *,
    query_token: str | None,
    header_token: str | None,
) -> None:
    """Dashboard runtime-control read: Dashboard Token only, not Runtime Control Token."""
    _check_dashboard_access(
        settings,
        query_token=query_token,
        header_token=header_token,
    )
    if not settings.runtime_status_allow_dashboard_token:
        raise HTTPException(
            status_code=403,
            detail="Dashboard 无权限读取运行控制状态",
        )


def _position_side_from_amt(amt_raw: Any) -> str:
    try:
        amt = Decimal(str(amt_raw))
    except Exception:
        return "FLAT"
    if amt > 0:
        return "LONG"
    if amt < 0:
        return "SHORT"
    return "FLAT"


def build_runtime_config(settings: Settings, app_version: str) -> dict[str, Any]:
    return {
        "app_version": app_version,
        "enable_trading": settings.enable_trading,
        "binance_base_url": settings.binance_base_url,
        "allowed_symbols": sorted(settings.allowed_symbol_set),
        "position_mode": settings.position_mode,
        "account_risk_enabled": settings.account_risk_enabled,
        "dashboard_enabled": settings.dashboard_enabled,
        "dashboard_require_token": settings.dashboard_require_token,
        "default_position_policy": settings.default_position_policy,
        "allow_market_entry": settings.allow_market_entry,
        "allow_limit_entry": settings.allow_limit_entry,
        "default_entry_type": settings.default_entry_type,
        "default_limit_fallback_to_market": settings.default_limit_fallback_to_market,
        "max_auto_leverage": settings.max_auto_leverage,
        "emergency_close_on_protection_fail": settings.emergency_close_on_protection_fail,
    }


def build_dashboard_positions(client: BinanceClient) -> list[dict[str, Any]]:
    rows = client.non_zero_positions()
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "symbol": row.get("symbol"),
                "side": _position_side_from_amt(row.get("positionAmt", "0")),
                "positionAmt": str(row.get("positionAmt", "0")),
                "entryPrice": str(row.get("entryPrice", "")),
                "markPrice": str(row.get("markPrice", "")),
                "unRealizedProfit": str(row.get("unRealizedProfit", row.get("unrealizedProfit", ""))),
                "notional": str(row.get("notional", "")),
                "initialMargin": str(row.get("initialMargin", "")),
                "liquidationPrice": str(row.get("liquidationPrice", "")),
            }
        )
    return result


def build_dashboard_algo_orders(settings: Settings, client: BinanceClient) -> list[dict[str, Any]]:
    symbols = sorted(settings.allowed_symbol_set)
    if not symbols:
        return []
    result: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            rows = client.open_algo_orders(symbol)
        except Exception:
            continue
        rows = rows if isinstance(rows, list) else [rows]
        for row in rows:
            if not isinstance(row, dict):
                continue
            result.append(
                {
                    "symbol": row.get("symbol") or symbol,
                    "orderType": row.get("orderType") or row.get("type"),
                    "side": row.get("side"),
                    "triggerPrice": str(row.get("triggerPrice") or row.get("stopPrice") or ""),
                    "quantity": str(row.get("quantity") or row.get("origQty") or ""),
                    "reduceOnly": row.get("reduceOnly"),
                    "closePosition": row.get("closePosition"),
                    "algoStatus": row.get("algoStatus") or row.get("status"),
                    "workingType": row.get("workingType"),
                    "createTime": row.get("createTime") or row.get("bookTime"),
                }
            )
    return result


def build_dashboard_health(settings: Settings, client: BinanceClient) -> dict[str, Any]:
    from .zh import to_jsonable

    payload: dict[str, Any] = {
        "ok": True,
        "enable_trading": settings.enable_trading,
        "binance_base_url": settings.binance_base_url,
        "allowed_symbols": sorted(settings.allowed_symbol_set),
    }
    try:
        payload["account"] = to_jsonable(client.futures_balance())
    except Exception as exc:
        payload["account_error"] = str(exc)[:500]
    return payload


def _health_level_rank(level: str) -> int:
    return {"OK": 0, "WARN": 1, "ERROR": 2}.get(level, 0)


def _aggregate_health_level(checks: list[dict[str, str]]) -> str:
    max_rank = 0
    result = "OK"
    for check in checks:
        level = check.get("level", "OK")
        rank = _health_level_rank(level)
        if rank > max_rank:
            max_rank = rank
            result = level
    return result


def _is_stop_loss_algo_order(row: dict[str, Any]) -> bool:
    order_type = str(row.get("orderType") or row.get("type") or "").upper()
    if "TAKE_PROFIT" in order_type:
        return False
    if order_type in {"STOP_MARKET", "STOP", "TRAILING_STOP_MARKET"}:
        return True
    if "STOP" in order_type and "TAKE_PROFIT" not in order_type:
        return True
    close_pos = row.get("closePosition")
    if str(close_pos).lower() in {"true", "1"} and "TAKE_PROFIT" not in order_type:
        return True
    return False


def build_health_overview(
    settings: Settings,
    client: BinanceClient,
    journal_store: TradeJournalStore,
    trade_stats: TradeStatsService,
    runtime_control: RuntimeControl,
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    summary: dict[str, Any] = {
        "enable_trading": settings.enable_trading,
        "binance_ok": False,
        "runtime_locked": False,
        "open_position_count": 0,
        "algo_order_count": 0,
        "recent_execution_count": 0,
        "last_execution_time": None,
        "last_execution_status": None,
        "last_rejection_reason": None,
    }

    checks.append(
        {
            "name": "service_health",
            "level": "OK",
            "message": "Dashboard 健康检查服务正常",
        }
    )

    account_error: str | None = None
    try:
        client.futures_balance()
        summary["binance_ok"] = True
        checks.append(
            {
                "name": "binance_account",
                "level": "OK",
                "message": "Binance account 查询正常",
            }
        )
    except Exception as exc:
        account_error = str(exc)[:500]
        summary["binance_ok"] = False
        checks.append(
            {
                "name": "binance_account",
                "level": "ERROR",
                "message": f"Binance account 查询失败: {account_error}",
            }
        )

    if settings.enable_trading:
        checks.append(
            {
                "name": "enable_trading",
                "level": "WARN",
                "message": "真实交易已启用，请确认风控与 Runtime Control 配置",
            }
        )
    else:
        checks.append(
            {
                "name": "enable_trading",
                "level": "WARN",
                "message": "交易执行已关闭，仅计划/监控",
            }
        )

    runtime_state = runtime_control.status_payload()
    runtime_enabled = bool(runtime_state.get("enabled"))
    runtime_locked = bool(runtime_state.get("locked"))
    summary["runtime_locked"] = runtime_locked
    if not runtime_enabled:
        checks.append(
            {
                "name": "runtime_lock",
                "level": "WARN",
                "message": "Runtime Control 未启用",
            }
        )
    elif runtime_locked:
        reason = runtime_state.get("reason") or "未说明"
        locked_by = runtime_state.get("locked_by") or "-"
        locked_until = runtime_state.get("locked_until") or "无"
        checks.append(
            {
                "name": "runtime_lock",
                "level": "WARN",
                "message": (
                    f"Runtime Control 当前处于锁定状态: reason={reason}, "
                    f"locked_by={locked_by}, locked_until={locked_until}"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "runtime_lock",
                "level": "OK",
                "message": "Runtime Control 未锁定",
            }
        )

    recent_rows = journal_store.list_executions(limit=50)
    summary["recent_execution_count"] = len(recent_rows)
    last_row = recent_rows[0] if recent_rows else None
    if last_row:
        summary["last_execution_time"] = last_row.get("created_at")
        summary["last_execution_status"] = last_row.get("status")
        summary["last_rejection_reason"] = last_row.get("skip_reason")
        last_status = str(last_row.get("status") or "")
        if last_status in {"failed", "protection_failed"}:
            checks.append(
                {
                    "name": "recent_execution",
                    "level": "ERROR",
                    "message": f"最近执行状态异常: {last_status}",
                }
            )
        elif last_status in {"blocked_by_account_risk", "blocked_by_runtime_lock"}:
            skip = last_row.get("skip_reason") or last_status
            checks.append(
                {
                    "name": "recent_execution",
                    "level": "WARN",
                    "message": f"最近执行被拒绝: {last_status} ({skip})",
                }
            )
        elif last_status == "protected":
            checks.append(
                {
                    "name": "recent_execution",
                    "level": "OK",
                    "message": "最近执行已成功保护",
                }
            )
        else:
            checks.append(
                {
                    "name": "recent_execution",
                    "level": "WARN",
                    "message": f"最近执行状态: {last_status}",
                }
            )
    else:
        checks.append(
            {
                "name": "recent_execution",
                "level": "WARN",
                "message": "暂无执行记录",
            }
        )

    positions: list[dict[str, Any]] = []
    try:
        positions = build_dashboard_positions(client)
    except Exception as exc:
        checks.append(
            {
                "name": "open_positions",
                "level": "WARN",
                "message": f"持仓查询失败: {str(exc)[:200]}",
            }
        )
    else:
        summary["open_position_count"] = len(positions)
        if not positions:
            checks.append(
                {
                    "name": "open_positions",
                    "level": "OK",
                    "message": "当前无持仓",
                }
            )
        else:
            symbols = ", ".join(p.get("symbol", "?") for p in positions)
            checks.append(
                {
                    "name": "open_positions",
                    "level": "WARN",
                    "message": f"当前有 {len(positions)} 个持仓: {symbols}",
                }
            )

    algo_orders: list[dict[str, Any]] = []
    try:
        algo_orders = build_dashboard_algo_orders(settings, client)
    except Exception as exc:
        checks.append(
            {
                "name": "protection_orders",
                "level": "WARN",
                "message": f"条件单查询失败: {str(exc)[:200]}",
            }
        )
    else:
        summary["algo_order_count"] = len(algo_orders)
        if not positions:
            checks.append(
                {
                    "name": "protection_orders",
                    "level": "OK",
                    "message": "当前无持仓，无需保护单检查",
                }
            )
        else:
            algo_by_symbol: dict[str, list[dict[str, Any]]] = {}
            for order in algo_orders:
                sym = str(order.get("symbol") or "").upper()
                algo_by_symbol.setdefault(sym, []).append(order)
            unprotected: list[str] = []
            for pos in positions:
                sym = str(pos.get("symbol") or "").upper()
                sl_orders = [o for o in algo_by_symbol.get(sym, []) if _is_stop_loss_algo_order(o)]
                if not sl_orders:
                    unprotected.append(sym)
            if unprotected:
                checks.append(
                    {
                        "name": "protection_orders",
                        "level": "ERROR",
                        "message": f"存在未保护持仓（无止损条件单）: {', '.join(unprotected)}",
                    }
                )
            else:
                checks.append(
                    {
                        "name": "protection_orders",
                        "level": "OK",
                        "message": "所有持仓均检测到止损类条件单",
                    }
                )

    api_issues: list[str] = []
    if not settings.dashboard_require_token:
        api_issues.append("DASHBOARD_REQUIRE_TOKEN=false")
    if not settings.protect_journal_api:
        api_issues.append("PROTECT_JOURNAL_API=false")
    if not settings.protect_stats_api:
        api_issues.append("PROTECT_STATS_API=false")
    if not api_issues:
        checks.append(
            {
                "name": "api_protection",
                "level": "OK",
                "message": "Dashboard / Journal / Stats API Token 保护均已启用",
            }
        )
    else:
        checks.append(
            {
                "name": "api_protection",
                "level": "WARN",
                "message": f"部分 API 未启用 Token 保护: {', '.join(api_issues)}",
            }
        )

    if settings.runtime_status_allow_dashboard_token:
        checks.append(
            {
                "name": "runtime_status_permission",
                "level": "OK",
                "message": "Dashboard 可读取 Runtime Control 状态",
            }
        )
    else:
        checks.append(
            {
                "name": "runtime_status_permission",
                "level": "WARN",
                "message": "RUNTIME_STATUS_ALLOW_DASHBOARD_TOKEN=false，Dashboard 运行控制状态可能不可读",
            }
        )

    return {
        "level": _aggregate_health_level(checks),
        "checks": checks,
        "summary": summary,
    }


_HEALTH_ALERT_TITLES: dict[str, str] = {
    "service_health": "服务健康检查",
    "binance_account": "Binance 账户异常",
    "enable_trading": "交易开关状态",
    "runtime_lock": "Runtime Control 状态",
    "recent_execution": "最近执行状态",
    "open_positions": "当前持仓",
    "protection_orders": "持仓保护检查",
    "api_protection": "API Token 保护",
    "runtime_status_permission": "Runtime 状态读取权限",
}

_JOURNAL_ALERT_RULES: dict[str, tuple[str, str]] = {
    "failed": ("ERROR", "执行异常"),
    "protection_failed": ("ERROR", "保护单失败"),
    "blocked_by_account_risk": ("WARN", "账户风控拒绝"),
    "blocked_by_runtime_lock": ("WARN", "信号被 Runtime Lock 拦截"),
    "entry_not_filled": ("WARN", "信号未成交"),
    "skipped_by_position_policy": ("WARN", "持仓策略跳过"),
}

_RUNTIME_EVENT_RULES: dict[str, tuple[str, str, str]] = {
    "lock": ("WARN", "runtime_lock", "Runtime Control 被锁定"),
    "auto_expire": ("INFO", "runtime_auto_expire", "Runtime Control 自动解锁"),
    "unlock": ("INFO", "runtime_unlock", "Runtime Control 已解锁"),
}


def _alert_from_health_check(check: dict[str, str], *, created_at: str | None) -> dict[str, Any] | None:
    level = check.get("level", "OK")
    if level not in {"WARN", "ERROR"}:
        return None
    name = str(check.get("name") or "health")
    alert_type = name
    title = _HEALTH_ALERT_TITLES.get(name, name)
    if name == "protection_orders" and level == "ERROR":
        alert_type = "unprotected_position"
        title = "存在未保护持仓"
    return {
        "id": f"health-{name}",
        "source": "health",
        "level": level,
        "type": alert_type,
        "title": title,
        "message": str(check.get("message") or ""),
        "symbol": None,
        "status": None,
        "reason": None,
        "created_at": created_at,
    }


def _alert_from_journal_row(row: dict[str, Any]) -> dict[str, Any] | None:
    status = str(row.get("status") or "")
    rule = _JOURNAL_ALERT_RULES.get(status)
    if rule is None:
        return None
    level, title = rule
    symbol = row.get("symbol")
    signal_id = row.get("signal_id")
    skip_reason = row.get("skip_reason")
    exec_id = row.get("id")
    message_parts = [str(symbol or "")]
    if signal_id:
        message_parts.append(f"signal_id={signal_id}")
    if skip_reason:
        message_parts.append(f"reason={skip_reason}")
    return {
        "id": f"execution-{exec_id}",
        "source": "journal",
        "level": level,
        "type": skip_reason or status,
        "title": title,
        "message": " ".join(p for p in message_parts if p).strip() or title,
        "symbol": symbol,
        "status": status,
        "reason": skip_reason,
        "created_at": row.get("created_at"),
    }


def _alert_from_runtime_event(event: dict[str, Any]) -> dict[str, Any] | None:
    action = str(event.get("action") or "")
    rule = _RUNTIME_EVENT_RULES.get(action)
    if rule is None:
        return None
    level, alert_type, title = rule
    reason = event.get("reason")
    actor = event.get("actor")
    message_parts: list[str] = []
    if reason:
        message_parts.append(f"reason={reason}")
    if actor:
        message_parts.append(f"actor={actor}")
    if event.get("locked_until"):
        message_parts.append(f"locked_until={event.get('locked_until')}")
    return {
        "id": f"runtime-{event.get('id')}",
        "source": "runtime",
        "level": level,
        "type": alert_type,
        "title": title,
        "message": ", ".join(message_parts) if message_parts else title,
        "symbol": None,
        "status": None,
        "reason": reason,
        "created_at": event.get("created_at"),
    }


def _alert_summary(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    error_count = sum(1 for a in alerts if a.get("level") == "ERROR")
    warn_count = sum(1 for a in alerts if a.get("level") == "WARN")
    info_count = sum(1 for a in alerts if a.get("level") == "INFO")
    if error_count:
        latest_level = "ERROR"
    elif warn_count:
        latest_level = "WARN"
    elif info_count:
        latest_level = "INFO"
    else:
        latest_level = "OK"
    return {
        "total": len(alerts),
        "error_count": error_count,
        "warn_count": warn_count,
        "info_count": info_count,
        "latest_level": latest_level,
    }


def build_alerts(
    settings: Settings,
    client: BinanceClient,
    journal_store: TradeJournalStore,
    trade_stats: TradeStatsService,
    runtime_control: RuntimeControl,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    cap = max(1, min(limit, 100))
    now_iso = datetime.now(timezone.utc).isoformat()
    alerts: list[dict[str, Any]] = []

    overview = build_health_overview(settings, client, journal_store, trade_stats, runtime_control)
    for check in overview.get("checks") or []:
        alert = _alert_from_health_check(check, created_at=now_iso)
        if alert:
            alerts.append(alert)

    journal_scan = max(cap * 3, 50)
    for row in journal_store.list_executions(limit=journal_scan):
        alert = _alert_from_journal_row(row)
        if alert:
            alerts.append(alert)

    runtime_events = runtime_control.list_events(limit=cap)
    for event in runtime_events:
        alert = _alert_from_runtime_event(event)
        if alert:
            alerts.append(alert)

    runtime_state = runtime_control.status_payload()
    if runtime_state.get("enabled") and runtime_state.get("locked"):
        reason = runtime_state.get("reason") or "未说明"
        locked_by = runtime_state.get("locked_by") or "-"
        locked_until = runtime_state.get("locked_until") or "无"
        alerts.append(
            {
                "id": "runtime-lock-current",
                "source": "runtime",
                "level": "WARN",
                "type": "runtime_locked",
                "title": "Runtime Control 当前处于锁定状态",
                "message": (
                    f"reason={reason}, locked_by={locked_by}, locked_until={locked_until}"
                ),
                "symbol": None,
                "status": None,
                "reason": reason,
                "created_at": runtime_state.get("locked_at") or runtime_state.get("updated_at"),
            }
        )

    alerts.sort(
        key=lambda item: (str(item.get("created_at") or ""), _health_level_rank(str(item.get("level", "OK")))),
        reverse=True,
    )
    alerts = alerts[:cap]
    summary = _alert_summary(alerts)
    return {"告警": alerts, "summary": summary, "数量": len(alerts)}


def _check_dashboard_access(
    settings: Settings,
    *,
    query_token: str | None,
    header_token: str | None,
) -> None:
    if not settings.dashboard_enabled:
        raise HTTPException(status_code=404, detail="Dashboard 未启用")
    if not settings.dashboard_require_token:
        return
    verify_dashboard_token(settings, query_token=query_token, header_token=header_token)


def _error_html(title: str, message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    body {{
      margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
      font-family: system-ui, -apple-system, Segoe UI, sans-serif;
      background: #0f1419; color: #e7ecf3;
    }}
    .box {{
      max-width: 420px; padding: 2rem; border: 1px solid #2a3441; border-radius: 12px;
      background: #151b23; text-align: center;
    }}
    h1 {{ margin: 0 0 0.75rem; font-size: 1.25rem; }}
    p {{ margin: 0; color: #9aa7b8; line-height: 1.5; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>{title}</h1>
    <p>{message}</p>
  </div>
</body>
</html>"""


def render_dashboard_html(auto_refresh_sec: int) -> str:
    refresh = max(0, int(auto_refresh_sec))
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Trade Journal Dashboard</title>
  <style>
    :root {{
      --bg: #0f1419;
      --panel: #151b23;
      --border: #2a3441;
      --text: #e7ecf3;
      --muted: #9aa7b8;
      --accent: #3b82f6;
      --ok: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.45;
    }}
    header {{
      padding: 1rem 1.25rem; border-bottom: 1px solid var(--border);
      display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; justify-content: space-between;
      background: var(--panel); position: sticky; top: 0; z-index: 10;
    }}
    header h1 {{ margin: 0; font-size: 1.1rem; font-weight: 600; }}
    .badge {{ font-size: 0.75rem; color: var(--muted); border: 1px solid var(--border); padding: 0.15rem 0.5rem; border-radius: 999px; }}
    main {{ padding: 1rem 1.25rem 2rem; max-width: 1400px; margin: 0 auto; }}
    .cards {{
      display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 0.75rem; margin-bottom: 1.25rem;
    }}
    .card {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 0.85rem 1rem;
    }}
    .card .label {{ font-size: 0.75rem; color: var(--muted); margin-bottom: 0.25rem; }}
    .card .value {{ font-size: 1.35rem; font-weight: 700; }}
    section {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
      padding: 1rem; margin-bottom: 1rem;
    }}
    section h2 {{ margin: 0 0 0.75rem; font-size: 1rem; }}
    .toolbar {{
      display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: end; margin-bottom: 0.75rem;
    }}
    label {{ display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.75rem; color: var(--muted); }}
    input, select, button {{
      background: #0b1016; color: var(--text); border: 1px solid var(--border);
      border-radius: 8px; padding: 0.45rem 0.6rem; font: inherit;
    }}
    button {{
      cursor: pointer; background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600;
    }}
    button.secondary {{ background: transparent; color: var(--text); border-color: var(--border); }}
    button:disabled {{ opacity: 0.55; cursor: not-allowed; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
    th, td {{ border-bottom: 1px solid var(--border); padding: 0.45rem 0.35rem; text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; white-space: nowrap; }}
    .status {{ display: inline-block; padding: 0.1rem 0.45rem; border-radius: 999px; font-size: 0.72rem; border: 1px solid var(--border); }}
    .status.protected {{ color: var(--ok); border-color: #14532d; }}
    .status.failed, .status.protection_failed {{ color: var(--bad); border-color: #7f1d1d; }}
    .status.blocked_by_account_risk, .status.skipped_by_position_policy {{ color: var(--warn); border-color: #78350f; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.78rem; word-break: break-all; }}
    .error-banner {{
      background: #2a1215; border: 1px solid #7f1d1d; color: #fecaca; padding: 0.65rem 0.85rem;
      border-radius: 8px; margin-bottom: 1rem; display: none;
    }}
    .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }}
    @media (max-width: 900px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
    .detail-backdrop {{
      position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: none; align-items: center; justify-content: center;
      padding: 1rem; z-index: 20;
    }}
    .detail-panel {{
      width: min(960px, 100%); max-height: 90vh; overflow: auto; background: var(--panel);
      border: 1px solid var(--border); border-radius: 12px; padding: 1rem;
    }}
    .detail-panel h3 {{ margin: 0 0 0.75rem; }}
    pre {{
      margin: 0 0 0.75rem; padding: 0.75rem; background: #0b1016; border: 1px solid var(--border);
      border-radius: 8px; overflow: auto; font-size: 0.75rem; white-space: pre-wrap; word-break: break-word;
    }}
    .detail-meta {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 0.5rem; margin-bottom: 0.75rem; }}
    .detail-meta div {{ font-size: 0.82rem; }}
    .detail-meta span {{ color: var(--muted); display: block; font-size: 0.72rem; }}
    .empty {{ color: var(--muted); font-size: 0.85rem; padding: 0.5rem 0; }}
    .section-note {{ margin: -0.35rem 0 0.75rem; font-size: 0.75rem; color: var(--muted); }}
    .subsection-title {{ margin: 1rem 0 0.5rem; font-size: 0.9rem; font-weight: 600; }}
    .lock-badge {{ display: inline-block; padding: 0.1rem 0.45rem; border-radius: 999px; font-size: 0.72rem; }}
    .lock-badge.locked {{ color: var(--warn); border: 1px solid #78350f; }}
    .lock-badge.unlocked {{ color: var(--ok); border: 1px solid #14532d; }}
    .health-level {{ font-size: 1.4rem; font-weight: 700; margin-bottom: 0.75rem; }}
    .health-level.ok {{ color: var(--ok); }}
    .health-level.warn {{ color: var(--warn); }}
    .health-level.error {{ color: var(--bad); }}
    .check-level {{ display: inline-block; min-width: 3.2rem; font-size: 0.72rem; font-weight: 600; }}
    .check-level.ok {{ color: var(--ok); }}
    .check-level.warn {{ color: var(--warn); }}
    .check-level.error {{ color: var(--bad); }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Trade Journal Dashboard</h1>
      <div class="badge">只读 · 无下单能力</div>
    </div>
    <div class="badge" id="refreshHint">自动刷新: {refresh}s</div>
  </header>
  <main>
    <div id="errorBanner" class="error-banner"></div>

    <div class="cards" id="summaryCards">
      <div class="card"><div class="label">总执行数</div><div class="value" data-k="total_executions">-</div></div>
      <div class="card"><div class="label">保护成功</div><div class="value" data-k="protected_count">-</div></div>
      <div class="card"><div class="label">账户风控拒绝</div><div class="value" data-k="blocked_by_account_risk_count">-</div></div>
      <div class="card"><div class="label">持仓策略跳过</div><div class="value" data-k="skipped_by_position_policy_count">-</div></div>
      <div class="card"><div class="label">保护单失败</div><div class="value" data-k="protection_failed_count">-</div></div>
      <div class="card"><div class="label">执行异常</div><div class="value" data-k="failed_count">-</div></div>
      <div class="card"><div class="label">成功率</div><div class="value" data-k="success_rate">-</div></div>
      <div class="card"><div class="label">今日执行</div><div class="value" data-k="today_executions">-</div></div>
      <div class="card"><div class="label">今日保护成功</div><div class="value" data-k="today_protected">-</div></div>
    </div>

    <section>
      <h2>系统健康摘要</h2>
      <p class="section-note">只读监控 · 不会自动下单、平仓、撤单或解锁</p>
      <div id="healthOverviewWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section>
      <h2>告警中心</h2>
      <p class="section-note">只读聚合 · 不会自动交易、撤单、平仓或解锁</p>
      <div id="alertCenterWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section>
      <h2>运行控制</h2>
      <p class="section-note">只读展示 · 不支持锁定/解锁操作</p>
      <div id="runtimeControlStatus" class="detail-meta">
        <div class="empty">加载中...</div>
      </div>
      <h3 class="subsection-title">最近运行控制事件</h3>
      <div style="overflow-x:auto" id="runtimeControlEventsWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section>
      <h2>运行状态</h2>
      <div class="detail-meta" id="runtimeMeta"></div>
      <div class="detail-meta" id="healthMeta" style="margin-top:0.5rem"></div>
    </section>

    <section>
      <h2>当前持仓</h2>
      <div style="overflow-x:auto" id="positionsWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section>
      <h2>当前条件单</h2>
      <div style="overflow-x:auto" id="algoOrdersWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section>
      <h2>最近执行记录</h2>
      <div class="toolbar">
        <label>交易对<input id="filterSymbol" placeholder="BTCUSDT" /></label>
        <label>状态
          <select id="filterStatus">
            <option value="">全部</option>
            <option value="protected">protected</option>
            <option value="entry_not_filled">entry_not_filled</option>
            <option value="blocked_by_account_risk">blocked_by_account_risk</option>
            <option value="skipped_by_position_policy">skipped_by_position_policy</option>
            <option value="protection_failed">protection_failed</option>
            <option value="failed">failed</option>
          </select>
        </label>
        <label>条数<input id="filterLimit" type="number" min="1" max="500" value="50" /></label>
        <button id="refreshBtn" type="button">刷新</button>
      </div>
      <div style="overflow-x:auto">
        <table>
          <thead>
            <tr>
              <th>编号</th><th>创建时间</th><th>交易对</th><th>方向</th><th>状态</th><th>状态说明</th>
              <th>跳过原因</th><th>计划数量</th><th>成交数量</th><th>进场价</th><th>杠杆</th><th>信号编号</th><th></th>
            </tr>
          </thead>
          <tbody id="executionsBody"><tr><td colspan="13" class="empty">加载中...</td></tr></tbody>
        </table>
      </div>
    </section>

    <div class="grid-2">
      <section>
        <h2>按交易对统计</h2>
        <div style="overflow-x:auto">
          <table>
            <thead>
              <tr><th>交易对</th><th>总数</th><th>保护成功</th><th>未成交</th><th>风控拒绝</th><th>保护失败</th></tr>
            </thead>
            <tbody id="bySymbolBody"><tr><td colspan="6" class="empty">加载中...</td></tr></tbody>
          </table>
        </div>
      </section>
      <section>
        <h2>拒绝原因统计</h2>
        <div style="overflow-x:auto">
          <table>
            <thead><tr><th>原因</th><th>状态</th><th>次数</th></tr></thead>
            <tbody id="rejectionsBody"><tr><td colspan="3" class="empty">加载中...</td></tr></tbody>
          </table>
        </div>
      </section>
    </div>
  </main>

  <div id="detailBackdrop" class="detail-backdrop">
    <div class="detail-panel">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:0.5rem;">
        <h3 id="detailTitle">执行详情</h3>
        <button class="secondary" id="closeDetailBtn" type="button">关闭</button>
      </div>
      <div class="detail-meta" id="detailMeta"></div>
      <h4 style="margin:0.75rem 0 0.35rem;font-size:0.85rem;color:var(--muted)">原始信号</h4>
      <pre id="detailRawSignal"></pre>
      <h4 style="margin:0.75rem 0 0.35rem;font-size:0.85rem;color:var(--muted)">交易计划</h4>
      <pre id="detailPlan"></pre>
      <h4 style="margin:0.75rem 0 0.35rem;font-size:0.85rem;color:var(--muted)">账户风控</h4>
      <pre id="detailAccountRisk"></pre>
      <h4 style="margin:0.75rem 0 0.35rem;font-size:0.85rem;color:var(--muted)">进场摘要</h4>
      <pre id="detailEntrySummary"></pre>
      <h4 style="margin:0.75rem 0 0.35rem;font-size:0.85rem;color:var(--muted)">保护单摘要</h4>
      <pre id="detailProtectionSummary"></pre>
      <h4 style="margin:0.75rem 0 0.35rem;font-size:0.85rem;color:var(--muted)">执行结果</h4>
      <pre id="detailResult"></pre>
      <h4 style="margin:0.75rem 0 0.35rem;font-size:0.85rem;color:var(--muted)">关联订单</h4>
      <pre id="detailOrders"></pre>
    </div>
  </div>

  <script>
    const AUTO_REFRESH_SEC = {refresh};
    let dashboardToken = null;
    let refreshTimer = null;

    function readTokenFromUrl() {{
      const params = new URLSearchParams(window.location.search);
      return params.get("token");
    }}

    function showError(message) {{
      const el = document.getElementById("errorBanner");
      el.textContent = message;
      el.style.display = "block";
    }}

    function clearError() {{
      const el = document.getElementById("errorBanner");
      el.textContent = "";
      el.style.display = "none";
    }}

    async function apiFetch(path) {{
      const headers = {{ Accept: "application/json" }};
      if (dashboardToken) headers["X-Dashboard-Token"] = dashboardToken;
      const resp = await fetch(path, {{ headers }});
      let data = null;
      try {{ data = await resp.json(); }} catch (_) {{ data = null; }}
      if (!resp.ok) {{
        const detail = (data && (data.detail || data.错误)) || resp.statusText || "请求失败";
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }}
      return data;
    }}

    function esc(text) {{
      if (text === null || text === undefined) return "";
      return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}

    function fmtJson(value) {{
      if (value === null || value === undefined) return "null";
      if (typeof value === "string") {{
        try {{ return JSON.stringify(JSON.parse(value), null, 2); }} catch (_) {{ return value; }}
      }}
      return JSON.stringify(value, null, 2);
    }}

    function renderSummary(stats) {{
      document.querySelectorAll("#summaryCards .value").forEach((el) => {{
        const key = el.getAttribute("data-k");
        let val = stats[key];
        if (key === "success_rate" && val !== undefined) val = (Number(val) * 100).toFixed(2) + "%";
        el.textContent = val ?? "-";
      }});
    }}

    const RUNTIME_LABELS = {{
      app_version: "应用版本",
      enable_trading: "允许真实下单",
      binance_base_url: "币安接口",
      allowed_symbols: "允许交易对",
      position_mode: "持仓模式",
      account_risk_enabled: "账户风控",
      dashboard_enabled: "Dashboard 启用",
      dashboard_require_token: "Dashboard 需 Token",
      default_position_policy: "默认持仓策略",
      allow_market_entry: "允许市价进场",
      allow_limit_entry: "允许限价进场",
      default_entry_type: "默认进场方式",
      default_limit_fallback_to_market: "限价超时改市价",
      max_auto_leverage: "最大自动杠杆",
      emergency_close_on_protection_fail: "保护失败紧急平仓",
    }};

    const HEALTH_LABELS = {{
      ok: "服务正常",
      enable_trading: "允许真实下单",
      binance_base_url: "币安接口",
      allowed_symbols: "允许交易对",
      account_error: "账户查询错误",
    }};

    function renderKeyValueGrid(containerId, data, labels) {{
      const el = document.getElementById(containerId);
      if (!data) {{
        el.innerHTML = '<div class="empty">暂无数据</div>';
        return;
      }}
      el.innerHTML = Object.entries(data).map(([key, val]) => {{
        if (key === "account") return "";
        const label = labels[key] || key;
        let display = val;
        if (Array.isArray(val)) display = val.join(", ");
        if (typeof val === "boolean") display = val ? "是" : "否";
        if (val === null || val === undefined) display = "-";
        return `<div><span>${{esc(label)}}</span>${{esc(display)}}</div>`;
      }}).join("");
    }}

    function renderPositions(rows) {{
      const wrap = document.getElementById("positionsWrap");
      if (!rows || rows.length === 0) {{
        wrap.innerHTML = '<div class="empty">当前无持仓</div>';
        return;
      }}
      wrap.innerHTML = `<table>
        <thead><tr>
          <th>交易对</th><th>方向</th><th>数量</th><th>开仓价</th><th>标记价</th>
          <th>未实现盈亏</th><th>名义价值</th><th>初始保证金</th><th>强平价</th>
        </tr></thead>
        <tbody>${{rows.map((row) => `<tr>
          <td>${{esc(row.symbol)}}</td>
          <td>${{esc(row.side)}}</td>
          <td>${{esc(row.positionAmt)}}</td>
          <td>${{esc(row.entryPrice)}}</td>
          <td>${{esc(row.markPrice)}}</td>
          <td>${{esc(row.unRealizedProfit)}}</td>
          <td>${{esc(row.notional)}}</td>
          <td>${{esc(row.initialMargin)}}</td>
          <td>${{esc(row.liquidationPrice)}}</td>
        </tr>`).join("")}}</tbody>
      </table>`;
    }}

    function renderAlgoOrders(rows) {{
      const wrap = document.getElementById("algoOrdersWrap");
      if (!rows || rows.length === 0) {{
        wrap.innerHTML = '<div class="empty">当前无条件单</div>';
        return;
      }}
      wrap.innerHTML = `<table>
        <thead><tr>
          <th>交易对</th><th>类型</th><th>方向</th><th>触发价</th><th>数量</th>
          <th>只减仓</th><th>全平</th><th>状态</th><th>触发类型</th><th>创建时间</th>
        </tr></thead>
        <tbody>${{rows.map((row) => `<tr>
          <td>${{esc(row.symbol)}}</td>
          <td>${{esc(row.orderType)}}</td>
          <td>${{esc(row.side)}}</td>
          <td>${{esc(row.triggerPrice)}}</td>
          <td>${{esc(row.quantity)}}</td>
          <td>${{esc(row.reduceOnly)}}</td>
          <td>${{esc(row.closePosition)}}</td>
          <td>${{esc(row.algoStatus)}}</td>
          <td>${{esc(row.workingType)}}</td>
          <td class="mono">${{esc(row.createTime)}}</td>
        </tr>`).join("")}}</tbody>
      </table>`;
    }}

    function renderExecutions(rows) {{
      const body = document.getElementById("executionsBody");
      if (!rows || rows.length === 0) {{
        body.innerHTML = '<tr><td colspan="13" class="empty">暂无记录</td></tr>';
        return;
      }}
      body.innerHTML = rows.map((row) => {{
        const status = row["状态"] || "";
        return `<tr>
          <td>${{esc(row["编号"])}}</td>
          <td class="mono">${{esc(row["创建时间"])}}</td>
          <td>${{esc(row["交易对"])}}</td>
          <td>${{esc(row["方向"])}}</td>
          <td><span class="status ${{esc(status)}}">${{esc(status)}}</span></td>
          <td>${{esc(row["状态说明"])}}</td>
          <td class="mono">${{esc(row["跳过原因"])}}</td>
          <td>${{esc(row["计划数量"])}}</td>
          <td>${{esc(row["成交数量"])}}</td>
          <td>${{esc(row["进场价"])}}</td>
          <td>${{esc(row["杠杆"])}}</td>
          <td class="mono">${{esc(row["信号编号"])}}</td>
          <td><button class="secondary" type="button" data-id="${{esc(row["编号"])}}">详情</button></td>
        </tr>`;
      }}).join("");
      body.querySelectorAll("button[data-id]").forEach((btn) => {{
        btn.addEventListener("click", () => openDetail(btn.getAttribute("data-id")));
      }});
    }}

    function renderBySymbol(rows) {{
      const body = document.getElementById("bySymbolBody");
      if (!rows || rows.length === 0) {{
        body.innerHTML = '<tr><td colspan="6" class="empty">暂无数据</td></tr>';
        return;
      }}
      body.innerHTML = rows.map((row) => `<tr>
        <td>${{esc(row.symbol)}}</td>
        <td>${{esc(row.total_executions)}}</td>
        <td>${{esc(row.protected_count)}}</td>
        <td>${{esc(row.entry_not_filled_count)}}</td>
        <td>${{esc(row.blocked_count)}}</td>
        <td>${{esc(row.protection_failed_count)}}</td>
      </tr>`).join("");
    }}

    function renderRejections(rows) {{
      const body = document.getElementById("rejectionsBody");
      if (!rows || rows.length === 0) {{
        body.innerHTML = '<tr><td colspan="3" class="empty">暂无数据</td></tr>';
        return;
      }}
      body.innerHTML = rows.map((row) => `<tr>
        <td class="mono">${{esc(row.reason)}}</td>
        <td>${{esc(row.status)}}</td>
        <td>${{esc(row.count)}}</td>
      </tr>`).join("");
    }}

    function renderRuntimeControlStatus(data) {{
      const el = document.getElementById("runtimeControlStatus");
      if (!data) {{
        el.innerHTML = '<div class="empty">暂无运行控制数据</div>';
        return;
      }}
      if (!data.enabled) {{
        el.innerHTML = '<div class="empty">Runtime Control 未启用</div>';
        return;
      }}
      const locked = !!data.locked;
      const lockLabel = locked ? "已锁定" : "未锁定";
      const lockClass = locked ? "locked" : "unlocked";
      const fields = [
        ["Runtime Control", "启用"],
        ["锁定原因", data.reason],
        ["锁定人", data.locked_by],
        ["锁定时间", data.locked_at],
        ["自动解锁时间", data.locked_until],
        ["更新时间", data.updated_at],
      ];
      let html = `<div><span>当前状态</span><span class="lock-badge ${{lockClass}}">${{lockLabel}}</span></div>`;
      html += fields.map(([k, v]) => {{
        const display = (v === null || v === undefined || v === "") ? "-" : v;
        return `<div><span>${{esc(k)}}</span>${{esc(display)}}</div>`;
      }}).join("");
      el.innerHTML = html;
    }}

    function renderRuntimeControlEvents(rows, errorMessage) {{
      const wrap = document.getElementById("runtimeControlEventsWrap");
      if (errorMessage) {{
        wrap.innerHTML = `<div class="empty">${{esc(errorMessage)}}</div>`;
        return;
      }}
      if (!rows || rows.length === 0) {{
        wrap.innerHTML = '<div class="empty">暂无运行控制事件</div>';
        return;
      }}
      wrap.innerHTML = `<table>
        <thead><tr>
          <th>时间</th><th>action</th><th>reason</th><th>actor</th><th>locked_until</th>
        </tr></thead>
        <tbody>${{rows.map((row) => `<tr>
          <td class="mono">${{esc(row.created_at)}}</td>
          <td>${{esc(row.action)}}</td>
          <td>${{esc(row.reason)}}</td>
          <td>${{esc(row.actor)}}</td>
          <td class="mono">${{esc(row.locked_until)}}</td>
        </tr>`).join("")}}</tbody>
      </table>`;
    }}

    async function loadAlertsSection() {{
      const wrap = document.getElementById("alertCenterWrap");
      try {{
        const resp = await apiFetch("/dashboard/api/alerts?limit=20");
        renderAlertCenter(resp);
      }} catch (err) {{
        wrap.innerHTML = `<div class="empty">${{esc(err.message || String(err))}}</div>`;
      }}
    }}

    function renderAlertCenter(resp) {{
      const wrap = document.getElementById("alertCenterWrap");
      const summary = resp.summary || {{}};
      const alerts = resp["告警"] || [];
      const level = (summary.latest_level || "OK").toLowerCase();
      const summaryCards = `
        <div class="cards" style="margin-bottom:0.75rem">
          <div class="card"><div class="label">ERROR</div><div class="value">${{esc(summary.error_count ?? 0)}}</div></div>
          <div class="card"><div class="label">WARN</div><div class="value">${{esc(summary.warn_count ?? 0)}}</div></div>
          <div class="card"><div class="label">INFO</div><div class="value">${{esc(summary.info_count ?? 0)}}</div></div>
          <div class="card"><div class="label">最新等级</div><div class="value health-level ${{level}}" style="font-size:1rem">${{esc(summary.latest_level || "OK")}}</div></div>
        </div>`;
      if (!alerts.length) {{
        wrap.innerHTML = summaryCards + '<div class="empty">当前无 WARN/ERROR/INFO 告警</div>';
        return;
      }}
      wrap.innerHTML = summaryCards + `<div style="overflow-x:auto"><table>
        <thead><tr>
          <th>时间</th><th>等级</th><th>来源</th><th>类型</th><th>标题</th><th>说明</th>
        </tr></thead>
        <tbody>${{alerts.map((a) => `<tr>
          <td class="mono">${{esc(a.created_at || "-")}}</td>
          <td><span class="check-level ${{esc((a.level||'OK').toLowerCase())}}">${{esc(a.level)}}</span></td>
          <td>${{esc(a.source)}}</td>
          <td class="mono">${{esc(a.type)}}</td>
          <td>${{esc(a.title)}}</td>
          <td>${{esc(a.message)}}</td>
        </tr>`).join("")}}</tbody>
      </table></div>`;
    }}

    async function loadHealthOverviewSection() {{
      const wrap = document.getElementById("healthOverviewWrap");
      try {{
        const resp = await apiFetch("/dashboard/api/health-overview");
        renderHealthOverview(resp["健康摘要"] || null);
      }} catch (err) {{
        wrap.innerHTML = `<div class="empty">${{esc(err.message || String(err))}}</div>`;
      }}
    }}

    function renderHealthOverview(data) {{
      const wrap = document.getElementById("healthOverviewWrap");
      if (!data) {{
        wrap.innerHTML = '<div class="empty">暂无健康摘要数据</div>';
        return;
      }}
      const level = (data.level || "OK").toLowerCase();
      const summary = data.summary || {{}};
      const checks = data.checks || [];
      const summaryHtml = [
        ["允许真实下单", summary.enable_trading ? "是" : "否"],
        ["Binance 状态", summary.binance_ok ? "正常" : "异常"],
        ["Runtime 锁定", summary.runtime_locked ? "是" : "否"],
        ["当前持仓数", summary.open_position_count ?? "-"],
        ["条件单数", summary.algo_order_count ?? "-"],
        ["最近执行数", summary.recent_execution_count ?? "-"],
        ["最近执行状态", summary.last_execution_status || "-"],
        ["最近拒绝原因", summary.last_rejection_reason || "-"],
      ].map(([k, v]) => `<div><span>${{esc(k)}}</span>${{esc(v)}}</div>`).join("");
      const checksHtml = checks.length ? `<table style="margin-top:0.75rem">
        <thead><tr><th>检查项</th><th>等级</th><th>说明</th></tr></thead>
        <tbody>${{checks.map((c) => `<tr>
          <td class="mono">${{esc(c.name)}}</td>
          <td><span class="check-level ${{esc((c.level||'OK').toLowerCase())}}">${{esc(c.level)}}</span></td>
          <td>${{esc(c.message)}}</td>
        </tr>`).join("")}}</tbody>
      </table>` : '<div class="empty">无检查项</div>';
      wrap.innerHTML = `
        <div class="health-level ${{level}}">总体等级: ${{esc(data.level || "OK")}}</div>
        <div class="detail-meta">${{summaryHtml}}</div>
        <h3 class="subsection-title">风险提示</h3>
        ${{checksHtml}}
      `;
    }}

    async function loadRuntimeControlSection() {{
      const statusEl = document.getElementById("runtimeControlStatus");
      try {{
        const statusResp = await apiFetch("/dashboard/api/runtime-control/status");
        renderRuntimeControlStatus(statusResp["运行控制"] || null);
      }} catch (err) {{
        statusEl.innerHTML = `<div class="empty">${{esc(err.message || String(err))}}</div>`;
      }}
      try {{
        const eventsResp = await apiFetch("/dashboard/api/runtime-control/events?limit=10");
        renderRuntimeControlEvents(eventsResp["事件"] || []);
      }} catch (err) {{
        renderRuntimeControlEvents([], err.message || String(err));
      }}
    }}

    async function loadAll() {{
      clearError();
      const symbol = document.getElementById("filterSymbol").value.trim();
      const status = document.getElementById("filterStatus").value;
      const limit = document.getElementById("filterLimit").value || "50";
      const params = new URLSearchParams();
      params.set("limit", limit);
      if (symbol) params.set("symbol", symbol);
      if (status) params.set("status", status);

      try {{
        const [summaryResp, execResp, bySymbolResp, rejectResp, runtimeResp, healthResp, posResp, algoResp] = await Promise.all([
          apiFetch("/dashboard/api/summary"),
          apiFetch("/dashboard/api/executions?" + params.toString()),
          apiFetch("/dashboard/api/by-symbol"),
          apiFetch("/dashboard/api/rejections"),
          apiFetch("/dashboard/api/runtime"),
          apiFetch("/dashboard/api/health"),
          apiFetch("/dashboard/api/positions"),
          apiFetch("/dashboard/api/algo-orders"),
        ]);
        renderSummary(summaryResp["统计"] || {{}});
        renderKeyValueGrid("runtimeMeta", runtimeResp["运行配置"] || {{}}, RUNTIME_LABELS);
        renderKeyValueGrid("healthMeta", healthResp["健康"] || {{}}, HEALTH_LABELS);
        renderPositions(posResp["持仓"] || []);
        renderAlgoOrders(algoResp["条件单"] || []);
        renderExecutions(execResp["记录"] || []);
        renderBySymbol(bySymbolResp["按交易对"] || []);
        renderRejections(rejectResp["拒绝统计"] || []);
      }} catch (err) {{
        showError(err.message || String(err));
      }}
      await loadHealthOverviewSection();
      await loadAlertsSection();
      await loadRuntimeControlSection();
    }}

    async function openDetail(id) {{
      try {{
        const [detailResp, ordersResp] = await Promise.all([
          apiFetch("/dashboard/api/executions/" + id),
          apiFetch("/dashboard/api/orders/" + id),
        ]);
        const record = detailResp["记录"] || {{}};
        document.getElementById("detailTitle").textContent = "执行详情 #" + id;
        document.getElementById("detailMeta").innerHTML = [
          ["交易对", record["交易对"]],
          ["方向", record["方向"]],
          ["状态", record["状态"]],
          ["状态说明", record["状态说明"]],
          ["跳过原因", record["跳过原因"]],
          ["计划数量", record["计划数量"]],
          ["成交数量", record["成交数量"]],
          ["进场价", record["进场价"]],
          ["杠杆", record["杠杆"]],
          ["信号编号", record["信号编号"]],
          ["创建时间", record["创建时间"]],
        ].map(([k, v]) => `<div><span>${{esc(k)}}</span>${{esc(v)}}</div>`).join("");
        document.getElementById("detailRawSignal").textContent = fmtJson(record["原始信号"]);
        document.getElementById("detailPlan").textContent = fmtJson(record["交易计划"]);
        document.getElementById("detailAccountRisk").textContent = fmtJson(record["账户风控"]);
        document.getElementById("detailEntrySummary").textContent = fmtJson(record["进场摘要"]);
        document.getElementById("detailProtectionSummary").textContent = fmtJson(record["保护单摘要"]);
        document.getElementById("detailResult").textContent = fmtJson(record["执行结果"]);
        document.getElementById("detailOrders").textContent = fmtJson(ordersResp["订单"] || []);
        document.getElementById("detailBackdrop").style.display = "flex";
      }} catch (err) {{
        showError(err.message || String(err));
      }}
    }}

    function closeDetail() {{
      document.getElementById("detailBackdrop").style.display = "none";
    }}

    function setupAutoRefresh() {{
      if (refreshTimer) clearInterval(refreshTimer);
      if (AUTO_REFRESH_SEC > 0) {{
        refreshTimer = setInterval(loadAll, AUTO_REFRESH_SEC * 1000);
      }} else {{
        document.getElementById("refreshHint").textContent = "自动刷新: 关闭";
      }}
    }}

    document.getElementById("refreshBtn").addEventListener("click", loadAll);
    document.getElementById("closeDetailBtn").addEventListener("click", closeDetail);
    document.getElementById("detailBackdrop").addEventListener("click", (ev) => {{
      if (ev.target.id === "detailBackdrop") closeDetail();
    }});

    dashboardToken = readTokenFromUrl();
    loadAll();
    setupAutoRefresh();
  </script>
</body>
</html>"""


def create_dashboard_router(
    settings: Settings,
    journal_store: TradeJournalStore,
    trade_stats: TradeStatsService,
    client: BinanceClient,
    app_version: str,
    runtime_control: RuntimeControl,
) -> APIRouter:
    router = APIRouter(prefix="/dashboard", tags=["dashboard"])

    def guard(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ) -> None:
        _check_dashboard_access(
            settings,
            query_token=token,
            header_token=x_dashboard_token,
        )

    @router.get("", include_in_schema=False)
    @router.get("/", include_in_schema=False)
    async def dashboard_page(
        request: Request,
        token: str | None = Query(None),
    ) -> HTMLResponse:
        if not settings.dashboard_enabled:
            return HTMLResponse(
                content=_error_html("Dashboard 未启用", "请在 .env 中设置 DASHBOARD_ENABLED=true"),
                status_code=404,
            )
        try:
            _check_dashboard_access(
                settings,
                query_token=token,
                header_token=request.headers.get("X-Dashboard-Token"),
            )
        except HTTPException as exc:
            if exc.status_code == 401:
                return HTMLResponse(
                    content=_error_html(
                        "访问被拒绝",
                        "请通过 /dashboard?token=你的密钥 访问，Token 需与 DASHBOARD_TOKEN 一致。",
                    ),
                    status_code=401,
                )
            if exc.status_code == 403:
                return HTMLResponse(
                    content=_error_html("Dashboard 未配置", "服务端尚未设置 DASHBOARD_TOKEN。"),
                    status_code=403,
                )
            raise
        return HTMLResponse(content=render_dashboard_html(settings.dashboard_auto_refresh_sec))

    @router.get("/api/summary")
    async def api_summary(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        return JSONResponse(content={"成功": True, "统计": trade_stats.summary()})

    @router.get("/api/by-symbol")
    async def api_by_symbol(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        rows = trade_stats.by_symbol()
        return JSONResponse(content={"成功": True, "数量": len(rows), "按交易对": rows})

    @router.get("/api/rejections")
    async def api_rejections(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
        limit: int = 20,
    ):
        guard(request, token, x_dashboard_token)
        rows = trade_stats.rejections(limit=limit)
        return JSONResponse(content={"成功": True, "数量": len(rows), "拒绝统计": rows})

    @router.get("/api/executions")
    async def api_executions(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
        limit: int = 50,
        symbol: str | None = None,
        status: str | None = None,
    ):
        guard(request, token, x_dashboard_token)
        rows = journal_store.list_executions(limit=limit, symbol=symbol, status=status)
        return JSONResponse(
            content={
                "成功": True,
                "数量": len(rows),
                "记录": [TradeStatsService.execution_brief(row) for row in rows],
            }
        )

    @router.get("/api/executions/{execution_id}")
    async def api_execution_detail(
        execution_id: int,
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        row = journal_store.get_execution(execution_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"执行记录不存在: {execution_id}")
        return JSONResponse(
            content={"成功": True, "记录": TradeStatsService.execution_detail(row)}
        )

    @router.get("/api/orders/{execution_id}")
    async def api_orders(
        execution_id: int,
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        if journal_store.get_execution(execution_id) is None:
            raise HTTPException(status_code=404, detail=f"执行记录不存在: {execution_id}")
        rows = journal_store.list_orders(execution_id)
        return JSONResponse(
            content={
                "成功": True,
                "执行编号": execution_id,
                "订单数量": len(rows),
                "订单": [TradeStatsService.order_brief(row) for row in rows],
            }
        )

    @router.get("/api/runtime")
    async def api_runtime(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        return JSONResponse(
            content={"成功": True, "运行配置": build_runtime_config(settings, app_version)}
        )

    @router.get("/api/positions")
    async def api_positions(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        try:
            rows = build_dashboard_positions(client)
        except Exception as exc:
            return JSONResponse(
                content={"成功": False, "错误": str(exc)[:500], "持仓": []},
                status_code=200,
            )
        return JSONResponse(content={"成功": True, "数量": len(rows), "持仓": rows})

    @router.get("/api/algo-orders")
    async def api_algo_orders(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        try:
            rows = build_dashboard_algo_orders(settings, client)
        except Exception as exc:
            return JSONResponse(
                content={"成功": False, "错误": str(exc)[:500], "条件单": []},
                status_code=200,
            )
        return JSONResponse(content={"成功": True, "数量": len(rows), "条件单": rows})

    @router.get("/api/health")
    async def api_health(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        return JSONResponse(content={"成功": True, "健康": build_dashboard_health(settings, client)})

    @router.get("/api/runtime-control/status")
    async def api_runtime_control_status(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        _guard_runtime_control_dashboard_read(
            settings,
            query_token=token,
            header_token=x_dashboard_token,
        )
        return JSONResponse(
            content={"成功": True, "运行控制": runtime_control.status_payload()}
        )

    @router.get("/api/runtime-control/events")
    async def api_runtime_control_events(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
        limit: int = 10,
    ):
        _guard_runtime_control_dashboard_read(
            settings,
            query_token=token,
            header_token=x_dashboard_token,
        )
        rows = runtime_control.list_events(limit=limit)
        return JSONResponse(content={"成功": True, "数量": len(rows), "事件": rows})

    @router.get("/api/health-overview")
    async def api_health_overview(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        try:
            overview = build_health_overview(
                settings, client, journal_store, trade_stats, runtime_control
            )
        except Exception as exc:
            return JSONResponse(
                content={
                    "成功": False,
                    "错误": str(exc)[:500],
                    "健康摘要": {
                        "level": "ERROR",
                        "checks": [
                            {
                                "name": "health_overview",
                                "level": "ERROR",
                                "message": f"健康摘要生成失败: {str(exc)[:200]}",
                            }
                        ],
                        "summary": {},
                    },
                },
                status_code=200,
            )
        return JSONResponse(content={"成功": True, "健康摘要": overview})

    @router.get("/api/alerts")
    async def api_alerts(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
        limit: int = 20,
    ):
        guard(request, token, x_dashboard_token)
        try:
            payload = build_alerts(
                settings,
                client,
                journal_store,
                trade_stats,
                runtime_control,
                limit=limit,
            )
        except Exception as exc:
            return JSONResponse(
                content={
                    "成功": False,
                    "错误": str(exc)[:500],
                    "数量": 0,
                    "告警": [],
                    "summary": {
                        "total": 0,
                        "error_count": 0,
                        "warn_count": 0,
                        "info_count": 0,
                        "latest_level": "ERROR",
                    },
                },
                status_code=200,
            )
        return JSONResponse(content={"成功": True, **payload})

    return router

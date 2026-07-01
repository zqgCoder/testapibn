from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .trader import Trader

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .stats import TradeStatsService
from .storage import TradeJournalStore
from .tv_cloud_audit import alerts_from_tv_cloud_audit, build_tv_cloud_audit
from .tv_observation import alerts_from_tv_observation, build_tv_alert_readiness, build_tv_observation
from .tv_sandbox import binance_env_label, is_tv_execution_row

if TYPE_CHECKING:
    from .binance_client import BinanceClient
    from .config import Settings
    from .reconcile import SafetyReconcileService
    from .runtime_control import RuntimeControl


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
    runtime_locked = bool(runtime_state.get("effective_locked", runtime_state.get("locked")))
    one_shot = runtime_state.get("one_shot") or {}
    summary["runtime_locked"] = runtime_locked
    summary["runtime_one_shot_active"] = bool(
        one_shot.get("enabled") and int(one_shot.get("remaining") or 0) > 0
    )
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

    if runtime_enabled and summary.get("runtime_one_shot_active"):
        checks.append(
            {
                "name": "runtime_one_shot",
                "level": "WARN",
                "message": (
                    f"Runtime One-Shot 已启用，剩余 {one_shot.get('remaining', 0)} 次，"
                    f"expires={one_shot.get('expires_at') or '-'}"
                ),
            }
        )
    elif runtime_enabled and one_shot.get("consumed_at"):
        checks.append(
            {
                "name": "runtime_one_shot",
                "level": "INFO",
                "message": (
                    f"最近 One-Shot 已被消费: signal_id={one_shot.get('consumed_by_signal_id') or '-'}, "
                    f"at={one_shot.get('consumed_at') or '-'}"
                ),
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
    "runtime_one_shot": "Runtime One-Shot 状态",
    "safety_audit": "安全审计",
}

_JOURNAL_ALERT_RULES: dict[str, tuple[str, str]] = {
    "failed": ("ERROR", "执行异常"),
    "protection_failed": ("ERROR", "保护单失败"),
    "blocked_by_account_risk": ("WARN", "账户风控拒绝"),
    "blocked_by_runtime_lock": ("WARN", "信号被 Runtime Lock 拦截"),
    "tv_sandbox_rejected": ("WARN", "TradingView 信号被沙盒拒绝"),
    "entry_not_filled": ("WARN", "信号未成交"),
    "skipped_by_position_policy": ("WARN", "持仓策略跳过"),
}

_RUNTIME_EVENT_RULES: dict[str, tuple[str, str, str]] = {
    "lock": ("WARN", "runtime_lock", "Runtime Control 被锁定"),
    "auto_expire": ("INFO", "runtime_auto_expire", "Runtime Control 自动解锁"),
    "unlock": ("INFO", "runtime_unlock", "Runtime Control 已解锁"),
    "unlock_once": ("INFO", "runtime_one_shot_unlock", "Runtime One-Shot 已启用"),
    "one_shot_consumed": ("WARN", "runtime_one_shot_consumed", "Runtime One-Shot 已被信号消费"),
    "one_shot_expired": ("WARN", "runtime_one_shot_expired", "Runtime One-Shot 已过期"),
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
    skip_reason = row.get("skip_reason")
    rule = _JOURNAL_ALERT_RULES.get(status)
    if rule is None and skip_reason and str(skip_reason).startswith("tv_"):
        rule = ("WARN", "TradingView 信号被沙盒拒绝")
    if rule is None:
        return None
    level, title = rule
    symbol = row.get("symbol")
    signal_id = row.get("signal_id")
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
    reconcile_service: SafetyReconcileService | None = None,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    from .reconcile import alerts_from_reconcile_report

    cap = max(1, min(limit, 100))
    now_iso = datetime.now(timezone.utc).isoformat()
    alerts: list[dict[str, Any]] = []

    if reconcile_service is not None:
        reconcile_report = reconcile_service.get_latest_report()
        alerts.extend(alerts_from_reconcile_report(reconcile_report, created_at=now_iso))

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

    try:
        observation = build_tv_observation(
            settings, journal_store, client, runtime_control
        )
        alerts.extend(alerts_from_tv_observation(observation, created_at=now_iso))
    except Exception:
        pass

    try:
        cloud_audit = build_tv_cloud_audit(settings, journal_store)
        alerts.extend(alerts_from_tv_cloud_audit(cloud_audit, created_at=now_iso))
    except Exception:
        pass

    runtime_events = runtime_control.list_events(limit=cap)
    for event in runtime_events:
        alert = _alert_from_runtime_event(event)
        if alert:
            alerts.append(alert)

    runtime_state = runtime_control.status_payload()
    effective_locked = bool(runtime_state.get("effective_locked", runtime_state.get("locked")))
    if runtime_state.get("enabled") and effective_locked:
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
    one_shot = runtime_state.get("one_shot") or {}
    if (
        runtime_state.get("enabled")
        and one_shot.get("enabled")
        and int(one_shot.get("remaining") or 0) > 0
    ):
        alerts.append(
            {
                "id": "runtime-one-shot-active",
                "source": "runtime",
                "level": "WARN",
                "type": "runtime_one_shot_unlock",
                "title": "Runtime One-Shot 等待下一条 TV 信号",
                "message": (
                    f"remaining={one_shot.get('remaining')}, expires={one_shot.get('expires_at') or '-'}, "
                    f"reason={one_shot.get('reason') or '-'}"
                ),
                "symbol": None,
                "status": None,
                "reason": one_shot.get("reason"),
                "created_at": one_shot.get("started_at") or runtime_state.get("updated_at"),
            }
        )

    alerts.sort(
        key=lambda item: (str(item.get("created_at") or ""), _health_level_rank(str(item.get("level", "OK")))),
        reverse=True,
    )
    alerts = alerts[:cap]
    summary = _alert_summary(alerts)
    return {"告警": alerts, "summary": summary, "数量": len(alerts)}


def _secret_meta(value: str) -> dict[str, bool | int]:
    stripped = (value or "").strip()
    return {"configured": bool(stripped), "length": len(stripped)}


def build_tv_sandbox_status(settings: Settings, journal_store: TradeJournalStore) -> dict[str, Any]:
    last_tv_execution = None
    for row in journal_store.list_executions(limit=100):
        if is_tv_execution_row(row, settings):
            last_tv_execution = {
                "id": row.get("id"),
                "signal_id": row.get("signal_id"),
                "symbol": row.get("symbol"),
                "status": row.get("status"),
                "skip_reason": row.get("skip_reason"),
                "created_at": row.get("created_at"),
            }
            break
    return {
        "enabled": settings.tv_signal_sandbox_enabled,
        "binance_env": binance_env_label(settings.binance_base_url),
        "reject_live_binance": settings.tv_signal_reject_live_binance,
        "allowed_sources": sorted(settings.tv_signal_allowed_source_set),
        "signal_id_prefix": settings.tv_signal_id_prefix,
        "max_risk_usdt": settings.tv_signal_max_risk_usdt,
        "max_margin_usdt": settings.tv_signal_max_margin_usdt,
        "allowed_entry_types": sorted(settings.tv_signal_allowed_entry_type_set),
        "require_source": settings.tv_signal_require_source,
        "last_tv_execution": last_tv_execution,
    }


def build_risk_config_inspector(settings: Settings, app_version: str) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    symbols = sorted(settings.allowed_symbol_set)
    binance_env = binance_env_label(settings.binance_base_url)
    webhook_meta = _secret_meta(settings.webhook_secret)
    dashboard_token_meta = _secret_meta(settings.dashboard_token)
    runtime_token_meta = _secret_meta(settings.runtime_control_token)
    binance_key_meta = _secret_meta(settings.binance_api_key)
    binance_secret_meta = _secret_meta(settings.binance_api_secret)

    if binance_env == "demo":
        checks.append(
            {
                "name": "binance_environment",
                "level": "OK",
                "message": f"当前使用 Binance 模拟/测试环境 ({settings.binance_base_url})",
            }
        )
    elif binance_env == "live":
        if settings.enable_trading:
            checks.append(
                {
                    "name": "binance_environment",
                    "level": "ERROR",
                    "message": "当前为 Binance 实盘 endpoint 且 ENABLE_TRADING=true，请确认环境配置",
                }
            )
        else:
            checks.append(
                {
                    "name": "binance_environment",
                    "level": "WARN",
                    "message": "当前为 Binance 实盘 endpoint，但 ENABLE_TRADING=false",
                }
            )
    else:
        checks.append(
            {
                "name": "binance_environment",
                "level": "WARN",
                "message": f"无法识别 Binance 环境类型: {settings.binance_base_url}",
            }
        )

    if settings.enable_trading:
        checks.append(
            {
                "name": "enable_trading",
                "level": "WARN",
                "message": "真实交易开关已启用，请确认当前环境为 demo/testnet 且风控已配置",
            }
        )
    else:
        checks.append(
            {
                "name": "enable_trading",
                "level": "OK",
                "message": "交易执行已关闭，仅计划/监控，不会真实下单",
            }
        )

    if not webhook_meta["configured"]:
        checks.append(
            {
                "name": "webhook_secret",
                "level": "ERROR",
                "message": "WEBHOOK_SECRET 未配置",
            }
        )
    elif int(webhook_meta["length"]) < 20:
        checks.append(
            {
                "name": "webhook_secret",
                "level": "WARN",
                "message": f"WEBHOOK_SECRET 已配置但长度过短 (length={webhook_meta['length']}, 建议>=20)",
            }
        )
    else:
        checks.append(
            {
                "name": "webhook_secret",
                "level": "OK",
                "message": f"WEBHOOK_SECRET 已配置，长度正常 (length={webhook_meta['length']})",
            }
        )

    if not settings.dashboard_enabled:
        checks.append(
            {
                "name": "dashboard_token",
                "level": "WARN",
                "message": "DASHBOARD_ENABLED=false，Dashboard 未启用",
            }
        )
    elif not settings.dashboard_require_token:
        checks.append(
            {
                "name": "dashboard_token",
                "level": "ERROR",
                "message": "DASHBOARD_REQUIRE_TOKEN=false，Dashboard 未强制 Token 保护",
            }
        )
    elif not dashboard_token_meta["configured"]:
        checks.append(
            {
                "name": "dashboard_token",
                "level": "ERROR",
                "message": "DASHBOARD_TOKEN 未配置",
            }
        )
    else:
        level = "OK" if int(dashboard_token_meta["length"]) >= 16 else "WARN"
        msg = (
            f"DASHBOARD_TOKEN 已配置 (length={dashboard_token_meta['length']})"
            if level == "OK"
            else f"DASHBOARD_TOKEN 长度过短 (length={dashboard_token_meta['length']}, 建议>=16)"
        )
        checks.append({"name": "dashboard_token", "level": level, "message": msg})

    if settings.runtime_control_enabled:
        checks.append(
            {
                "name": "runtime_control",
                "level": "OK",
                "message": "RUNTIME_CONTROL_ENABLED=true",
            }
        )
        if not settings.runtime_control_require_token:
            checks.append(
                {
                    "name": "runtime_control_token",
                    "level": "ERROR",
                    "message": "RUNTIME_CONTROL_REQUIRE_TOKEN=false，运行控制写操作未强制 Token",
                }
            )
        elif not runtime_token_meta["configured"]:
            checks.append(
                {
                    "name": "runtime_control_token",
                    "level": "ERROR",
                    "message": "RUNTIME_CONTROL_TOKEN 未配置",
                }
            )
        else:
            checks.append(
                {
                    "name": "runtime_control_token",
                    "level": "OK",
                    "message": f"RUNTIME_CONTROL_TOKEN 已配置 (length={runtime_token_meta['length']})",
                }
            )
    else:
        checks.append(
            {
                "name": "runtime_control",
                "level": "WARN",
                "message": "RUNTIME_CONTROL_ENABLED=false，无法手动锁定机器人",
            }
        )

    if settings.runtime_status_allow_dashboard_token:
        checks.append(
            {
                "name": "runtime_status_permission",
                "level": "OK",
                "message": "RUNTIME_STATUS_ALLOW_DASHBOARD_TOKEN=true，Dashboard 可读取运行控制状态",
            }
        )
    else:
        checks.append(
            {
                "name": "runtime_status_permission",
                "level": "WARN",
                "message": "RUNTIME_STATUS_ALLOW_DASHBOARD_TOKEN=false，Dashboard 无法读取运行控制状态",
            }
        )

    if settings.protect_journal_api and settings.protect_stats_api:
        checks.append(
            {
                "name": "journal_stats_protection",
                "level": "OK",
                "message": "Journal 与 Stats API 均已启用 Token 保护",
            }
        )
    elif settings.protect_journal_api or settings.protect_stats_api:
        checks.append(
            {
                "name": "journal_stats_protection",
                "level": "WARN",
                "message": (
                    f"部分 API 未保护: PROTECT_JOURNAL_API={settings.protect_journal_api}, "
                    f"PROTECT_STATS_API={settings.protect_stats_api}"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "journal_stats_protection",
                "level": "ERROR",
                "message": "Journal 与 Stats API 均未启用 Token 保护",
            }
        )

    if not symbols:
        checks.append(
            {
                "name": "allowed_symbols",
                "level": "ERROR",
                "message": "ALLOWED_SYMBOLS 为空",
            }
        )
    else:
        symbol_level = "OK"
        symbol_msg = f"允许交易对 {len(symbols)} 个: {', '.join(symbols)}"
        if len(symbols) > 10:
            symbol_level = "WARN"
            symbol_msg = f"允许交易对数量较多 ({len(symbols)} 个)，请确认范围"
        non_usdt = [s for s in symbols if not s.endswith("USDT")]
        if non_usdt:
            symbol_level = "WARN"
            symbol_msg += f"；非常见 USDT 永续格式: {', '.join(non_usdt)}"
        checks.append({"name": "allowed_symbols", "level": symbol_level, "message": symbol_msg})

    leverage = settings.max_auto_leverage
    if leverage > 50:
        checks.append(
            {
                "name": "leverage_policy",
                "level": "ERROR",
                "message": f"MAX_AUTO_LEVERAGE={leverage} 过高 (>{50})",
            }
        )
    elif leverage > 20:
        checks.append(
            {
                "name": "leverage_policy",
                "level": "WARN",
                "message": f"MAX_AUTO_LEVERAGE={leverage} 偏高 (>{20})",
            }
        )
    else:
        checks.append(
            {
                "name": "leverage_policy",
                "level": "OK",
                "message": f"MAX_AUTO_LEVERAGE={leverage} 在推荐范围内",
            }
        )

    entry_issues: list[str] = []
    if settings.allow_market_entry:
        entry_issues.append("ALLOW_MARKET_ENTRY=true")
    if settings.default_entry_type == "market":
        entry_issues.append("DEFAULT_ENTRY_TYPE=market")
    if settings.default_limit_fallback_to_market:
        entry_issues.append("DEFAULT_LIMIT_FALLBACK_TO_MARKET=true")
    if entry_issues:
        checks.append(
            {
                "name": "order_entry_policy",
                "level": "WARN",
                "message": f"入场策略偏激进: {', '.join(entry_issues)}",
            }
        )
    else:
        checks.append(
            {
                "name": "order_entry_policy",
                "level": "OK",
                "message": "限价进场为主，未启用限价超时改市价",
            }
        )
    if settings.allow_limit_entry:
        pass  # covered in OK message above

    policy = settings.default_position_policy
    policy_labels = {
        "replace": "replace：先清理旧仓再开新仓",
        "reverse_only": "reverse_only：仅反向信号平旧仓",
        "ignore_same_side": "ignore_same_side：同向跳过",
        "add": "add：保留旧仓加仓（谨慎）",
    }
    if settings.emergency_close_on_protection_fail:
        checks.append(
            {
                "name": "protection_policy",
                "level": "WARN",
                "message": (
                    f"EMERGENCY_CLOSE_ON_PROTECTION_FAIL=true，保护单失败将触发紧急平仓；"
                    f"持仓策略={policy_labels.get(policy, policy)}"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "protection_policy",
                "level": "OK",
                "message": (
                    f"保护单失败不会自动平仓；持仓策略={policy_labels.get(policy, policy)}"
                ),
            }
        )

    if not settings.account_risk_enabled:
        checks.append(
            {
                "name": "account_risk_guard",
                "level": "ERROR",
                "message": "ACCOUNT_RISK_ENABLED=false，账户级风控未启用",
            }
        )
    else:
        risk_warnings: list[str] = []
        if settings.daily_max_loss_usdt <= 0:
            risk_warnings.append("DAILY_MAX_LOSS_USDT 未限制")
        elif settings.daily_max_loss_usdt > 500:
            risk_warnings.append(f"DAILY_MAX_LOSS_USDT={settings.daily_max_loss_usdt} 偏大")
        if settings.daily_max_trades <= 0:
            risk_warnings.append("DAILY_MAX_TRADES 未限制")
        elif settings.daily_max_trades > 50:
            risk_warnings.append(f"DAILY_MAX_TRADES={settings.daily_max_trades} 偏大")
        if settings.max_open_positions <= 0:
            risk_warnings.append("MAX_OPEN_POSITIONS 未限制")
        elif settings.max_open_positions > 10:
            risk_warnings.append(f"MAX_OPEN_POSITIONS={settings.max_open_positions} 偏大")
        if settings.max_total_risk_usdt <= 0:
            risk_warnings.append("MAX_TOTAL_RISK_USDT 未限制")
        elif settings.max_total_risk_usdt > 1000:
            risk_warnings.append(f"MAX_TOTAL_RISK_USDT={settings.max_total_risk_usdt} 偏大")
        if risk_warnings:
            checks.append(
                {
                    "name": "account_risk_guard",
                    "level": "WARN",
                    "message": f"账户风控已启用，但参数偏宽: {'; '.join(risk_warnings)}",
                }
            )
        else:
            checks.append(
                {
                    "name": "account_risk_guard",
                    "level": "OK",
                    "message": "账户级风控已启用且参数在合理范围",
                }
            )

    if settings.enable_trading:
        if binance_key_meta["configured"] and binance_secret_meta["configured"]:
            checks.append(
                {
                    "name": "binance_credentials",
                    "level": "OK",
                    "message": (
                        "Binance API 凭证已配置 "
                        f"(key_length={binance_key_meta['length']}, secret_length={binance_secret_meta['length']})"
                    ),
                }
            )
        else:
            checks.append(
                {
                    "name": "binance_credentials",
                    "level": "ERROR",
                    "message": "ENABLE_TRADING=true 但 Binance API 凭证未完整配置",
                }
            )

    if settings.tv_signal_sandbox_enabled:
        checks.append(
            {
                "name": "tv_signal_sandbox",
                "level": "OK",
                "message": "TV Signal Sandbox 已启用，TradingView 信号将受沙盒校验",
            }
        )
    else:
        checks.append(
            {
                "name": "tv_signal_sandbox",
                "level": "WARN",
                "message": "TV_SIGNAL_SANDBOX_ENABLED=false，TradingView 信号未启用沙盒保护",
            }
        )

    if settings.tv_signal_sandbox_enabled:
        if settings.tv_signal_reject_live_binance and binance_env == "demo":
            checks.append(
                {
                    "name": "tv_signal_live_guard",
                    "level": "OK",
                    "message": "demo/testnet 环境且 TV_SIGNAL_REJECT_LIVE_BINANCE=true",
                }
            )
        elif settings.tv_signal_reject_live_binance and binance_env == "live":
            checks.append(
                {
                    "name": "tv_signal_live_guard",
                    "level": "ERROR",
                    "message": "TV Sandbox 已启用但 Binance 为实盘 endpoint，TV 信号将被拒绝",
                }
            )
        elif not settings.tv_signal_reject_live_binance:
            checks.append(
                {
                    "name": "tv_signal_live_guard",
                    "level": "WARN",
                    "message": "TV_SIGNAL_REJECT_LIVE_BINANCE=false，TV 信号可能在实盘 endpoint 执行",
                }
            )
        else:
            checks.append(
                {
                    "name": "tv_signal_live_guard",
                    "level": "WARN",
                    "message": f"无法识别 Binance 环境 ({binance_env})，请确认 endpoint",
                }
            )

    risk_level = "OK"
    risk_msg = f"TV_SIGNAL_MAX_RISK_USDT={settings.tv_signal_max_risk_usdt}"
    if settings.tv_signal_max_risk_usdt > 10:
        risk_level = "WARN"
        risk_msg += " 偏高 (>10)"
    checks.append({"name": "tv_signal_risk_limit", "level": risk_level, "message": risk_msg})

    prefix = settings.tv_signal_id_prefix.strip()
    if not prefix:
        checks.append(
            {
                "name": "tv_signal_source_policy",
                "level": "ERROR",
                "message": "TV_SIGNAL_ID_PREFIX 为空",
            }
        )
    elif not settings.tv_signal_allowed_source_set:
        checks.append(
            {
                "name": "tv_signal_source_policy",
                "level": "ERROR",
                "message": "TV_SIGNAL_ALLOWED_SOURCES 为空",
            }
        )
    else:
        source_msg = (
            f"require_source={settings.tv_signal_require_source}, "
            f"allowed={', '.join(sorted(settings.tv_signal_allowed_source_set))}, "
            f"prefix={prefix}"
        )
        checks.append(
            {
                "name": "tv_signal_source_policy",
                "level": "OK",
                "message": source_msg,
            }
        )

    if settings.tv_alert_observation_enabled:
        checks.append(
            {
                "name": "tv_alert_observation",
                "level": "OK",
                "message": "TV Alert 连续观察已启用",
            }
        )
    else:
        checks.append(
            {
                "name": "tv_alert_observation",
                "level": "WARN",
                "message": "TV_ALERT_OBSERVATION_ENABLED=false",
            }
        )

    public_url = (settings.tv_alert_public_base_url or "").strip()
    if public_url.startswith("http://") or public_url.startswith("https://"):
        checks.append(
            {
                "name": "tv_alert_public_url",
                "level": "OK",
                "message": f"TV_ALERT_PUBLIC_BASE_URL 已配置，Webhook: {public_url.rstrip('/')}/tradingview",
            }
        )
    else:
        checks.append(
            {
                "name": "tv_alert_public_url",
                "level": "WARN",
                "message": "TV_ALERT_PUBLIC_BASE_URL 未配置，TradingView 无法公网回调",
            }
        )

    stale_level = "OK"
    stale_msg = f"TV_ALERT_STALE_MINUTES={settings.tv_alert_stale_minutes}"
    if settings.tv_alert_stale_minutes > 360:
        stale_level = "WARN"
        stale_msg += " 偏长，可能延迟发现信号中断"
    checks.append({"name": "tv_alert_stale_policy", "level": stale_level, "message": stale_msg})

    fail_level = "OK"
    fail_msg = (
        f"连续失败 WARN>={settings.tv_alert_consecutive_failure_warn}, "
        f"ERROR>={settings.tv_alert_consecutive_failure_error}"
    )
    if settings.tv_alert_consecutive_failure_warn >= settings.tv_alert_consecutive_failure_error:
        fail_level = "ERROR"
        fail_msg += " 阈值配置无效"
    checks.append({"name": "tv_alert_failure_policy", "level": fail_level, "message": fail_msg})

    if settings.tv_cloud_audit_enabled:
        checks.append(
            {
                "name": "tv_cloud_audit_enabled",
                "level": "OK",
                "message": "TV 云端 Alert 审计已启用",
            }
        )
    else:
        checks.append(
            {
                "name": "tv_cloud_audit_enabled",
                "level": "WARN",
                "message": "TV_CLOUD_AUDIT_ENABLED=false",
            }
        )

    window_level = "OK"
    window_msg = f"TV_CLOUD_AUDIT_WINDOW_HOURS={settings.tv_cloud_audit_window_hours}"
    if settings.tv_cloud_audit_window_hours > 168:
        window_level = "ERROR"
        window_msg += " 超出上限 168"
    elif settings.tv_cloud_audit_window_hours < 1:
        window_level = "ERROR"
        window_msg += " 无效"
    checks.append({"name": "tv_cloud_audit_window", "level": window_level, "message": window_msg})

    checks.append(
        {
            "name": "tv_cloud_duplicate_policy",
            "level": "OK",
            "message": f"重复 signal_id WARN>={settings.tv_cloud_duplicate_signal_warn}",
        }
    )
    checks.append(
        {
            "name": "tv_cloud_payload_invalid_policy",
            "level": "OK",
            "message": f"tv_payload_invalid WARN>={settings.tv_cloud_payload_invalid_warn}",
        }
    )
    checks.append(
        {
            "name": "tv_cloud_unauthorized_policy",
            "level": "INFO",
            "message": (
                f"401 未授权 WARN 阈值>={settings.tv_cloud_unauthorized_warn}；"
                "v6.2 未从 journal 统计 401，请结合 access log"
            ),
        }
    )

    checks.append(
        {
            "name": "dashboard_readonly_guarantee",
            "level": "OK",
            "message": "Dashboard 仅展示监控信息，不提供交易操作按钮",
        }
    )

    summary = {
        "app_version": app_version,
        "enable_trading": settings.enable_trading,
        "binance_env": binance_env,
        "runtime_control_enabled": settings.runtime_control_enabled,
        "dashboard_protected": bool(
            settings.dashboard_enabled
            and settings.dashboard_require_token
            and dashboard_token_meta["configured"]
        ),
        "journal_protected": settings.protect_journal_api,
        "stats_protected": settings.protect_stats_api,
        "allowed_symbol_count": len(symbols),
        "allowed_symbols": symbols,
        "max_auto_leverage": settings.max_auto_leverage,
        "webhook_secret_configured": webhook_meta["configured"],
        "webhook_secret_length": webhook_meta["length"],
        "dashboard_token_configured": dashboard_token_meta["configured"],
        "dashboard_token_length": dashboard_token_meta["length"],
        "runtime_control_token_configured": runtime_token_meta["configured"],
        "runtime_control_token_length": runtime_token_meta["length"],
        "account_risk_enabled": settings.account_risk_enabled,
        "tv_signal_sandbox_enabled": settings.tv_signal_sandbox_enabled,
        "tv_signal_max_risk_usdt": settings.tv_signal_max_risk_usdt,
        "tv_signal_id_prefix": settings.tv_signal_id_prefix,
        "tv_alert_observation_enabled": settings.tv_alert_observation_enabled,
        "tv_alert_stale_minutes": settings.tv_alert_stale_minutes,
        "tv_alert_observation_window_hours": settings.tv_alert_observation_window_hours,
        "tv_cloud_audit_enabled": settings.tv_cloud_audit_enabled,
        "tv_cloud_audit_window_hours": settings.tv_cloud_audit_window_hours,
        "tv_cloud_duplicate_signal_warn": settings.tv_cloud_duplicate_signal_warn,
        "tv_cloud_payload_invalid_warn": settings.tv_cloud_payload_invalid_warn,
        "tv_cloud_unauthorized_warn": settings.tv_cloud_unauthorized_warn,
        "tv_cloud_runtime_lock_warn": settings.tv_cloud_runtime_lock_warn,
    }

    return {
        "level": _aggregate_health_level(checks),
        "checks": checks,
        "summary": summary,
    }


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
      --bg: #0b1016;
      --panel: #121820;
      --panel-2: #171f2a;
      --border: #263041;
      --text: #e8edf4;
      --muted: #8b98a8;
      --accent: #3b82f6;
      --info: #60a5fa;
      --ok: #22c55e;
      --ok-bg: rgba(34, 197, 94, 0.12);
      --warn: #f59e0b;
      --warn-bg: rgba(245, 158, 11, 0.12);
      --bad: #ef4444;
      --bad-bg: rgba(239, 68, 68, 0.12);
      --info-bg: rgba(96, 165, 250, 0.12);
      --dash-scroll-offset: 192px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.5;
    }}
    header {{
      padding: 0.85rem 1.25rem 0; border-bottom: 1px solid var(--border);
      background: linear-gradient(180deg, #151c26 0%, var(--panel) 100%);
      position: sticky; top: 0; z-index: 20;
      transition: box-shadow 0.2s ease, border-color 0.2s ease;
    }}
    header.is-scrolled {{
      box-shadow: 0 6px 24px rgba(0, 0, 0, 0.38);
      border-bottom-color: #2f3b4d;
    }}
    html {{ scroll-behavior: smooth; scroll-padding-top: var(--dash-scroll-offset); }}
    .header-row {{
      display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; justify-content: space-between;
    }}
    header h1 {{ margin: 0; font-size: 1.15rem; font-weight: 700; letter-spacing: -0.01em; }}
    .header-sub {{ display: flex; flex-wrap: wrap; gap: 0.45rem; align-items: center; margin-top: 0.35rem; }}
    .badge {{
      font-size: 0.72rem; color: var(--muted); border: 1px solid var(--border);
      padding: 0.18rem 0.55rem; border-radius: 999px; background: var(--panel-2);
    }}
    .badge.readonly {{ color: #93c5fd; border-color: rgba(96,165,250,0.35); background: var(--info-bg); }}
    .status-bar {{
      display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.75rem; padding-top: 0.75rem;
      border-top: 1px solid var(--border);
    }}
    .status-pill {{
      display: inline-flex; align-items: center; gap: 0.45rem; padding: 0.35rem 0.65rem;
      border-radius: 10px; border: 1px solid var(--border); background: var(--panel-2); font-size: 0.78rem;
    }}
    .status-pill .label {{ color: var(--muted); font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.04em; }}
    main {{ padding: 1rem 1.25rem 2.5rem; max-width: 1440px; width: 100%; margin: 0 auto; }}
    .cards {{
      display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 0.65rem; margin-bottom: 1rem;
    }}
    .card {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 0.75rem 0.85rem;
    }}
    .card .label {{ font-size: 0.72rem; color: var(--muted); margin-bottom: 0.2rem; }}
    .card .value {{ font-size: 1.25rem; font-weight: 700; line-height: 1.2; }}
    .card.highlight-ok {{ border-color: rgba(34,197,94,0.35); }}
    .card.highlight-warn {{ border-color: rgba(245,158,11,0.35); }}
    .card.highlight-error {{ border-color: rgba(239,68,68,0.35); }}
    section {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
      padding: 1rem 1.05rem; margin-bottom: 1rem;
    }}
    section h2 {{
      margin: 0 0 0.35rem; font-size: 1rem; font-weight: 700;
      display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
    }}
    .toolbar {{
      display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: end; margin-bottom: 0.75rem;
    }}
    label {{ display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.72rem; color: var(--muted); }}
    input, select, button {{
      background: #0b1016; color: var(--text); border: 1px solid var(--border);
      border-radius: 8px; padding: 0.45rem 0.6rem; font: inherit;
    }}
    button {{
      cursor: pointer; background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600;
    }}
    button.secondary {{ background: transparent; color: var(--text); border-color: var(--border); }}
    button:disabled {{ opacity: 0.55; cursor: not-allowed; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.84rem; }}
    th, td {{ border-bottom: 1px solid var(--border); padding: 0.5rem 0.55rem; text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; white-space: nowrap; background: var(--panel-2); font-size: 0.72rem; }}
    tbody tr:last-child td {{ border-bottom: none; }}
    tbody tr:hover {{ background: rgba(255,255,255,0.02); }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.78rem; }}
    .mono-wrap {{ word-break: break-all; }}
    .tech-field {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.78rem;
      display: inline-block;
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      vertical-align: bottom;
    }}
    td.col-tech {{ max-width: 10.5rem; }}
    td.col-tech-wide {{ max-width: 15rem; }}
    td.col-tech .tech-field, td.col-tech-wide .tech-field {{ display: block; max-width: 100%; }}
    td.col-status .lvl-badge {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      max-width: 10.5rem;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      vertical-align: bottom;
    }}
    .error-banner {{
      background: var(--bad-bg); border: 1px solid rgba(239,68,68,0.45); color: #fecaca;
      padding: 0.65rem 0.85rem; border-radius: 10px; margin-bottom: 1rem; display: none;
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
    .detail-meta {{
      display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 0.55rem; margin-bottom: 0.75rem;
    }}
    .detail-meta div, .kv-item {{
      font-size: 0.82rem; background: var(--panel-2); border: 1px solid var(--border);
      border-radius: 8px; padding: 0.45rem 0.55rem;
    }}
    .detail-meta span, .kv-item .kv-label {{ color: var(--muted); display: block; font-size: 0.68rem; margin-bottom: 0.15rem; }}
    .empty {{ color: var(--muted); font-size: 0.85rem; padding: 0.5rem 0; }}
    .section-note {{ margin: 0 0 0.85rem; font-size: 0.75rem; color: var(--muted); }}
    .subsection-title {{ margin: 1rem 0 0.5rem; font-size: 0.88rem; font-weight: 600; color: var(--muted); }}
    .lvl-badge {{
      display: inline-flex; align-items: center; justify-content: center;
      min-width: 3rem; padding: 0.12rem 0.5rem; border-radius: 999px;
      font-size: 0.68rem; font-weight: 700; letter-spacing: 0.04em; border: 1px solid transparent;
    }}
    .lvl-badge.ok {{ color: #4ade80; background: var(--ok-bg); border-color: rgba(34,197,94,0.35); }}
    .lvl-badge.warn {{ color: #fbbf24; background: var(--warn-bg); border-color: rgba(245,158,11,0.35); }}
    .lvl-badge.error {{ color: #f87171; background: var(--bad-bg); border-color: rgba(239,68,68,0.35); }}
    .lvl-badge.info {{ color: #93c5fd; background: var(--info-bg); border-color: rgba(96,165,250,0.35); }}
    .section-level {{
      font-size: 0.95rem; font-weight: 700; margin-bottom: 0.85rem; padding: 0.55rem 0.75rem;
      border-radius: 10px; border: 1px solid var(--border); background: var(--panel-2);
      display: flex; align-items: center; gap: 0.5rem;
    }}
    .section-level.ok {{ border-color: rgba(34,197,94,0.35); color: #86efac; }}
    .section-level.warn {{ border-color: rgba(245,158,11,0.35); color: #fcd34d; }}
    .section-level.error {{ border-color: rgba(239,68,68,0.35); color: #fca5a5; }}
    .overview-cards {{
      display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 0.55rem; margin-bottom: 0.85rem;
    }}
    .overview-card {{
      background: var(--panel-2); border: 1px solid var(--border); border-radius: 10px; padding: 0.55rem 0.65rem;
    }}
    .overview-card .k {{ font-size: 0.68rem; color: var(--muted); }}
    .overview-card .v {{ font-size: 0.92rem; font-weight: 600; margin-top: 0.15rem; }}
    .overview-card .v .tech-field {{ max-width: 100%; font-weight: 600; }}
    .kv-item .tech-field {{ max-width: min(100%, 22rem); }}
    .check-grid {{
      display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 0.55rem;
    }}
    .check-card {{
      background: var(--panel-2); border: 1px solid var(--border); border-radius: 10px;
      padding: 0.65rem 0.75rem; border-left: 3px solid var(--border);
    }}
    .check-card.ok {{ border-left-color: var(--ok); }}
    .check-card.warn {{ border-left-color: var(--warn); }}
    .check-card.error {{ border-left-color: var(--bad); }}
    .check-card.info {{ border-left-color: var(--info); }}
    .check-card .check-name {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 0.75rem; color: var(--muted); }}
    .check-card .check-msg {{ font-size: 0.82rem; margin-top: 0.35rem; line-height: 1.45; }}
    .check-card .check-head {{ display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; }}
    .alert-row-error {{ background: rgba(239,68,68,0.06); }}
    .alert-row-warn {{ background: rgba(245,158,11,0.05); }}
    .alert-row-info {{ background: rgba(96,165,250,0.05); }}
    .journal-row-fail td {{ background: rgba(239,68,68,0.04); }}
    .journal-row-warn td {{ background: rgba(245,158,11,0.04); }}
    .journal-row-ok td {{ background: rgba(34,197,94,0.03); }}
    .signal-id {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 0.76rem; color: #cbd5e1; }}
    .dash-nav {{
      display: flex; flex-wrap: nowrap; gap: 0.35rem; overflow-x: auto;
      padding: 0.55rem 0 0.7rem; margin-top: 0.55rem;
      border-top: 1px solid var(--border);
      -webkit-overflow-scrolling: touch; scrollbar-width: thin;
      transition: box-shadow 0.2s ease, border-color 0.2s ease;
    }}
    header.is-scrolled .dash-nav {{
      border-bottom: 1px solid rgba(38, 48, 65, 0.95);
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.22);
      margin-left: -1.25rem; margin-right: -1.25rem;
      padding-left: 1.25rem; padding-right: 1.25rem;
    }}
    .dash-nav::-webkit-scrollbar {{ height: 4px; }}
    .dash-nav::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}
    .dash-nav-item {{
      flex: 0 0 auto; display: inline-flex; align-items: center;
      padding: 0.38rem 0.72rem; border-radius: 999px; font-size: 0.78rem; font-weight: 600;
      color: var(--muted); text-decoration: none; border: 1px solid var(--border);
      background: var(--panel-2); cursor: pointer; transition: background 0.15s, color 0.15s, border-color 0.15s;
      white-space: nowrap;
    }}
    .dash-nav-item:hover {{ color: var(--text); border-color: #3b4a61; }}
    .dash-nav-item.active {{
      color: #fff; background: var(--accent); border-color: var(--accent);
    }}
    .dash-section {{
      scroll-margin-top: var(--dash-scroll-offset);
      margin-bottom: 1rem;
    }}
    .section-head {{
      display: flex; align-items: center; gap: 0.55rem; flex-wrap: wrap;
      margin-bottom: 0.25rem;
    }}
    .section-head h2 {{ margin: 0; font-size: 1rem; font-weight: 700; }}
    .section-badge:empty {{ display: none; }}
    .back-to-top {{
      position: fixed; right: 1.1rem; bottom: 1.1rem; z-index: 15;
      width: 2.5rem; height: 2.5rem; border-radius: 999px;
      background: var(--panel); border: 1px solid var(--border); color: var(--text);
      box-shadow: 0 4px 18px rgba(0,0,0,0.35); cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      font-size: 1rem; line-height: 1; opacity: 0; pointer-events: none;
      transition: opacity 0.2s, transform 0.2s, background 0.15s;
    }}
    .back-to-top.visible {{ opacity: 1; pointer-events: auto; }}
    .back-to-top:hover {{ background: var(--panel-2); transform: translateY(-2px); }}
  </style>
</head>
<body>
  <header>
    <div class="header-row">
      <div>
        <h1>Trade Journal Dashboard</h1>
        <div class="header-sub">
          <span class="badge readonly">只读 · 无下单能力</span>
          <span class="badge" id="refreshHint">自动刷新: {refresh}s</span>
        </div>
      </div>
    </div>
    <div class="status-bar" id="topStatusBar">
      <div class="status-pill"><span class="label">Runtime Lock</span><span id="sbRuntime">加载中...</span></div>
      <div class="status-pill"><span class="label">Binance</span><span id="sbBinance">加载中...</span></div>
      <div class="status-pill"><span class="label">TradingView</span><span id="sbTradingView">加载中...</span></div>
    </div>
    <nav class="dash-nav" id="dashNav" aria-label="Dashboard 区块导航">
      <a class="dash-nav-item active" href="#overview" data-section="overview">概览</a>
      <a class="dash-nav-item" href="#health" data-section="health">系统健康</a>
      <a class="dash-nav-item" href="#alerts" data-section="alerts">告警中心</a>
      <a class="dash-nav-item" href="#risk-config" data-section="risk-config">风控配置</a>
      <a class="dash-nav-item" href="#tv-sandbox" data-section="tv-sandbox">TradingView 沙盒</a>
      <a class="dash-nav-item" href="#tv-readiness" data-section="tv-readiness">TV 接入准备</a>
      <a class="dash-nav-item" href="#tv-observation" data-section="tv-observation">TV 连续观察</a>
      <a class="dash-nav-item" href="#tv-cloud-audit" data-section="tv-cloud-audit">TV 云端审计</a>
      <a class="dash-nav-item" href="#runtime-control" data-section="runtime-control">运行控制</a>
      <a class="dash-nav-item" href="#journal" data-section="journal">最近执行</a>
    </nav>
  </header>
  <main>
    <div id="errorBanner" class="error-banner"></div>

    <section id="overview" class="dash-section">
      <div class="section-head">
        <h2>概览</h2>
        <span class="section-badge" id="badge-overview"></span>
      </div>
      <p class="section-note">执行统计摘要 · 只读</p>
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
    </section>

    <section id="health" class="dash-section">
      <div class="section-head">
        <h2>系统健康摘要</h2>
        <span class="section-badge" id="badge-health"></span>
      </div>
      <p class="section-note">只读监控 · 不会自动下单、平仓、撤单或解锁</p>
      <div id="healthOverviewWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section id="alerts" class="dash-section">
      <div class="section-head">
        <h2>告警中心</h2>
        <span class="section-badge" id="badge-alerts"></span>
      </div>
      <p class="section-note">只读聚合 · 不会自动交易、撤单、平仓或解锁</p>
      <div id="alertCenterWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section id="risk-config" class="dash-section">
      <div class="section-head">
        <h2>风控配置体检</h2>
        <span class="section-badge" id="badge-risk-config"></span>
      </div>
      <p class="section-note">只读检查 · 不会修改 .env 或任何配置</p>
      <div id="riskConfigWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section id="tv-sandbox" class="dash-section">
      <div class="section-head">
        <h2>TradingView 沙盒</h2>
        <span class="section-badge" id="badge-tv-sandbox"></span>
      </div>
      <p class="section-note">只读展示 · 仅允许 demo/testnet 接入演练</p>
      <div id="tvSandboxWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section id="tv-readiness" class="dash-section">
      <div class="section-head">
        <h2>TradingView 接入准备</h2>
        <span class="section-badge" id="badge-tv-readiness"></span>
      </div>
      <p class="section-note">只读检查 · demo/testnet 接入前清单</p>
      <div id="tvAlertReadinessWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section id="tv-observation" class="dash-section">
      <div class="section-head">
        <h2>TradingView 连续观察</h2>
        <span class="section-badge" id="badge-tv-observation"></span>
      </div>
      <p class="section-note">只读统计 · 最近窗口内 TV 信号运行状态</p>
      <div id="tvObservationWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section id="tv-cloud-audit" class="dash-section">
      <div class="section-head">
        <h2>TV 云端 Alert 审计</h2>
        <span class="section-badge" id="badge-tv-cloud-audit"></span>
      </div>
      <p class="section-note">只读审计 · 窗口内 TV 云端信号安全统计（不修改执行逻辑）</p>
      <div id="tvCloudAuditWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section id="runtime-control" class="dash-section">
      <div class="section-head">
        <h2>运行控制</h2>
        <span class="section-badge" id="badge-runtime-control"></span>
      </div>
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
      <div class="section-head"><h2>运行状态</h2></div>
      <div class="detail-meta" id="runtimeMeta"></div>
      <div class="detail-meta" id="healthMeta" style="margin-top:0.5rem"></div>
    </section>

    <section>
      <div class="section-head"><h2>当前持仓</h2></div>
      <div style="overflow-x:auto" id="positionsWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section>
      <div class="section-head"><h2>当前条件单</h2></div>
      <div style="overflow-x:auto" id="algoOrdersWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section id="journal" class="dash-section">
      <div class="section-head">
        <h2>最近执行记录</h2>
        <span class="section-badge" id="badge-journal"></span>
      </div>
      <div class="toolbar">
        <label>交易对<input id="filterSymbol" placeholder="BTCUSDT" /></label>
        <label>状态
          <select id="filterStatus">
            <option value="">全部</option>
            <option value="protected">protected</option>
            <option value="entry_not_filled">entry_not_filled</option>
            <option value="blocked_by_account_risk">blocked_by_account_risk</option>
            <option value="blocked_by_runtime_lock">blocked_by_runtime_lock</option>
            <option value="tv_sandbox_rejected">tv_sandbox_rejected</option>
            <option value="skipped_by_position_policy">skipped_by_position_policy</option>
            <option value="protection_failed">protection_failed</option>
            <option value="failed">failed</option>
          </select>
        </label>
        <label>条数<input id="filterLimit" type="number" min="1" max="500" value="50" /></label>
        <button id="refreshBtn" type="button">刷新</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>编号</th><th>创建时间</th><th>交易对</th><th>方向</th><th>状态</th><th>状态说明</th>
              <th>跳过原因</th><th>计划数量</th><th>成交数量</th><th>进场价</th><th>杠杆</th>
              <th>signal_id</th><th>信号编号</th><th></th>
            </tr>
          </thead>
          <tbody id="executionsBody"><tr><td colspan="14" class="empty">加载中...</td></tr></tbody>
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

  <button type="button" class="back-to-top" id="backToTop" title="返回顶部" aria-label="返回顶部">↑</button>

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

    function techField(value) {{
      const raw = value === null || value === undefined || value === "" ? "-" : String(value);
      const safe = esc(raw);
      return `<span class="tech-field" title="${{safe}}">${{safe}}</span>`;
    }}

    function normLevel(level) {{
      const s = String(level || "OK").toUpperCase();
      if (s === "ERROR") return "error";
      if (s === "WARN") return "warn";
      if (s === "INFO") return "info";
      return "ok";
    }}

    function lvlBadge(level, text) {{
      const cls = normLevel(level);
      const label = text || String(level || "OK").toUpperCase();
      return `<span class="lvl-badge ${{cls}}" title="${{esc(label)}}">${{esc(label)}}</span>`;
    }}

    function statusBadgeForJournal(status) {{
      const s = String(status || "");
      if (s === "protected") return lvlBadge("OK", s);
      if (s === "failed" || s === "protection_failed") return lvlBadge("ERROR", s);
      if (s.startsWith("blocked_") || s === "tv_sandbox_rejected" || s === "entry_not_filled" || s === "skipped_by_position_policy") {{
        return lvlBadge("WARN", s);
      }}
      return lvlBadge("INFO", s || "-");
    }}

    function journalRowClass(status) {{
      const s = String(status || "");
      if (s === "protected") return "journal-row-ok";
      if (s === "failed" || s === "protection_failed") return "journal-row-fail";
      if (s) return "journal-row-warn";
      return "";
    }}

    function renderCheckGrid(checks) {{
      if (!checks || !checks.length) return '<div class="empty">无检查项</div>';
      return `<div class="check-grid">${{checks.map((c) => {{
        const cls = normLevel(c.level);
        return `<div class="check-card ${{cls}}">
          <div class="check-head">
            <span class="check-name">${{esc(c.name)}}</span>
            ${{lvlBadge(c.level)}}
          </div>
          <div class="check-msg">${{esc(c.message)}}</div>
        </div>`;
      }}).join("")}}</div>`;
    }}

    function renderOverviewCards(items) {{
      return `<div class="overview-cards">${{items.map((entry) => {{
        const k = entry[0];
        const v = entry[1];
        const useTech = entry[2];
        const valHtml = useTech ? techField(v) : esc(v);
        return `<div class="overview-card"><div class="k">${{esc(k)}}</div><div class="v">${{valHtml}}</div></div>`;
      }}).join("")}}</div>`;
    }}

    function renderKvGrid(items) {{
      const techKey = /signal_id|reason|status|symbol|prefix|action|skip|path|method|endpoint/i;
      return `<div class="detail-meta">${{items.map(([k, v]) => {{
        const raw = v === null || v === undefined || v === "" ? "-" : String(v);
        const valHtml = techKey.test(k) ? techField(raw) : esc(raw);
        return `<div class="kv-item"><span class="kv-label">${{esc(k)}}</span>${{valHtml}}</div>`;
      }}).join("")}}</div>`;
    }}

    const statusBarState = {{ runtime: null, health: null, tv: null, binanceUrl: null }};
    const NAV_SECTION_IDS = [
      "overview", "health", "alerts", "risk-config", "tv-sandbox",
      "tv-readiness", "tv-observation", "tv-cloud-audit", "runtime-control", "journal",
    ];
    let navClickLockUntil = 0;
    let scrollUiReady = false;

    function setSectionBadge(sectionKey, level, text) {{
      const el = document.getElementById("badge-" + sectionKey);
      if (!el) return;
      if (!level) {{
        el.innerHTML = "";
        return;
      }}
      el.innerHTML = lvlBadge(level, text || String(level).toUpperCase());
    }}

    function getSectionScrollOffset() {{
      const raw = getComputedStyle(document.documentElement).getPropertyValue("--dash-scroll-offset").trim();
      const parsed = parseInt(raw, 10);
      return Number.isFinite(parsed) && parsed > 0 ? parsed : 192;
    }}

    function getStickyHeaderOffset() {{
      const header = document.querySelector("header");
      return header ? header.offsetHeight + 12 : getSectionScrollOffset();
    }}

    function scrollToSection(id) {{
      const target = document.getElementById(id);
      if (!target) return;
      const offset = getSectionScrollOffset();
      const top = target.getBoundingClientRect().top + window.scrollY - offset;
      window.scrollTo({{ top: Math.max(0, top), behavior: "smooth" }});
    }}

    function resolveActiveSectionId() {{
      if (window.scrollY < 36) return "overview";
      const scrollLine = window.scrollY + getStickyHeaderOffset();
      let activeId = NAV_SECTION_IDS[0];
      for (const id of NAV_SECTION_IDS) {{
        const el = document.getElementById(id);
        if (!el) continue;
        const top = el.getBoundingClientRect().top + window.scrollY;
        if (top <= scrollLine) activeId = id;
      }}
      return activeId;
    }}

    function highlightNavSection(sectionId) {{
      document.querySelectorAll(".dash-nav-item").forEach((item) => {{
        item.classList.toggle("active", item.dataset.section === sectionId);
      }});
    }}

    function onDashboardScroll() {{
      const header = document.querySelector("header");
      if (header) header.classList.toggle("is-scrolled", window.scrollY > 12);
      const backBtn = document.getElementById("backToTop");
      if (backBtn) backBtn.classList.toggle("visible", window.scrollY > 420);
      if (Date.now() >= navClickLockUntil) {{
        highlightNavSection(resolveActiveSectionId());
      }}
    }}

    function setupSectionNav() {{
      highlightNavSection("overview");
      document.querySelectorAll(".dash-nav-item").forEach((item) => {{
        item.addEventListener("click", (ev) => {{
          ev.preventDefault();
          const id = item.dataset.section;
          navClickLockUntil = Date.now() + 900;
          highlightNavSection(id);
          scrollToSection(id);
        }});
      }});
    }}

    function setupScrollUi() {{
      if (scrollUiReady) return;
      scrollUiReady = true;
      window.addEventListener("scroll", onDashboardScroll, {{ passive: true }});
      onDashboardScroll();
    }}

    function setupBackToTop() {{
      const btn = document.getElementById("backToTop");
      if (!btn) return;
      btn.addEventListener("click", () => {{
        navClickLockUntil = Date.now() + 900;
        highlightNavSection("overview");
        window.scrollTo({{ top: 0, behavior: "smooth" }});
      }});
    }}

    function updateStatusBar() {{
      const rtEl = document.getElementById("sbRuntime");
      const bnEl = document.getElementById("sbBinance");
      const tvEl = document.getElementById("sbTradingView");
      const rt = statusBarState.runtime;
      const health = statusBarState.health;
      const tv = statusBarState.tv;
      if (rt && rt.enabled) {{
        const effLocked = rt.effective_locked !== undefined ? rt.effective_locked : rt.locked;
        rtEl.innerHTML = effLocked ? lvlBadge("WARN", "已锁定") : lvlBadge("OK", "未锁定");
      }} else if (rt && !rt.enabled) {{
        rtEl.innerHTML = lvlBadge("INFO", "未启用");
      }}
      if (statusBarState.binanceUrl) {{
        const url = String(statusBarState.binanceUrl).toLowerCase();
        let env = "unknown";
        let level = "WARN";
        if (url.includes("demo-fapi") || url.includes("testnet")) {{
          env = "demo/testnet";
          level = "OK";
        }} else if (url.includes("fapi.binance.com")) {{
          env = "live";
          level = "ERROR";
        }}
        if (health && health.summary && health.summary.binance_ok === false) {{
          env = "连接失败";
          level = "ERROR";
        }}
        bnEl.innerHTML = lvlBadge(level, env);
      }} else if (health && health.summary) {{
        bnEl.innerHTML = health.summary.binance_ok ? lvlBadge("OK", "正常") : lvlBadge("ERROR", "异常");
      }}
      if (tv) {{
        tvEl.innerHTML = lvlBadge(tv.level || "OK", "接入 " + (tv.level || "OK"));
      }}
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
      const failed = Number(stats.failed_count || 0);
      const protFailed = Number(stats.protection_failed_count || 0);
      if (failed + protFailed > 0) {{
        setSectionBadge("overview", "WARN", "有异常");
      }} else {{
        setSectionBadge("overview", "OK", "正常");
      }}
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
          <td class="col-tech">${{techField(row.symbol)}}</td>
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
          <td class="col-tech">${{techField(row.symbol)}}</td>
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
        body.innerHTML = '<tr><td colspan="14" class="empty">暂无记录</td></tr>';
        return;
      }}
      body.innerHTML = rows.map((row) => {{
        const status = row["状态"] || "";
        const signalId = row["信号ID"] || "-";
        const rowCls = journalRowClass(status);
        return `<tr class="${{esc(rowCls)}}">
          <td>${{esc(row["编号"])}}</td>
          <td class="mono">${{esc(row["创建时间"])}}</td>
          <td class="col-tech">${{techField(row["交易对"])}}</td>
          <td>${{esc(row["方向"])}}</td>
          <td class="col-status">${{statusBadgeForJournal(status)}}</td>
          <td>${{esc(row["状态说明"])}}</td>
          <td class="col-tech-wide">${{techField(row["跳过原因"] || "-")}}</td>
          <td class="mono">${{esc(row["计划数量"] || "-")}}</td>
          <td class="mono">${{esc(row["成交数量"] || "-")}}</td>
          <td class="mono">${{esc(row["进场价"] || "-")}}</td>
          <td>${{esc(row["杠杆"] || "-")}}</td>
          <td class="col-tech-wide">${{techField(signalId)}}</td>
          <td class="col-tech-wide">${{techField(row["信号编号"])}}</td>
          <td><button class="secondary" type="button" data-id="${{esc(row["编号"])}}">详情</button></td>
        </tr>`;
      }}).join("");
      body.querySelectorAll("button[data-id]").forEach((btn) => {{
        btn.addEventListener("click", () => openDetail(btn.getAttribute("data-id")));
      }});
      const hasFail = rows.some((r) => ["failed", "protection_failed"].includes(r["状态"]));
      const hasWarn = rows.some((r) =>
        ["blocked_by_runtime_lock", "tv_sandbox_rejected", "blocked_by_account_risk", "entry_not_filled"].includes(r["状态"])
      );
      if (hasFail) setSectionBadge("journal", "ERROR", "有失败");
      else if (hasWarn) setSectionBadge("journal", "WARN", "有拒绝");
      else if (rows.length) setSectionBadge("journal", "OK", "正常");
      else setSectionBadge("journal", "INFO", "无记录");
    }}

    function renderBySymbol(rows) {{
      const body = document.getElementById("bySymbolBody");
      if (!rows || rows.length === 0) {{
        body.innerHTML = '<tr><td colspan="6" class="empty">暂无数据</td></tr>';
        return;
      }}
      body.innerHTML = rows.map((row) => `<tr>
        <td class="col-tech">${{techField(row.symbol)}}</td>
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
        <td class="col-tech-wide">${{techField(row.reason)}}</td>
        <td class="col-tech">${{techField(row.status)}}</td>
        <td>${{esc(row.count)}}</td>
      </tr>`).join("");
    }}

    function renderRuntimeControlStatus(data) {{
      const el = document.getElementById("runtimeControlStatus");
      if (!data) {{
        el.innerHTML = '<div class="empty">暂无运行控制数据</div>';
        setSectionBadge("runtime-control", "INFO", "未知");
        return;
      }}
      if (!data.enabled) {{
        el.innerHTML = '<div class="empty">Runtime Control 未启用</div>';
        setSectionBadge("runtime-control", "WARN", "未启用");
        return;
      }}
      const locked = data.effective_locked !== undefined ? !!data.effective_locked : !!data.locked;
      const lockLabel = locked ? "已锁定" : "未锁定";
      const oneShot = data.one_shot || {{}};
      const fields = [
        ["Runtime Control", "启用"],
        ["有效锁定", locked ? "是" : "否"],
        ["锁定原因", data.reason],
        ["锁定人", data.locked_by],
        ["锁定时间", data.locked_at],
        ["自动解锁时间", data.locked_until],
        ["One-Shot 启用", oneShot.enabled ? "是" : "否"],
        ["One-Shot 剩余", oneShot.remaining ?? "-"],
        ["One-Shot 原因", oneShot.reason],
        ["One-Shot 操作人", oneShot.operator],
        ["One-Shot 开始", oneShot.started_at],
        ["One-Shot 过期", oneShot.expires_at],
        ["One-Shot 消费 signal_id", oneShot.consumed_by_signal_id],
        ["One-Shot 消费时间", oneShot.consumed_at],
        ["更新时间", data.updated_at],
      ];
      let html = `<div class="kv-item"><span class="kv-label">当前状态</span>${{locked ? lvlBadge("WARN", lockLabel) : lvlBadge("OK", lockLabel)}}</div>`;
      html += fields.map(([k, v]) => {{
        const display = (v === null || v === undefined || v === "") ? "-" : v;
        const valHtml = k === "锁定原因" ? techField(display) : esc(display);
        return `<div class="kv-item"><span class="kv-label">${{esc(k)}}</span>${{valHtml}}</div>`;
      }}).join("");
      el.innerHTML = html;
      setSectionBadge(
        "runtime-control",
        oneShot.enabled && (oneShot.remaining || 0) > 0 ? "WARN" : (locked ? "WARN" : "OK"),
        oneShot.enabled && (oneShot.remaining || 0) > 0 ? "One-Shot" : (locked ? "已锁定" : "未锁定")
      );
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
          <td class="col-tech">${{techField(row.action)}}</td>
          <td class="col-tech-wide">${{techField(row.reason)}}</td>
          <td>${{esc(row.actor)}}</td>
          <td class="mono">${{esc(row.locked_until)}}</td>
        </tr>`).join("")}}</tbody>
      </table>`;
    }}

    async function loadTvSandboxSection() {{
      const wrap = document.getElementById("tvSandboxWrap");
      try {{
        const resp = await apiFetch("/dashboard/api/tv-sandbox/status");
        renderTvSandbox(resp["TV沙盒"] || null);
      }} catch (err) {{
        wrap.innerHTML = `<div class="empty">${{esc(err.message || String(err))}}</div>`;
      }}
    }}

    function renderTvSandbox(data) {{
      const wrap = document.getElementById("tvSandboxWrap");
      if (!data) {{
        wrap.innerHTML = '<div class="empty">暂无 TV 沙盒数据</div>';
        return;
      }}
      const envLabel = {{
        demo: "demo/testnet",
        live: "live",
        unknown: "unknown",
      }}[data.binance_env] || data.binance_env || "-";
      const last = data.last_tv_execution;
      const lastHtml = last ? `<h3 class="subsection-title">最近 TV 信号</h3>
        <div class="detail-meta">
          <div class="kv-item"><span class="kv-label">编号</span>${{esc(last.id)}}</div>
          <div class="kv-item"><span class="kv-label">signal_id</span>${{techField(last.signal_id || "-")}}</div>
          <div class="kv-item"><span class="kv-label">交易对</span>${{techField(last.symbol || "-")}}</div>
          <div class="kv-item"><span class="kv-label">状态</span>${{statusBadgeForJournal(last.status || "")}}</div>
          <div class="kv-item"><span class="kv-label">跳过原因</span>${{techField(last.skip_reason || "-")}}</div>
          <div class="kv-item"><span class="kv-label">时间</span><span class="mono">${{esc(last.created_at || "-")}}</span></div>
        </div>` : '<div class="empty" style="margin-top:0.75rem">暂无 TV 信号执行记录</div>';
      wrap.innerHTML = `${{renderKvGrid([
        ["沙盒启用", data.enabled ? "是" : "否"],
        ["Binance 环境", envLabel],
        ["拒绝实盘 endpoint", data.reject_live_binance ? "是" : "否"],
        ["允许 source", (data.allowed_sources || []).join(", ") || "-"],
        ["signal_id 前缀", data.signal_id_prefix || "-"],
        ["最大 risk_usdt", data.max_risk_usdt ?? "-"],
        ["最大 margin_usdt", data.max_margin_usdt ?? "-"],
        ["允许 entry_type", (data.allowed_entry_types || []).join(", ") || "-"],
        ["要求 source", data.require_source ? "是" : "否"],
      ])}}${{lastHtml}}`;
      const env = data.binance_env || "unknown";
      if (!data.enabled) setSectionBadge("tv-sandbox", "WARN", "未启用");
      else if (env === "live") setSectionBadge("tv-sandbox", "ERROR", "live");
      else setSectionBadge("tv-sandbox", "OK", "demo");
    }}

    async function loadTvAlertReadinessSection() {{
      const wrap = document.getElementById("tvAlertReadinessWrap");
      try {{
        const resp = await apiFetch("/dashboard/api/tv-alert-readiness");
        statusBarState.tv = resp["TV接入检查"] || null;
        updateStatusBar();
        renderTvCheckBlock(wrap, resp["TV接入检查"] || null, "接入准备");
      }} catch (err) {{
        wrap.innerHTML = `<div class="empty">${{esc(err.message || String(err))}}</div>`;
      }}
    }}

    async function loadTvObservationSection() {{
      const wrap = document.getElementById("tvObservationWrap");
      try {{
        const hours = 24;
        const resp = await apiFetch("/dashboard/api/tv-observation?hours=" + hours);
        renderTvObservation(wrap, resp["TV观察"] || null);
      }} catch (err) {{
        wrap.innerHTML = `<div class="empty">${{esc(err.message || String(err))}}</div>`;
      }}
    }}

    function renderTvCheckBlock(wrap, data, titlePrefix) {{
      if (!data) {{
        wrap.innerHTML = '<div class="empty">暂无数据</div>';
        return;
      }}
      const level = normLevel(data.level || "OK");
      const summary = data.summary || {{}};
      const checks = data.checks || [];
      const summaryLabels = {{
        app_version: "应用版本",
        required_method: "请求方法",
        required_path: "Webhook 路径",
        binance_env: "Binance 环境",
        tv_sandbox_enabled: "TV 沙盒",
        tv_observation_enabled: "TV 观察",
        webhook_secret_configured: "Webhook 已配置",
        webhook_secret_length: "Webhook 长度",
        public_base_url_configured: "公网 URL 已配置",
        webhook_url_hint: "Webhook URL 提示",
        enable_trading: "允许真实下单",
        runtime_control_enabled: "Runtime Control",
        reject_live_binance: "拒绝实盘",
        expected_symbols: "预期交易对",
        allowed_symbols: "允许交易对",
        webhook_enabled: "Webhook 已启用",
        require_position_strategy: "要求 position_strategy",
        reject_expired: "拒绝过期信号",
        max_age_seconds: "信号最大年龄(秒)",
        last_tv_signal_time: "最近 TV 信号时间",
        last_tv_signal_status: "最近 TV 信号状态",
        last_tv_signal_skip_reason: "最近跳过原因",
        recent_duplicate_count: "24h 重复信号",
        recent_rejected_count: "24h 拒绝数",
        recent_blocked_count: "24h Runtime 拦截",
        recent_expired_count: "24h 过期信号",
      }};
      const summaryItems = Object.entries(summary).map(([k, v]) => {{
        const label = summaryLabels[k] || k;
        let display = v;
        if (Array.isArray(v)) display = v.join(", ");
        if (typeof v === "boolean") display = v ? "是" : "否";
        if (v === null || v === undefined) display = "-";
        if (k === "webhook_url_hint" && v) display = String(v);
        return [label, display];
      }});
      wrap.innerHTML = `
        <div class="section-level ${{level}}">${{lvlBadge(data.level)}} ${{esc(titlePrefix)}}检查</div>
        ${{renderOverviewCards(summaryItems.slice(0, 8))}}
        ${{renderKvGrid(summaryItems.slice(8))}}
        <h3 class="subsection-title">检查项</h3>
        ${{renderCheckGrid(checks)}}
      `;
      setSectionBadge("tv-readiness", data.level);
    }}

    function renderTvObservation(wrap, data) {{
      if (!data) {{
        wrap.innerHTML = '<div class="empty">暂无观察数据</div>';
        return;
      }}
      const level = normLevel(data.level || "OK");
      const summary = data.summary || {{}};
      const checks = data.checks || [];
      wrap.innerHTML = `
        <div class="section-level ${{level}}">${{lvlBadge(data.level)}} 连续观察 · ${{esc(data.window_hours)}}h 窗口</div>
        ${{renderOverviewCards([
          ["TV 信号总数", summary.total_tv_signals ?? 0],
          ["protected", summary.protected_count ?? 0],
          ["failed", summary.failed_count ?? 0],
          ["sandbox 拒绝", summary.tv_sandbox_rejected_count ?? 0],
          ["runtime lock", summary.blocked_by_runtime_lock_count ?? 0],
          ["连续失败", summary.consecutive_failures ?? 0],
          ["持仓数", summary.open_position_count ?? 0],
          ["未保护持仓", summary.unprotected_position_count ?? 0],
        ])}}
        <div class="detail-meta">
          <div class="kv-item"><span class="kv-label">最近 TV 信号时间</span><span class="mono">${{esc(summary.last_tv_signal_time || "-")}}</span></div>
          <div class="kv-item"><span class="kv-label">最近 TV 状态</span>${{statusBadgeForJournal(summary.last_tv_signal_status || "")}}</div>
          <div class="kv-item"><span class="kv-label">最近 signal_id</span>${{techField(summary.last_tv_signal_id || "-")}}</div>
          <div class="kv-item"><span class="kv-label">最近交易对</span>${{techField(summary.last_tv_signal_symbol || "-")}}</div>
        </div>
        <h3 class="subsection-title">观察检查</h3>
        ${{renderCheckGrid(checks)}}
      `;
      setSectionBadge("tv-observation", data.level);
    }}

    async function loadTvCloudAuditSection() {{
      const wrap = document.getElementById("tvCloudAuditWrap");
      try {{
        const resp = await apiFetch("/dashboard/api/tv-cloud-alerts");
        renderTvCloudAudit(wrap, resp["TV云端Alert审计"] || null);
      }} catch (err) {{
        wrap.innerHTML = `<div class="empty">${{esc(err.message || String(err))}}</div>`;
      }}
    }}

    function renderTvCloudAudit(wrap, data) {{
      if (!data) {{
        wrap.innerHTML = '<div class="empty">暂无云端审计数据</div>';
        return;
      }}
      const level = normLevel(data.level || "OK");
      const summary = data.summary || {{}};
      const checks = data.checks || [];
      const recent = data.recent || [];
      const recentTable = recent.length ? `<div class="table-wrap"><table>
        <thead><tr>
          <th>时间</th><th>signal_id</th><th>交易对</th><th>状态</th><th>跳过原因</th>
        </tr></thead>
        <tbody>${{recent.map((row) => `<tr>
          <td class="mono">${{esc(row.created_at || "-")}}</td>
          <td class="col-tech-wide">${{techField(row.signal_id || "-")}}</td>
          <td class="col-tech">${{techField(row.symbol || "-")}}</td>
          <td class="col-status">${{statusBadgeForJournal(row.status || "")}}</td>
          <td class="col-tech-wide">${{techField(row.skip_reason || "-")}}</td>
        </tr>`).join("")}}</tbody>
      </table></div>` : '<div class="empty">窗口内暂无 TV 云端信号记录</div>';
      wrap.innerHTML = `
        <div class="section-level ${{level}}">${{lvlBadge(data.level)}} 云端 Alert 审计 · ${{esc(data.window_hours)}}h 窗口</div>
        ${{renderOverviewCards([
          ["云端信号总数", summary.total_cloud_signals ?? 0],
          ["重复 signal_id", summary.duplicate_signal_count ?? 0],
          ["payload 无效", summary.payload_invalid_count ?? 0],
          ["Runtime Lock", summary.runtime_locked_count ?? 0],
          ["未授权 401", summary.unauthorized_count ?? 0],
          ["protected", summary.protected_count ?? 0],
          ["failed", summary.failed_count ?? 0],
        ])}}
        <div class="detail-meta">
          <div class="kv-item"><span class="kv-label">最近 signal_id</span>${{techField(summary.last_signal_id || "-")}}</div>
          <div class="kv-item"><span class="kv-label">最近状态</span>${{statusBadgeForJournal(summary.last_status || "")}}</div>
          <div class="kv-item"><span class="kv-label">最近交易对</span>${{techField(summary.last_symbol || "-")}}</div>
          <div class="kv-item"><span class="kv-label">最近时间</span><span class="mono">${{esc(summary.last_time || "-")}}</span></div>
        </div>
        <h3 class="subsection-title">审计检查</h3>
        ${{renderCheckGrid(checks)}}
        <h3 class="subsection-title">最近云端信号</h3>
        ${{recentTable}}
      `;
      setSectionBadge("tv-cloud-audit", data.level);
    }}

    async function loadRiskConfigSection() {{
      const wrap = document.getElementById("riskConfigWrap");
      try {{
        const resp = await apiFetch("/dashboard/api/risk-config");
        renderRiskConfig(resp["配置体检"] || null);
      }} catch (err) {{
        wrap.innerHTML = `<div class="empty">${{esc(err.message || String(err))}}</div>`;
      }}
    }}

    function renderRiskConfig(data) {{
      const wrap = document.getElementById("riskConfigWrap");
      if (!data) {{
        wrap.innerHTML = '<div class="empty">暂无配置体检数据</div>';
        return;
      }}
      const level = normLevel(data.level || "OK");
      const summary = data.summary || {{}};
      const checks = data.checks || [];
      const envLabel = {{
        demo: "demo/testnet",
        live: "live",
        unknown: "unknown",
      }}[summary.binance_env] || summary.binance_env || "-";
      wrap.innerHTML = `
        <div class="section-level ${{level}}">${{lvlBadge(data.level)}} 总体等级</div>
        ${{renderOverviewCards([
          ["当前环境", envLabel],
          ["允许真实下单", summary.enable_trading ? "是" : "否"],
          ["Runtime Control", summary.runtime_control_enabled ? "已启用" : "未启用"],
          ["Dashboard 保护", summary.dashboard_protected ? "是" : "否"],
          ["Journal 保护", summary.journal_protected ? "是" : "否"],
          ["Stats 保护", summary.stats_protected ? "是" : "否"],
          ["允许交易对数", summary.allowed_symbol_count ?? "-"],
          ["最大杠杆", summary.max_auto_leverage ?? "-"],
        ])}}
        ${{renderKvGrid([
          ["Webhook 已配置", summary.webhook_secret_configured ? "是" : "否"],
          ["Dashboard Token 已配置", summary.dashboard_token_configured ? "是" : "否"],
        ])}}
        <h3 class="subsection-title">风控提示</h3>
        ${{renderCheckGrid(checks)}}
      `;
      setSectionBadge("risk-config", data.level);
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
      const level = normLevel(summary.latest_level || "OK");
      const cardClass = (k) => k === "ERROR" ? "highlight-error" : k === "WARN" ? "highlight-warn" : "";
      const summaryCards = `
        <div class="cards" style="margin-bottom:0.85rem">
          <div class="card ${{cardClass("ERROR")}}"><div class="label">ERROR</div><div class="value">${{esc(summary.error_count ?? 0)}}</div></div>
          <div class="card ${{cardClass("WARN")}}"><div class="label">WARN</div><div class="value">${{esc(summary.warn_count ?? 0)}}</div></div>
          <div class="card"><div class="label">INFO</div><div class="value">${{esc(summary.info_count ?? 0)}}</div></div>
          <div class="card"><div class="label">最新等级</div><div class="value">${{lvlBadge(summary.latest_level || "OK")}}</div></div>
        </div>`;
      if (!alerts.length) {{
        wrap.innerHTML = summaryCards + '<div class="empty">当前无 WARN/ERROR/INFO 告警</div>';
        return;
      }}
      wrap.innerHTML = summaryCards + `<div class="table-wrap"><table>
        <thead><tr>
          <th>时间</th><th>等级</th><th>来源</th><th>类型</th><th>标题</th><th>说明</th>
        </tr></thead>
        <tbody>${{alerts.map((a) => {{
          const rowCls = "alert-row-" + normLevel(a.level);
          return `<tr class="${{esc(rowCls)}}">
          <td class="mono">${{esc(a.created_at || "-")}}</td>
          <td>${{lvlBadge(a.level)}}</td>
          <td>${{esc(a.source)}}</td>
          <td class="col-tech-wide">${{techField(a.type)}}</td>
          <td>${{esc(a.title)}}</td>
          <td class="col-tech-wide">${{techField(a.message)}}</td>
        </tr>`;
        }}).join("")}}</tbody>
      </table></div>`;
      setSectionBadge("alerts", summary.latest_level || "OK");
    }}

    async function loadHealthOverviewSection() {{
      const wrap = document.getElementById("healthOverviewWrap");
      try {{
        const resp = await apiFetch("/dashboard/api/health-overview");
        statusBarState.health = resp["健康摘要"] || null;
        updateStatusBar();
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
      const level = normLevel(data.level || "OK");
      const summary = data.summary || {{}};
      const checks = data.checks || [];
      wrap.innerHTML = `
        <div class="section-level ${{level}}">${{lvlBadge(data.level)}} 系统健康</div>
        ${{renderOverviewCards([
          ["允许真实下单", summary.enable_trading ? "是" : "否"],
          ["Binance 状态", summary.binance_ok ? "正常" : "异常"],
          ["Runtime 锁定", summary.runtime_locked ? "是" : "否"],
          ["当前持仓数", summary.open_position_count ?? "-"],
          ["条件单数", summary.algo_order_count ?? "-"],
          ["安全审计", summary.reconcile_level || "-"],
          ["未保护持仓", summary.reconcile_unprotected_position_count ?? "-"],
          ["残留委托交易对", summary.reconcile_residual_order_symbol_count ?? "-"],
          ["最近执行数", summary.recent_execution_count ?? "-"],
          ["最近执行状态", summary.last_execution_status || "-", true],
          ["最近拒绝原因", summary.last_rejection_reason || "-", true],
        ])}}
        <h3 class="subsection-title">风险提示</h3>
        ${{renderCheckGrid(checks)}}
      `;
      setSectionBadge("health", data.level);
    }}

    async function loadRuntimeControlSection() {{
      const statusEl = document.getElementById("runtimeControlStatus");
      try {{
        const statusResp = await apiFetch("/dashboard/api/runtime-control/status");
        statusBarState.runtime = statusResp["运行控制"] || null;
        updateStatusBar();
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
        statusBarState.binanceUrl = ((runtimeResp["运行配置"] || {{}}).binance_base_url) || null;
        updateStatusBar();
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
      await loadRiskConfigSection();
      await loadTvSandboxSection();
      await loadTvAlertReadinessSection();
      await loadTvObservationSection();
      await loadTvCloudAuditSection();
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
          ["signal_id", record["信号ID"]],
          ["信号编号", record["信号编号"]],
          ["创建时间", record["创建时间"]],
        ].map(([k, v]) => {{
          const techKeys = new Set(["交易对", "状态", "跳过原因", "signal_id", "信号编号"]);
          const inner = techKeys.has(k) ? techField(v ?? "-") : esc(v ?? "-");
          return `<div class="kv-item"><span class="kv-label">${{esc(k)}}</span>${{inner}}</div>`;
        }}).join("");
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
    setupSectionNav();
    setupScrollUi();
    setupBackToTop();
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
    reconcile_service: SafetyReconcileService | None = None,
    trader: Trader | None = None,
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
        from .reconcile import merge_reconcile_into_health_overview

        try:
            overview = build_health_overview(
                settings, client, journal_store, trade_stats, runtime_control
            )
            if reconcile_service is not None:
                overview = merge_reconcile_into_health_overview(
                    overview, reconcile_service.get_latest_report()
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

    @router.get("/api/reconcile")
    async def api_reconcile(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        if reconcile_service is None:
            return JSONResponse(
                content={
                    "成功": False,
                    "level": "WARN",
                    "提示": "安全审计服务未启用",
                    "报告": None,
                }
            )
        report = reconcile_service.get_latest_report()
        if report is None:
            return JSONResponse(
                content={
                    "成功": False,
                    "level": "WARN",
                    "提示": "尚未生成安全审计报告",
                    "报告": None,
                }
            )
        return JSONResponse(content={"成功": True, "报告": report})

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
                reconcile_service=reconcile_service,
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

    @router.get("/api/risk-config")
    async def api_risk_config(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        try:
            inspector = build_risk_config_inspector(settings, app_version)
        except Exception as exc:
            return JSONResponse(
                content={
                    "成功": False,
                    "错误": str(exc)[:500],
                    "配置体检": {
                        "level": "ERROR",
                        "checks": [
                            {
                                "name": "risk_config_inspector",
                                "level": "ERROR",
                                "message": f"配置体检失败: {str(exc)[:200]}",
                            }
                        ],
                        "summary": {},
                    },
                },
                status_code=200,
            )
        return JSONResponse(content={"成功": True, "配置体检": inspector})

    @router.get("/api/tv-sandbox/status")
    async def api_tv_sandbox_status(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        try:
            status_payload = build_tv_sandbox_status(settings, journal_store)
        except Exception as exc:
            return JSONResponse(
                content={
                    "成功": False,
                    "错误": str(exc)[:500],
                    "TV沙盒": {},
                },
                status_code=200,
            )
        return JSONResponse(content={"成功": True, "TV沙盒": status_payload})

    @router.get("/api/tv-alert-readiness")
    async def api_tv_alert_readiness(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        try:
            readiness = build_tv_alert_readiness(settings, app_version, journal_store)
        except Exception as exc:
            return JSONResponse(
                content={
                    "成功": False,
                    "错误": str(exc)[:500],
                    "TV接入检查": {"level": "ERROR", "checks": [], "summary": {}},
                },
                status_code=200,
            )
        return JSONResponse(content={"成功": True, "TV接入检查": readiness})

    @router.get("/api/tv-observation")
    async def api_tv_observation(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
        hours: int = Query(default=24, ge=1, le=168),
    ):
        guard(request, token, x_dashboard_token)
        try:
            observation = build_tv_observation(
                settings,
                journal_store,
                client,
                runtime_control,
                hours=hours,
            )
        except Exception as exc:
            return JSONResponse(
                content={
                    "成功": False,
                    "错误": str(exc)[:500],
                    "TV观察": {
                        "level": "ERROR",
                        "window_hours": hours,
                        "summary": {},
                        "checks": [],
                    },
                },
                status_code=200,
            )
        return JSONResponse(content={"成功": True, "TV观察": observation})

    @router.get("/api/tv-cloud-alerts")
    async def api_tv_cloud_alerts(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
        hours: int | None = Query(default=None, ge=1, le=168),
    ):
        guard(request, token, x_dashboard_token)
        try:
            audit = build_tv_cloud_audit(
                settings,
                journal_store,
                hours=hours,
            )
        except Exception as exc:
            window = hours if hours is not None else settings.tv_cloud_audit_window_hours
            return JSONResponse(
                content={
                    "成功": False,
                    "错误": str(exc)[:500],
                    "TV云端Alert审计": {
                        "level": "ERROR",
                        "window_hours": window,
                        "summary": {},
                        "checks": [],
                        "recent": [],
                    },
                },
                status_code=200,
            )
        return JSONResponse(content={"成功": True, "TV云端Alert审计": audit})

    if trader is not None:
        from .dashboard_runtime_control import register_runtime_control_dashboard_routes

        register_runtime_control_dashboard_routes(
            router,
            settings,
            journal_store,
            client,
            trader,
            runtime_control,
            reconcile_service,
        )

    return router

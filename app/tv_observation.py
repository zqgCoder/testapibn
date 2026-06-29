from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .tv_sandbox import binance_env_label, is_tv_execution_row

if TYPE_CHECKING:
    from .binance_client import BinanceClient
    from .config import Settings
    from .runtime_control import RuntimeControl
    from .storage import TradeJournalStore


def _health_level_rank(level: str) -> int:
    return {"OK": 0, "WARN": 1, "ERROR": 2}.get(level, 0)


def aggregate_level(checks: list[dict[str, str]]) -> str:
    max_rank = 0
    result = "OK"
    for check in checks:
        level = check.get("level", "OK")
        rank = _health_level_rank(level)
        if rank > max_rank:
            max_rank = rank
            result = level
    return result


def _secret_meta(value: str) -> dict[str, bool | int]:
    stripped = (value or "").strip()
    return {"configured": bool(stripped), "length": len(stripped)}


def _webhook_url_hint(settings: Settings) -> str | None:
    base = (settings.tv_alert_public_base_url or "").strip().rstrip("/")
    if not base:
        return None
    return f"{base}/tradingview"


def _parse_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _is_valid_public_base_url(url: str) -> bool:
    text = (url or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


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


def _count_unprotected_positions(
    positions: list[dict[str, Any]],
    algo_orders: list[dict[str, Any]],
) -> tuple[int, list[str]]:
    if not positions:
        return 0, []
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
    return len(unprotected), unprotected


def _consecutive_tv_failures(tv_rows: list[dict[str, Any]]) -> int:
    failure_statuses = {"failed", "protection_failed", "tv_sandbox_rejected", "entry_not_filled"}
    streak = 0
    for row in tv_rows:
        status = str(row.get("status") or "")
        if status == "protected":
            break
        if status in failure_statuses:
            streak += 1
        else:
            break
    return streak


def build_tv_alert_readiness(
    settings: Settings,
    app_version: str = "",
    journal_store: TradeJournalStore | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    binance_env = binance_env_label(settings.binance_base_url)
    webhook_meta = _secret_meta(settings.webhook_secret)
    public_url = (settings.tv_alert_public_base_url or "").strip()
    public_configured = _is_valid_public_base_url(public_url)
    webhook_hint = _webhook_url_hint(settings)

    summary: dict[str, Any] = {
        "app_version": app_version,
        "required_method": "POST",
        "required_path": "/tradingview",
        "webhook_enabled": True,
        "binance_env": binance_env,
        "tv_sandbox_enabled": settings.tv_signal_sandbox_enabled,
        "tv_observation_enabled": settings.tv_alert_observation_enabled,
        "webhook_secret_configured": webhook_meta["configured"],
        "webhook_secret_length": webhook_meta["length"],
        "public_base_url_configured": public_configured,
        "webhook_url_hint": webhook_hint,
        "enable_trading": settings.enable_trading,
        "runtime_control_enabled": settings.runtime_control_enabled,
        "reject_live_binance": settings.tv_signal_reject_live_binance,
        "expected_symbols": sorted(settings.tv_alert_expected_symbol_set),
        "allowed_symbols": sorted(settings.allowed_symbol_set),
        "require_position_strategy": settings.tv_signal_require_position_strategy,
        "reject_expired": settings.tv_signal_reject_expired,
        "max_age_seconds": settings.tv_signal_max_age_seconds,
        "last_tv_signal_time": None,
        "last_tv_signal_status": None,
        "last_tv_signal_skip_reason": None,
        "recent_duplicate_count": 0,
        "recent_rejected_count": 0,
        "recent_blocked_count": 0,
        "recent_expired_count": 0,
    }

    if journal_store is not None:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        rows = journal_store.list_executions_since(since, limit=500)
        tv_rows = [row for row in rows if is_tv_execution_row(row, settings)]
        for row in tv_rows:
            skip = str(row.get("skip_reason") or "")
            status = str(row.get("status") or "")
            if skip == "duplicate_signal":
                summary["recent_duplicate_count"] += 1
            if status == "tv_sandbox_rejected":
                summary["recent_rejected_count"] += 1
            if status == "blocked_by_runtime_lock":
                summary["recent_blocked_count"] += 1
            if skip == "signal_expired":
                summary["recent_expired_count"] += 1
        if tv_rows:
            latest = tv_rows[0]
            summary["last_tv_signal_time"] = latest.get("created_at")
            summary["last_tv_signal_status"] = latest.get("status")
            summary["last_tv_signal_skip_reason"] = latest.get("skip_reason")

    if not settings.tv_alert_observation_enabled:
        checks.append(
            {
                "name": "tv_observation_enabled",
                "level": "WARN",
                "message": "TV_ALERT_OBSERVATION_ENABLED=false，连续观察未启用",
            }
        )
    else:
        checks.append(
            {
                "name": "tv_observation_enabled",
                "level": "OK",
                "message": "TV Alert 连续观察已启用",
            }
        )

    if binance_env == "demo":
        checks.append(
            {
                "name": "binance_demo_environment",
                "level": "OK",
                "message": f"Binance 为 demo/testnet 环境 ({settings.binance_base_url})",
            }
        )
    elif binance_env == "live":
        checks.append(
            {
                "name": "binance_demo_environment",
                "level": "ERROR",
                "message": "Binance 为实盘 endpoint，TV 接入演练不允许实盘",
            }
        )
    else:
        checks.append(
            {
                "name": "binance_demo_environment",
                "level": "WARN",
                "message": f"无法识别 Binance 环境: {settings.binance_base_url}",
            }
        )

    if settings.tv_signal_sandbox_enabled and settings.tv_signal_reject_live_binance:
        checks.append(
            {
                "name": "tv_sandbox_guard",
                "level": "OK",
                "message": "TV Signal Sandbox 已启用且拒绝实盘 endpoint",
            }
        )
    elif not settings.tv_signal_sandbox_enabled:
        checks.append(
            {
                "name": "tv_sandbox_guard",
                "level": "WARN",
                "message": "TV_SIGNAL_SANDBOX_ENABLED=false",
            }
        )
    else:
        checks.append(
            {
                "name": "tv_sandbox_guard",
                "level": "WARN",
                "message": "TV_SIGNAL_REJECT_LIVE_BINANCE=false",
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
                "message": f"WEBHOOK_SECRET 长度过短 (length={webhook_meta['length']})",
            }
        )
    else:
        checks.append(
            {
                "name": "webhook_secret",
                "level": "OK",
                "message": f"WEBHOOK_SECRET 已配置 (length={webhook_meta['length']})",
            }
        )

    if public_configured:
        checks.append(
            {
                "name": "public_webhook_url",
                "level": "OK",
                "message": f"公网 Webhook 基址已配置，建议 URL: {webhook_hint}",
            }
        )
    else:
        checks.append(
            {
                "name": "public_webhook_url",
                "level": "WARN",
                "message": "TV_ALERT_PUBLIC_BASE_URL 未配置，请在 ngrok/公网域名就绪后填写",
            }
        )

    if settings.runtime_control_enabled:
        checks.append(
            {
                "name": "runtime_control",
                "level": "OK",
                "message": "Runtime Control 已启用，可随时锁定 webhook",
            }
        )
    else:
        checks.append(
            {
                "name": "runtime_control",
                "level": "WARN",
                "message": "RUNTIME_CONTROL_ENABLED=false，建议演练期间启用",
            }
        )

    missing_expected = sorted(settings.tv_alert_expected_symbol_set - settings.allowed_symbol_set)
    if missing_expected:
        checks.append(
            {
                "name": "expected_symbols",
                "level": "WARN",
                "message": f"ALLOWED_SYMBOLS 未包含预期交易对: {', '.join(missing_expected)}",
            }
        )
    else:
        checks.append(
            {
                "name": "expected_symbols",
                "level": "OK",
                "message": f"预期交易对已包含: {', '.join(sorted(settings.tv_alert_expected_symbol_set))}",
            }
        )

    if settings.enable_trading:
        checks.append(
            {
                "name": "enable_trading",
                "level": "WARN",
                "message": "ENABLE_TRADING=true，demo 环境也会真实下单，请确认风控",
            }
        )
    else:
        checks.append(
            {
                "name": "enable_trading",
                "level": "OK",
                "message": "ENABLE_TRADING=false，仅接收信号与 journal 记录",
            }
        )

    return {
        "level": aggregate_level(checks),
        "checks": checks,
        "summary": summary,
    }


def build_tv_observation(
    settings: Settings,
    journal_store: TradeJournalStore,
    client: BinanceClient,
    runtime_control: RuntimeControl,
    *,
    hours: int | None = None,
) -> dict[str, Any]:
    window_hours = hours if hours is not None else settings.tv_alert_observation_window_hours
    window_hours = max(1, min(int(window_hours), 168))
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    since_iso = since.isoformat()

    all_rows = journal_store.list_executions_since(since_iso, limit=1000)
    tv_rows = [row for row in all_rows if is_tv_execution_row(row, settings)]

    summary: dict[str, Any] = {
        "total_tv_signals": len(tv_rows),
        "protected_count": 0,
        "failed_count": 0,
        "tv_sandbox_rejected_count": 0,
        "blocked_by_runtime_lock_count": 0,
        "last_tv_signal_time": None,
        "last_tv_signal_status": None,
        "last_tv_signal_id": None,
        "last_tv_signal_symbol": None,
        "consecutive_failures": 0,
        "open_position_count": 0,
        "unprotected_position_count": 0,
        "live_rejection_count": 0,
    }

    for row in tv_rows:
        status = str(row.get("status") or "")
        skip = str(row.get("skip_reason") or "")
        if status == "protected":
            summary["protected_count"] += 1
        if status in {"failed", "protection_failed"}:
            summary["failed_count"] += 1
        if status == "tv_sandbox_rejected":
            summary["tv_sandbox_rejected_count"] += 1
        if status == "blocked_by_runtime_lock":
            summary["blocked_by_runtime_lock_count"] += 1
        if skip == "tv_live_binance_rejected":
            summary["live_rejection_count"] += 1

    if tv_rows:
        latest = tv_rows[0]
        summary["last_tv_signal_time"] = latest.get("created_at")
        summary["last_tv_signal_status"] = latest.get("status")
        summary["last_tv_signal_id"] = latest.get("signal_id")
        summary["last_tv_signal_symbol"] = latest.get("symbol")
        summary["consecutive_failures"] = _consecutive_tv_failures(tv_rows)

    positions: list[dict[str, Any]] = []
    algo_orders: list[dict[str, Any]] = []
    try:
        from .dashboard import build_dashboard_algo_orders, build_dashboard_positions

        positions = build_dashboard_positions(client)
        algo_orders = build_dashboard_algo_orders(settings, client)
    except Exception:
        pass

    summary["open_position_count"] = len(positions)
    unprotected_count, unprotected_symbols = _count_unprotected_positions(positions, algo_orders)
    summary["unprotected_position_count"] = unprotected_count
    summary["unprotected_symbols"] = unprotected_symbols

    checks: list[dict[str, str]] = []

    if not settings.tv_alert_observation_enabled:
        checks.append(
            {
                "name": "tv_observation_enabled",
                "level": "WARN",
                "message": "TV Alert 连续观察未启用",
            }
        )
        return {
            "level": aggregate_level(checks),
            "window_hours": window_hours,
            "summary": summary,
            "checks": checks,
        }

    runtime_state = runtime_control.status_payload()
    if runtime_state.get("enabled") and runtime_state.get("locked"):
        reason = runtime_state.get("reason") or "未说明"
        checks.append(
            {
                "name": "tv_runtime_locked",
                "level": "WARN",
                "message": f"Runtime Control 当前锁定: {reason}",
            }
        )

    last_time = _parse_created_at(summary["last_tv_signal_time"])
    if last_time is None:
        checks.append(
            {
                "name": "tv_alert_stale",
                "level": "WARN",
                "message": f"观察窗口 {window_hours}h 内暂无 TV 信号",
            }
        )
    else:
        age_minutes = (datetime.now(timezone.utc) - last_time).total_seconds() / 60.0
        if age_minutes > settings.tv_alert_stale_minutes:
            checks.append(
                {
                    "name": "tv_alert_stale",
                    "level": "WARN",
                    "message": (
                        f"最近 TV 信号已 {int(age_minutes)} 分钟未更新 "
                        f"(阈值 {settings.tv_alert_stale_minutes} 分钟)"
                    ),
                }
            )
        else:
            checks.append(
                {
                    "name": "tv_alert_stale",
                    "level": "OK",
                    "message": f"最近 TV 信号: {summary['last_tv_signal_time']}",
                }
            )

    streak = int(summary["consecutive_failures"])
    if streak >= settings.tv_alert_consecutive_failure_error:
        checks.append(
            {
                "name": "tv_alert_consecutive_failures",
                "level": "ERROR",
                "message": (
                    f"TV 信号连续失败 {streak} 次 "
                    f"(阈值 ERROR>={settings.tv_alert_consecutive_failure_error})"
                ),
            }
        )
    elif streak >= settings.tv_alert_consecutive_failure_warn:
        checks.append(
            {
                "name": "tv_alert_consecutive_failures",
                "level": "WARN",
                "message": (
                    f"TV 信号连续失败 {streak} 次 "
                    f"(阈值 WARN>={settings.tv_alert_consecutive_failure_warn})"
                ),
            }
        )
    elif streak > 0:
        checks.append(
            {
                "name": "tv_alert_consecutive_failures",
                "level": "OK",
                "message": f"TV 连续失败 {streak} 次，未达告警阈值",
            }
        )
    else:
        checks.append(
            {
                "name": "tv_alert_consecutive_failures",
                "level": "OK",
                "message": "无连续失败 TV 信号",
            }
        )

    if unprotected_count > 0:
        checks.append(
            {
                "name": "tv_unprotected_position",
                "level": "ERROR",
                "message": f"存在未保护持仓: {', '.join(unprotected_symbols)}",
            }
        )
    elif summary["open_position_count"] > 0:
        checks.append(
            {
                "name": "tv_unprotected_position",
                "level": "OK",
                "message": "当前持仓均有止损类条件单",
            }
        )
    else:
        checks.append(
            {
                "name": "tv_unprotected_position",
                "level": "OK",
                "message": "当前无持仓",
            }
        )

    if summary["live_rejection_count"] > 0:
        checks.append(
            {
                "name": "tv_live_binance_rejected",
                "level": "WARN",
                "message": f"窗口内 {summary['live_rejection_count']} 次 tv_live_binance_rejected",
            }
        )
    elif binance_env_label(settings.binance_base_url) == "live":
        checks.append(
            {
                "name": "tv_live_binance_rejected",
                "level": "WARN",
                "message": "当前 Binance 为实盘 endpoint，TV 信号应被拒绝",
            }
        )
    else:
        checks.append(
            {
                "name": "tv_live_binance_rejected",
                "level": "OK",
                "message": "窗口内无实盘 endpoint 拒绝记录",
            }
        )

    return {
        "level": aggregate_level(checks),
        "window_hours": window_hours,
        "summary": summary,
        "checks": checks,
    }


_OBSERVATION_ALERT_TITLES: dict[str, str] = {
    "tv_alert_stale": "TradingView 信号长时间未更新",
    "tv_alert_consecutive_failures": "TradingView 信号连续失败",
    "tv_unprotected_position": "TradingView 演练存在未保护持仓",
    "tv_runtime_locked": "TradingView 演练期间 Runtime 锁定",
    "tv_live_binance_rejected": "TradingView 信号被实盘环境保护拒绝",
}


def alerts_from_tv_observation(observation: dict[str, Any], *, created_at: str | None) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    summary = observation.get("summary") or {}
    for check in observation.get("checks") or []:
        level = check.get("level", "OK")
        if level not in {"WARN", "ERROR"}:
            continue
        name = str(check.get("name") or "tv_observation")
        alerts.append(
            {
                "id": f"tv-observation-{name}",
                "source": "tv_observation",
                "level": level,
                "type": name,
                "title": _OBSERVATION_ALERT_TITLES.get(name, name),
                "message": str(check.get("message") or ""),
                "symbol": summary.get("last_tv_signal_symbol"),
                "status": summary.get("last_tv_signal_status"),
                "reason": name,
                "created_at": created_at or summary.get("last_tv_signal_time"),
            }
        )
    return alerts

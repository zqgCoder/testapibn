from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .live_guard import build_live_guard_status, live_guard_applies

if TYPE_CHECKING:
    from .config import Settings
    from .runtime_control import RuntimeControl

DEFAULT_CANARY_SYMBOL = "BTCUSDT"


@dataclass
class CanaryMarketSnapshot:
    symbol: str = DEFAULT_CANARY_SYMBOL
    positions: list[dict[str, Any]] = field(default_factory=list)
    algo_orders: list[dict[str, Any]] = field(default_factory=list)
    open_orders: list[dict[str, Any]] = field(default_factory=list)


def _position_amount(row: dict[str, Any]) -> Decimal:
    try:
        return Decimal(str(row.get("positionAmt", "0")))
    except Exception:
        return Decimal("0")


def symbol_has_open_position(positions: list[dict[str, Any]]) -> bool:
    for row in positions:
        if _position_amount(row) != 0:
            return True
    return False


def _empty_reconcile_summary() -> dict[str, Any]:
    return {
        "open_position_count": 0,
        "unprotected_position_count": 0,
        "residual_order_symbol_count": 0,
        "error_count": 0,
        "warn_count": 0,
    }


def _reconcile_summary_from_report(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return _empty_reconcile_summary()
    summary = dict((report.get("summary") or {}))
    return {
        "open_position_count": summary.get("open_position_count", 0),
        "unprotected_position_count": summary.get("unprotected_position_count", 0),
        "residual_order_symbol_count": summary.get("residual_order_symbol_count", 0),
        "error_count": summary.get("error_count", 0),
        "warn_count": summary.get("warn_count", 0),
    }


def _runtime_section(runtime_control: RuntimeControl | None) -> dict[str, Any]:
    if runtime_control is None:
        return {
            "enabled": False,
            "locked": None,
            "effective_locked": None,
            "reason": None,
            "one_shot": {
                "enabled": False,
                "remaining": 0,
                "reason": None,
                "operator": None,
                "started_at": None,
                "expires_at": None,
                "consumed_by_signal_id": None,
                "consumed_at": None,
            },
        }
    payload = runtime_control.status_payload()
    return {
        "enabled": payload.get("enabled"),
        "locked": payload.get("locked"),
        "effective_locked": payload.get("effective_locked"),
        "reason": payload.get("reason"),
        "locked_by": payload.get("locked_by"),
        "locked_at": payload.get("locked_at"),
        "one_shot": payload.get("one_shot") or {},
    }


def evaluate_canary_blocking_reasons(
    settings: Settings,
    runtime_control: RuntimeControl | None,
    *,
    market: CanaryMarketSnapshot | None = None,
    reconcile_summary: dict[str, Any] | None = None,
) -> list[str]:
    reasons: list[str] = []
    guard = build_live_guard_status(settings, runtime_control)
    is_live = bool(guard.get("is_live"))
    guard_active = bool(guard.get("guard_active"))

    if not is_live:
        reasons.append("not_live_environment")
    if is_live and not guard_active:
        reasons.append("live_guard_inactive")

    if not settings.live_trading_enabled:
        reasons.append("live_trading_disabled")
    if not guard.get("live_confirm_phrase_valid"):
        reasons.append("live_confirm_phrase_invalid")

    if DEFAULT_CANARY_SYMBOL not in settings.live_allowed_symbol_set:
        reasons.append("btcusdt_not_allowed")

    if float(settings.live_max_risk_usdt) > 1:
        reasons.append("live_max_risk_too_high")
    if float(settings.live_max_margin_usdt) > 20:
        reasons.append("live_max_margin_too_high")
    if float(settings.live_max_position_notional_usdt) > 100:
        reasons.append("live_max_notional_too_high")

    if not settings.live_reject_tradingview_by_default:
        reasons.append("tradingview_live_not_rejected")

    runtime = _runtime_section(runtime_control)
    if not bool(runtime.get("locked")):
        reasons.append("runtime_not_locked")
    one_shot = runtime.get("one_shot") or {}
    if bool(one_shot.get("enabled")):
        reasons.append("one_shot_active")

    snapshot = market or CanaryMarketSnapshot()
    if symbol_has_open_position(snapshot.positions):
        reasons.append("btcusdt_position_not_flat")
    if snapshot.algo_orders:
        reasons.append("btcusdt_algo_orders_exist")
    if snapshot.open_orders:
        reasons.append("btcusdt_open_orders_exist")

    summary = reconcile_summary or _empty_reconcile_summary()
    error_count = int(summary.get("error_count") or 0)
    if error_count > 0:
        reasons.append("reconcile_error")

    return reasons


def build_live_canary_preflight(
    settings: Settings,
    runtime_control: RuntimeControl | None,
    *,
    market: CanaryMarketSnapshot | None = None,
    reconcile_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    guard = build_live_guard_status(settings, runtime_control)
    runtime = _runtime_section(runtime_control)
    snapshot = market or CanaryMarketSnapshot()
    reconcile_summary = _reconcile_summary_from_report(reconcile_report)
    blocking_reasons = evaluate_canary_blocking_reasons(
        settings,
        runtime_control,
        market=snapshot,
        reconcile_summary=reconcile_summary,
    )

    position_amt = Decimal("0")
    for row in snapshot.positions:
        position_amt += _position_amount(row)

    return {
        "binance_env": guard["binance_env"],
        "binance_base_url": guard["binance_base_url"],
        "is_live": guard["is_live"],
        "live_guard": guard,
        "live_trading_enabled": guard["live_trading_enabled"],
        "live_confirm_phrase_configured": guard["live_confirm_phrase_configured"],
        "live_confirm_phrase_valid": guard["live_confirm_phrase_valid"],
        "live_allowed_symbols": guard["live_allowed_symbols"],
        "live_max_risk_usdt": guard["live_max_risk_usdt"],
        "live_max_margin_usdt": guard["live_max_margin_usdt"],
        "live_max_position_notional_usdt": guard["live_max_position_notional_usdt"],
        "live_require_one_shot": guard["live_require_one_shot"],
        "live_reject_tradingview_by_default": guard["live_reject_tradingview_by_default"],
        "live_force_runtime_locked_on_startup": guard["live_force_runtime_locked_on_startup"],
        "live_canary_mode": guard["live_canary_mode"],
        "runtime": runtime,
        "btcusdt": {
            "symbol": snapshot.symbol,
            "position_count": len(snapshot.positions),
            "position_amt": str(position_amt),
            "position_flat": not symbol_has_open_position(snapshot.positions),
            "algo_order_count": len(snapshot.algo_orders),
            "open_order_count": len(snapshot.open_orders),
            "positions": snapshot.positions,
            "algo_orders": snapshot.algo_orders,
            "open_orders": snapshot.open_orders,
        },
        "reconcile_summary": reconcile_summary,
        "canary_ready": len(blocking_reasons) == 0,
        "blocking_reasons": blocking_reasons,
        "phase": "preflight_only",
        "note": "v6.4.5 仅 preflight 展示，本阶段不执行真实实盘交易",
    }


def fetch_canary_market_snapshot(client: Any, symbol: str = DEFAULT_CANARY_SYMBOL) -> CanaryMarketSnapshot:
    positions_raw = client.position_risk(symbol)
    positions = positions_raw if isinstance(positions_raw, list) else [positions_raw]
    positions = [row for row in positions if isinstance(row, dict)]

    algo_raw = client.open_algo_orders(symbol)
    algo_orders = algo_raw if isinstance(algo_raw, list) else [algo_raw]
    algo_orders = [row for row in algo_orders if isinstance(row, dict)]

    open_raw = client.open_orders(symbol)
    open_orders = open_raw if isinstance(open_raw, list) else [open_raw]
    open_orders = [row for row in open_orders if isinstance(row, dict)]

    return CanaryMarketSnapshot(
        symbol=symbol,
        positions=positions,
        algo_orders=algo_orders,
        open_orders=open_orders,
    )

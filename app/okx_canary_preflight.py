from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .exchanges.base import ExchangeClient
from .live_canary_preflight import (
    CanaryMarketSnapshot,
    _empty_reconcile_summary,
    _reconcile_summary_from_report,
    _runtime_section,
    symbol_has_open_position,
)
from .okx_guard import (
    build_okx_guard_status,
    evaluate_okx_guard_blocking_reasons,
    okx_guard_applies,
)

if TYPE_CHECKING:
    from .config import Settings
    from .runtime_control import RuntimeControl

DEFAULT_OKX_SYMBOL = "BTCUSDT"


def fetch_okx_market_snapshot(
    exchange_client: ExchangeClient,
    symbol: str = DEFAULT_OKX_SYMBOL,
) -> CanaryMarketSnapshot:
    positions = exchange_client.get_positions(symbol)
    open_orders = exchange_client.get_open_orders(symbol)
    algo_orders = exchange_client.get_algo_orders(symbol)
    return CanaryMarketSnapshot(
        symbol=symbol.strip().upper(),
        positions=positions,
        algo_orders=algo_orders,
        open_orders=open_orders,
    )


def evaluate_okx_preflight_blocking_reasons(
    settings: Settings,
    runtime_control: RuntimeControl | None,
    *,
    market: CanaryMarketSnapshot | None = None,
    reconcile_summary: dict[str, Any] | None = None,
) -> list[str]:
    reasons = evaluate_okx_guard_blocking_reasons(settings, runtime_control)
    if not okx_guard_applies(settings):
        reasons.insert(0, "not_okx_exchange")
    reasons.append("okx_execution_not_implemented")
    snapshot = market or CanaryMarketSnapshot()
    if symbol_has_open_position(snapshot.positions):
        reasons.append("btcusdt_position_not_flat")
    if snapshot.algo_orders:
        reasons.append("btcusdt_algo_orders_exist")
    if snapshot.open_orders:
        reasons.append("btcusdt_open_orders_exist")
    summary = reconcile_summary or _empty_reconcile_summary()
    if int(summary.get("error_count") or 0) > 0:
        reasons.append("reconcile_error")
    return reasons


def build_okx_canary_preflight(
    settings: Settings,
    runtime_control: RuntimeControl | None,
    *,
    market: CanaryMarketSnapshot | None = None,
    reconcile_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    guard = build_okx_guard_status(settings, runtime_control)
    reconcile_summary = _reconcile_summary_from_report(reconcile_report)
    snapshot = market or CanaryMarketSnapshot()
    blocking_reasons = evaluate_okx_preflight_blocking_reasons(
        settings,
        runtime_control,
        market=snapshot,
        reconcile_summary=reconcile_summary,
    )
    canary_ready = len(blocking_reasons) == 0
    return {
        "exchange": "okx",
        "phase": "okx_readonly_rejection_guard",
        "readonly_mode": bool(settings.okx_readonly_mode),
        "okx_live_trading_enabled": bool(settings.okx_live_trading_enabled),
        "okx_confirm_phrase_configured": guard["okx_confirm_phrase_configured"],
        "okx_confirm_phrase_valid": guard["okx_confirm_phrase_valid"],
        "allowed_inst_ids": guard["okx_allowed_inst_ids"],
        "okx_guard": guard,
        "runtime": _runtime_section(runtime_control),
        "btcusdt": {
            "symbol": snapshot.symbol,
            "position_count": len(snapshot.positions),
            "has_open_position": symbol_has_open_position(snapshot.positions),
            "algo_order_count": len(snapshot.algo_orders),
            "open_order_count": len(snapshot.open_orders),
            "positions": snapshot.positions,
            "algo_orders": snapshot.algo_orders,
            "open_orders": snapshot.open_orders,
        },
        "reconcile_summary": reconcile_summary,
        "canary_ready": canary_ready,
        "blocking_reasons": blocking_reasons,
    }

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .exchanges.okx import OkxExchange
from .exchanges.okx_sizing import compute_minimal_contract_sz, decimal_field
from .exchanges.okx_symbols import inst_id_to_symbol, symbol_to_inst_id
from .okx_guard import (
    OkxGuardRejection,
    build_okx_guard_skip_result,
    validate_okx_canary_before_execute,
)

if TYPE_CHECKING:
    from .config import Settings
    from .schemas import TradingViewSignal

logger = logging.getLogger(__name__)

OKX_CANARY_ALLOWED_SOURCE = "local_canary"
OKX_CANARY_OPEN_SIDE = "buy"


def _client_order_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"[:32]


def execute_okx_minimal_open_close(
    exchange: OkxExchange,
    settings: Settings,
    signal: TradingViewSignal,
    raw_payload: dict[str, Any],
    *,
    signal_key: str,
    runtime_control=None,
) -> dict[str, Any]:
    rejection = validate_okx_canary_before_execute(
        settings,
        signal,
        raw_payload,
        runtime_control=runtime_control,
    )
    if rejection:
        return build_okx_guard_skip_result(rejection)

    inst_id = symbol_to_inst_id(str(signal.symbol))
    internal_symbol = inst_id_to_symbol(inst_id)
    td_mode = settings.okx_td_mode.strip().lower()
    mark_price = exchange.get_mark_price(inst_id)
    meta = exchange.get_instrument(inst_id)
    margin_budget = decimal_field(signal.margin_usdt or raw_payload.get("margin_usdt"))
    sz, notional, estimated_margin = compute_minimal_contract_sz(
        meta,
        mark_price=mark_price,
        margin_usdt=margin_budget if margin_budget > 0 else None,
    )

    limit_rejection = validate_okx_canary_after_sizing(
        settings,
        signal,
        raw_payload,
        sz=sz,
        notional=notional,
        estimated_margin=estimated_margin,
    )
    if limit_rejection:
        return build_okx_guard_skip_result(limit_rejection)

    open_client_id = _client_order_id("okxcanaryopen")
    logger.info(
        "OKX minimal canary open: signal_key=%s instId=%s sz=%s notional=%s tdMode=%s",
        signal_key,
        inst_id,
        sz,
        notional,
        td_mode,
    )
    open_order = exchange.place_market_order_minimal(
        inst_id=inst_id,
        side=OKX_CANARY_OPEN_SIDE,
        sz=sz,
        td_mode=td_mode,
        client_order_id=open_client_id,
    )
    close_order = exchange.close_position_market(
        inst_id=inst_id,
        td_mode=td_mode,
        client_order_id=_client_order_id("okxcanaryclose"),
    )
    reconcile_report = exchange.reconcile(trigger=f"okx_canary:{signal_key}")
    reconcile_summary = (reconcile_report or {}).get("summary") or {}
    success = bool((reconcile_report or {}).get("success"))
    if int(reconcile_summary.get("error_count") or 0) > 0:
        success = False

    return {
        "orders": {
            "open": open_order,
            "close": close_order,
        },
        "skipped": False,
        "exchange": "okx",
        "okx_canary": {
            "phase": "minimal_open_close",
            "signal_key": signal_key,
            "symbol": internal_symbol,
            "instId": inst_id,
            "side": OKX_CANARY_OPEN_SIDE,
            "sz": format(sz, "f"),
            "notional_usdt": format(notional, "f"),
            "estimated_margin_usdt": format(estimated_margin, "f"),
            "mark_price": format(mark_price, "f"),
            "td_mode": td_mode,
            "instrument": {
                "lotSz": format(meta.lot_sz, "f"),
                "minSz": format(meta.min_sz, "f"),
                "ctVal": format(meta.ct_val, "f"),
            },
            "reconcile_summary": reconcile_summary,
            "success": success,
        },
        "protection_summary": {
            "protection_skipped_reason": "okx_minimal_canary_no_tpsl",
            "stop_loss_submitted": False,
            "take_profit_submitted_count": 0,
            "position_confirmed": True,
        },
        "entry_summary": {
            "filled_qty": format(sz, "f"),
            "entry_type": "market",
        },
    }


def validate_okx_canary_after_sizing(
    settings: Settings,
    signal: TradingViewSignal,
    raw_payload: dict[str, Any],
    *,
    sz: Decimal,
    notional: Decimal,
    estimated_margin: Decimal,
) -> OkxGuardRejection | None:
    _ = sz
    max_risk = Decimal(str(settings.okx_max_risk_usdt))
    max_margin = Decimal(str(settings.okx_max_margin_usdt))
    max_notional = Decimal(str(settings.okx_max_position_notional_usdt))

    risk_usdt = decimal_field(signal.risk_usdt or raw_payload.get("risk_usdt"))
    if risk_usdt > 0 and risk_usdt > max_risk:
        return OkxGuardRejection(
            skip_reason="okx_risk_too_large",
            message=(
                f"OKX canary 拒绝：risk_usdt={risk_usdt} 超过上限 OKX_MAX_RISK_USDT={max_risk}"
            ),
        )

    margin_usdt = decimal_field(signal.margin_usdt or raw_payload.get("margin_usdt"))
    if margin_usdt > 0 and margin_usdt > max_margin:
        return OkxGuardRejection(
            skip_reason="okx_margin_too_large",
            message=(
                f"OKX canary 拒绝：margin_usdt={margin_usdt} 超过上限 OKX_MAX_MARGIN_USDT={max_margin}"
            ),
        )
    if estimated_margin > max_margin:
        return OkxGuardRejection(
            skip_reason="okx_margin_too_large",
            message=(
                f"OKX canary 拒绝：estimated_margin={estimated_margin} 超过上限 "
                f"OKX_MAX_MARGIN_USDT={max_margin}"
            ),
        )
    if notional > max_notional:
        return OkxGuardRejection(
            skip_reason="okx_notional_too_large",
            message=(
                f"OKX canary 拒绝：notional={notional} 超过上限 "
                f"OKX_MAX_NOTIONAL_USDT={max_notional}"
            ),
        )
    return None

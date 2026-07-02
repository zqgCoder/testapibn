from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, InvalidOperation


@dataclass(frozen=True)
class OkxInstrumentMeta:
    inst_id: str
    inst_type: str
    lot_sz: Decimal
    min_sz: Decimal
    ct_val: Decimal
    ct_mult: Decimal
    tick_sz: Decimal


def floor_to_lot_sz(value: Decimal, lot_sz: Decimal) -> Decimal:
    if lot_sz <= 0:
        return value
    steps = (value / lot_sz).to_integral_value(rounding=ROUND_DOWN)
    return steps * lot_sz


def parse_instrument_row(row: dict) -> OkxInstrumentMeta:
    inst_id = str(row.get("instId") or "")
    return OkxInstrumentMeta(
        inst_id=inst_id,
        inst_type=str(row.get("instType") or ""),
        lot_sz=Decimal(str(row.get("lotSz") or "1")),
        min_sz=Decimal(str(row.get("minSz") or "1")),
        ct_val=Decimal(str(row.get("ctVal") or "1")),
        ct_mult=Decimal(str(row.get("ctMult") or "1")),
        tick_sz=Decimal(str(row.get("tickSz") or "0.1")),
    )


def estimate_swap_notional_usdt(sz: Decimal, ct_val: Decimal, mark_price: Decimal) -> Decimal:
    return abs(sz) * ct_val * mark_price


def compute_minimal_contract_sz(
    meta: OkxInstrumentMeta,
    *,
    mark_price: Decimal,
    margin_usdt: Decimal | None = None,
    leverage: Decimal | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (sz contracts, notional_usdt, estimated_margin_usdt) using minimum allowed size."""
    if mark_price <= 0:
        raise ValueError("mark_price must be > 0")
    sz = max(meta.min_sz, floor_to_lot_sz(meta.min_sz, meta.lot_sz))
    if sz < meta.min_sz:
        sz = meta.min_sz
    notional = estimate_swap_notional_usdt(sz, meta.ct_val, mark_price)
    lev = leverage if leverage is not None and leverage > 0 else Decimal("1")
    if margin_usdt is not None and margin_usdt > 0:
        estimated_margin = notional / lev
        if estimated_margin > margin_usdt:
            raise ValueError(
                f"minimum contract size requires margin={estimated_margin} > budget={margin_usdt}"
            )
    else:
        estimated_margin = notional / lev
    return sz, notional, estimated_margin


def decimal_field(raw: object, default: str = "0") -> Decimal:
    if raw in {None, ""}:
        return Decimal(default)
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return Decimal(default)

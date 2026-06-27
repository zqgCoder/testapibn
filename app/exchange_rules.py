from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from functools import lru_cache

from .binance_client import BinanceClient


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    price_tick: Decimal
    qty_step: Decimal
    market_qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def normalize_market_quantity(qty: Decimal, rules: SymbolRules, reference_price: Decimal) -> Decimal | None:
    """Floor qty to market step and verify exchange min_qty / min_notional."""
    normalized = floor_to_step(qty, rules.market_qty_step)
    if normalized <= 0:
        return None
    if rules.min_qty and normalized < rules.min_qty:
        return None
    if rules.min_notional and normalized * reference_price < rules.min_notional:
        return None
    return normalized


class ExchangeRules:
    def __init__(self, client: BinanceClient):
        self.client = client

    @lru_cache(maxsize=512)
    def get(self, symbol: str) -> SymbolRules:
        info = self.client.exchange_info()
        for item in info.get("symbols", []):
            if item.get("symbol") != symbol:
                continue
            filters = {f.get("filterType"): f for f in item.get("filters", [])}
            price_filter = filters.get("PRICE_FILTER", {})
            lot_size = filters.get("LOT_SIZE", {})
            market_lot_size = filters.get("MARKET_LOT_SIZE", lot_size)
            min_notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}

            return SymbolRules(
                symbol=symbol,
                price_tick=Decimal(str(price_filter.get("tickSize", "0.00000001"))),
                qty_step=Decimal(str(lot_size.get("stepSize", "0.00000001"))),
                market_qty_step=Decimal(str(market_lot_size.get("stepSize", lot_size.get("stepSize", "0.00000001")))),
                min_qty=Decimal(str(lot_size.get("minQty", "0"))),
                min_notional=Decimal(str(min_notional_filter.get("notional", min_notional_filter.get("minNotional", "0")))),
            )
        raise ValueError(f"Symbol not found in exchangeInfo: {symbol}")

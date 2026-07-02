from __future__ import annotations

from typing import TYPE_CHECKING

from .base import ExchangeClient
from .binance_futures import BinanceFuturesExchange
from .okx import OkxExchange

if TYPE_CHECKING:
    from ..binance_client import BinanceClient
    from ..config import Settings
    from ..reconcile import SafetyReconcileService


def create_exchange_client(
    settings: Settings,
    client: BinanceClient | None = None,
    *,
    reconcile_service: SafetyReconcileService | None = None,
) -> ExchangeClient:
    exchange = settings.exchange.strip().lower()
    if exchange == "binance":
        if client is None:
            raise RuntimeError("BinanceClient is required when EXCHANGE=binance")
        return BinanceFuturesExchange(
            settings,
            client,
            reconcile_service=reconcile_service,
        )
    if exchange == "okx":
        return OkxExchange(settings)
    raise RuntimeError(f"Unsupported EXCHANGE={settings.exchange!r}")

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import ExchangeClient
from .binance_futures import BinanceFuturesExchange

if TYPE_CHECKING:
    from ..binance_client import BinanceClient
    from ..config import Settings
    from ..reconcile import SafetyReconcileService


def create_exchange_client(
    settings: Settings,
    client: BinanceClient,
    *,
    reconcile_service: SafetyReconcileService | None = None,
) -> ExchangeClient:
    exchange = settings.exchange.strip().lower()
    if exchange == "binance":
        return BinanceFuturesExchange(
            settings,
            client,
            reconcile_service=reconcile_service,
        )
    raise RuntimeError(
        f"Unsupported EXCHANGE={settings.exchange!r}; only 'binance' is available in v6.5.0"
    )

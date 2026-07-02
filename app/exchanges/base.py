from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ExchangeClient(ABC):
    """Exchange adapter for read-only queries and maintenance operations."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short exchange identifier, e.g. ``binance``."""

    @abstractmethod
    def get_positions(self, symbol: str) -> list[dict[str, Any]]:
        """Return position rows for ``symbol`` (empty when flat)."""

    @abstractmethod
    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        """Return regular open orders for ``symbol``."""

    @abstractmethod
    def get_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        """Return conditional/algo open orders for ``symbol``."""

    @abstractmethod
    def cancel_open_orders(self, symbol: str) -> Any:
        """Cancel all regular open orders for ``symbol``."""

    @abstractmethod
    def cancel_algo_orders(self, symbol: str) -> Any:
        """Cancel all algo/conditional open orders for ``symbol``."""

    @abstractmethod
    def close_position(
        self,
        symbol: str,
        *,
        reason: str = "",
        operator: str = "",
    ) -> dict[str, Any]:
        """Close an open position and perform maintenance cleanup for ``symbol``."""

    @abstractmethod
    def reconcile(self, *, trigger: str = "manual") -> dict[str, Any]:
        """Run a safety reconcile/audit and return the report payload."""

from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .base import ExchangeClient

if TYPE_CHECKING:
    from ..binance_client import BinanceClient
    from ..config import Settings
    from ..reconcile import SafetyReconcileService

logger = logging.getLogger(__name__)


class BinanceFuturesExchange(ExchangeClient):
    """Binance USD-M Futures adapter wrapping :class:`~app.binance_client.BinanceClient`."""

    def __init__(
        self,
        settings: Settings,
        client: BinanceClient,
        *,
        reconcile_service: SafetyReconcileService | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._reconcile_service = reconcile_service

    @property
    def name(self) -> str:
        return "binance"

    @property
    def client(self) -> BinanceClient:
        """Underlying REST client (trading paths may still use this directly)."""
        return self._client

    def get_positions(self, symbol: str) -> list[dict[str, Any]]:
        data = self._client.position_risk(symbol)
        rows = data if isinstance(data, list) else [data]
        normalized = symbol.strip().upper()
        return [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("symbol", "")).upper() == normalized
        ]

    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        data = self._client.open_orders(symbol)
        rows = data if isinstance(data, list) else [data]
        return [row for row in rows if isinstance(row, dict)]

    def get_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        data = self._client.open_algo_orders(symbol)
        rows = data if isinstance(data, list) else [data]
        return [row for row in rows if isinstance(row, dict)]

    def cancel_open_orders(self, symbol: str) -> Any:
        return self._client.cancel_all_open_orders(symbol)

    def cancel_algo_orders(self, symbol: str) -> Any:
        return self._client.cancel_all_algo_open_orders(symbol)

    def reconcile(self, *, trigger: str = "manual") -> dict[str, Any]:
        if self._reconcile_service is None:
            raise RuntimeError("Reconcile service is not configured for this exchange adapter")
        return self._reconcile_service.run_audit(trigger=trigger)

    @staticmethod
    def _position_amount_from_row(row: dict[str, Any] | None) -> Decimal:
        if not row:
            return Decimal("0")
        return Decimal(str(row.get("positionAmt", "0")))

    @staticmethod
    def _client_order_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:16]}"[:36]

    def _cancel_symbol_orders(self, symbol: str) -> dict[str, Any]:
        result: dict[str, Any] = {"regular": None, "algo": None}
        try:
            result["regular"] = self.cancel_open_orders(symbol)
        except Exception as exc:
            logger.warning("Failed to cancel regular open orders for %s: %s", symbol, exc)
            result["regular_error"] = str(exc)
        try:
            result["algo"] = self.cancel_algo_orders(symbol)
        except Exception as exc:
            logger.warning("Failed to cancel algo open orders for %s: %s", symbol, exc)
            result["algo_error"] = str(exc)
        return result

    def _wait_for_position_zero(
        self,
        symbol: str,
        wait_seconds: int,
        poll_interval_sec: float = 0.5,
    ) -> Decimal:
        deadline = time.time() + max(0.0, float(wait_seconds))
        latest_amt = self._client.current_position_amount(symbol)
        while latest_amt != 0 and time.time() < deadline:
            time.sleep(min(poll_interval_sec, max(0.0, deadline - time.time())))
            latest_amt = self._client.current_position_amount(symbol)
        return latest_amt

    def close_position(
        self,
        symbol: str,
        *,
        reason: str = "",
        operator: str = "",
        cancel_before_close: bool = True,
        cancel_after_close: bool = True,
        wait_seconds: int = 10,
    ) -> dict[str, Any]:
        position_rows = self.get_positions(symbol)
        position_before_row = position_rows[0] if position_rows else None
        position_amt = self._position_amount_from_row(position_before_row)

        cancel_regular_before = None
        cancel_algo_before = None
        cancel_regular_after = None
        cancel_algo_after = None
        close_side: str | None = None
        close_quantity: str | None = None
        close_order = None

        if position_amt == 0:
            if cancel_before_close or cancel_after_close:
                cancel_result = self._cancel_symbol_orders(symbol)
                cancel_regular_before = cancel_result.get("regular")
                cancel_algo_before = cancel_result.get("algo")
                if cancel_result.get("regular_error"):
                    cancel_regular_before = {"error": cancel_result["regular_error"]}
                if cancel_result.get("algo_error"):
                    cancel_algo_before = {"error": cancel_result["algo_error"]}
            position_after_rows = self.get_positions(symbol)
            position_after_row = position_after_rows[0] if position_after_rows else None
            return {
                "symbol": symbol,
                "position_before": position_before_row,
                "close_side": None,
                "close_quantity": None,
                "close_order": None,
                "position_after": position_after_row,
                "cancel_regular_before": cancel_regular_before,
                "cancel_algo_before": cancel_algo_before,
                "cancel_regular_after": cancel_regular_after,
                "cancel_algo_after": cancel_algo_after,
                "success": True,
                "status": "no_position_cleanup_done",
                "reason": reason,
                "operator": operator,
            }

        close_side = "SELL" if position_amt > 0 else "BUY"
        close_quantity = format(abs(position_amt), "f")

        if cancel_before_close:
            cancel_result = self._cancel_symbol_orders(symbol)
            cancel_regular_before = cancel_result.get("regular")
            cancel_algo_before = cancel_result.get("algo")
            if cancel_result.get("regular_error"):
                cancel_regular_before = {"error": cancel_result["regular_error"]}
            if cancel_result.get("algo_error"):
                cancel_algo_before = {"error": cancel_result["algo_error"]}

        try:
            close_order = self._client.close_position_market(
                symbol=symbol,
                position_amt=position_amt,
                client_order_id=self._client_order_id("maintclose"),
            )
        except Exception as exc:
            close_error = str(exc)
            logger.error(
                "Maintenance position close failed: symbol=%s operator=%s reason=%s error=%s",
                symbol,
                operator,
                reason,
                exc,
            )
            position_after_rows = self.get_positions(symbol)
            position_after_row = position_after_rows[0] if position_after_rows else None
            return {
                "symbol": symbol,
                "position_before": position_before_row,
                "close_side": close_side,
                "close_quantity": close_quantity,
                "close_order": None,
                "close_error": close_error,
                "position_after": position_after_row,
                "cancel_regular_before": cancel_regular_before,
                "cancel_algo_before": cancel_algo_before,
                "cancel_regular_after": cancel_regular_after,
                "cancel_algo_after": cancel_algo_after,
                "success": False,
                "status": "close_order_failed",
                "reason": reason,
                "operator": operator,
            }

        self._wait_for_position_zero(symbol, wait_seconds)
        position_after_rows = self.get_positions(symbol)
        position_after_row = position_after_rows[0] if position_after_rows else None
        position_after_amt = self._position_amount_from_row(position_after_row)

        if cancel_after_close:
            cancel_result = self._cancel_symbol_orders(symbol)
            cancel_regular_after = cancel_result.get("regular")
            cancel_algo_after = cancel_result.get("algo")
            if cancel_result.get("regular_error"):
                cancel_regular_after = {"error": cancel_result["regular_error"]}
            if cancel_result.get("algo_error"):
                cancel_algo_after = {"error": cancel_result["algo_error"]}

        closed = position_after_amt == 0
        status = "position_closed" if closed else "position_close_incomplete"
        logger.info(
            "Maintenance position close: symbol=%s operator=%s reason=%s status=%s close_side=%s qty=%s",
            symbol,
            operator,
            reason,
            status,
            close_side,
            close_quantity,
        )
        return {
            "symbol": symbol,
            "position_before": position_before_row,
            "close_side": close_side,
            "close_quantity": close_quantity,
            "close_order": close_order,
            "position_after": position_after_row,
            "cancel_regular_before": cancel_regular_before,
            "cancel_algo_before": cancel_algo_before,
            "cancel_regular_after": cancel_regular_after,
            "cancel_algo_after": cancel_algo_after,
            "success": closed,
            "status": status,
            "reason": reason,
            "operator": operator,
        }

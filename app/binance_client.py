from __future__ import annotations

import hashlib
import hmac
import logging
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Settings

logger = logging.getLogger(__name__)


class BinanceAPIError(RuntimeError):
    pass


class BinanceClient:
    """Small USD-M Futures REST client using Binance signed requests."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.binance_base_url.rstrip("/")
        self.session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.5,
            status_forcelist=[418, 429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "DELETE"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"X-MBX-APIKEY": settings.binance_api_key})

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _sign(self, params: dict[str, Any]) -> str:
        query = urlencode(params, doseq=True)
        return hmac.new(
            self.settings.binance_api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def public_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url + path
        resp = self.session.request(
            method=method.upper(),
            url=url,
            params=params,
            timeout=self.settings.request_timeout,
        )
        if resp.status_code >= 400:
            raise BinanceAPIError(f"{resp.status_code} {resp.text}")
        return resp.json()

    def signed_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.settings.binance_api_key or not self.settings.binance_api_secret:
            raise BinanceAPIError("Binance API key/secret are required for signed Binance requests")

        payload: dict[str, Any] = dict(params or {})
        payload["timestamp"] = self._now_ms()
        payload["recvWindow"] = self.settings.recv_window
        payload["signature"] = self._sign(payload)

        url = self.base_url + path
        resp = self.session.request(
            method=method.upper(),
            url=url,
            params=payload,
            timeout=self.settings.request_timeout,
        )
        if resp.status_code >= 400:
            logger.error("Binance API error path=%s status=%s body=%s", path, resp.status_code, resp.text)
            raise BinanceAPIError(f"{resp.status_code} {resp.text}")
        return resp.json()


    def futures_balance(self) -> Any:
        """Return USD-M futures account balances using the V3 endpoint."""
        return self.signed_request("GET", "/fapi/v3/balance")

    def asset_balance(self, asset: str = "USDT") -> dict[str, Any]:
        balances = self.futures_balance()
        target = asset.upper()
        for item in balances:
            if str(item.get("asset", "")).upper() == target:
                return item
        raise BinanceAPIError(f"Asset balance not found: {asset}")

    def commission_rate(self, symbol: str) -> dict[str, Any]:
        """Return user's maker/taker commission rate for a symbol."""
        return self.signed_request("GET", "/fapi/v1/commissionRate", {"symbol": symbol})

    def leverage_brackets(self, symbol: str) -> Any:
        """Return user's notional/leverage brackets for a symbol."""
        return self.signed_request("GET", "/fapi/v1/leverageBracket", {"symbol": symbol})

    def exchange_info(self) -> Any:
        return self.public_request("GET", "/fapi/v1/exchangeInfo")

    def ticker_price(self, symbol: str) -> Decimal:
        data = self.public_request("GET", "/fapi/v2/ticker/price", {"symbol": symbol})
        return Decimal(str(data["price"]))

    def change_leverage(self, symbol: str, leverage: int) -> Any:
        return self.signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    def new_market_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        client_order_id: str | None = None,
        reduce_only: bool = False,
    ) -> Any:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": format(quantity, "f"),
            "newOrderRespType": "RESULT",
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id[:36]
        if reduce_only:
            params["reduceOnly"] = "true"
        return self.signed_request("POST", "/fapi/v1/order", params)


    def new_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        client_order_id: str | None = None,
        reduce_only: bool = False,
        time_in_force: str = "GTC",
    ) -> Any:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": time_in_force,
            "quantity": format(quantity, "f"),
            "price": format(price, "f"),
            "newOrderRespType": "RESULT",
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id[:36]
        if reduce_only:
            params["reduceOnly"] = "true"
        return self.signed_request("POST", "/fapi/v1/order", params)

    def get_order(
        self,
        symbol: str,
        order_id: int | None = None,
        orig_client_order_id: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id
        return self.signed_request("GET", "/fapi/v1/order", params)

    def cancel_order(
        self,
        symbol: str,
        order_id: int | None = None,
        orig_client_order_id: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id
        return self.signed_request("DELETE", "/fapi/v1/order", params)

    def new_algo_order(self, params: dict[str, Any]) -> Any:
        payload = {"algoType": "CONDITIONAL", **params}
        return self.signed_request("POST", "/fapi/v1/algoOrder", payload)

    def stop_loss_close_position(self, symbol: str, close_side: str, trigger_price: Decimal, working_type: str) -> Any:
        return self.new_algo_order(
            {
                "symbol": symbol,
                "side": close_side,
                "type": "STOP_MARKET",
                "triggerPrice": format(trigger_price, "f"),
                "closePosition": "true",
                "workingType": working_type,
            }
        )

    def take_profit_market(self, symbol: str, close_side: str, trigger_price: Decimal, quantity: Decimal, working_type: str) -> Any:
        return self.new_algo_order(
            {
                "symbol": symbol,
                "side": close_side,
                "type": "TAKE_PROFIT_MARKET",
                "triggerPrice": format(trigger_price, "f"),
                "quantity": format(quantity, "f"),
                "reduceOnly": "true",
                "workingType": working_type,
            }
        )

    def cancel_all_open_orders(self, symbol: str) -> Any:
        return self.signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    def cancel_all_algo_open_orders(self, symbol: str) -> Any:
        return self.signed_request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})

    def open_orders(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else None
        return self.signed_request("GET", "/fapi/v1/openOrders", params)

    def open_algo_orders(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else None
        return self.signed_request("GET", "/fapi/v1/openAlgoOrders", params)

    def position_risk(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else None
        try:
            return self.signed_request("GET", "/fapi/v3/positionRisk", params)
        except BinanceAPIError as exc:
            logger.warning("/fapi/v3/positionRisk unavailable, falling back to /fapi/v2/positionRisk: %s", exc)
            return self.signed_request("GET", "/fapi/v2/positionRisk", params)

    def current_position_amount(self, symbol: str) -> Decimal:
        data = self.position_risk(symbol)
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if str(row.get("symbol", "")).upper() == symbol.upper():
                return Decimal(str(row.get("positionAmt", "0")))
        return Decimal("0")

    def close_position_market(self, symbol: str, position_amt: Decimal, client_order_id: str | None = None) -> Any:
        qty = abs(position_amt)
        if qty <= 0:
            return {"skipped": True, "reason": "no position"}
        close_side = "SELL" if position_amt > 0 else "BUY"
        return self.new_market_order(
            symbol=symbol,
            side=close_side,
            quantity=qty,
            client_order_id=client_order_id,
            reduce_only=True,
        )

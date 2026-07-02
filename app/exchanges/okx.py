from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..okx_auth import format_okx_timestamp, sign_okx_request
from .base import ExchangeClient
from .okx_symbols import inst_id_to_symbol, symbol_to_inst_id, SYMBOL_TO_INST_ID

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)


class OkxAPIError(RuntimeError):
    """OKX REST API returned an error response."""

    def __init__(self, message: str, *, code: str | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class OkxReadOnlyUnsupportedError(RuntimeError):
    """Raised when a write operation is attempted in OKX read-only mode."""


class OkxRestClient:
    """Minimal OKX v5 REST client for signed read-only requests."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.okx_base_url.rstrip("/")
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

    def _build_headers(
        self,
        *,
        timestamp: str,
        method: str,
        request_path: str,
        body: str,
    ) -> dict[str, str]:
        headers = {
            "OK-ACCESS-KEY": self.settings.okx_api_key,
            "OK-ACCESS-SIGN": sign_okx_request(
                self.settings.okx_api_secret,
                timestamp,
                method,
                request_path,
                body,
            ),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.settings.okx_api_passphrase,
            "Content-Type": "application/json",
        }
        if self.settings.okx_simulated_trading:
            headers["x-simulated-trading"] = "1"
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if not self.settings.okx_api_key or not self.settings.okx_api_secret:
            raise OkxAPIError("OKX API key/secret are required for signed OKX requests")
        if not self.settings.okx_api_passphrase:
            raise OkxAPIError("OKX_API_PASSPHRASE is required for signed OKX requests")

        query = ""
        if params:
            query = "?" + urlencode(params, doseq=True)
        request_path = f"{path}{query}"
        body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""
        timestamp = format_okx_timestamp()
        headers = self._build_headers(
            timestamp=timestamp,
            method=method.upper(),
            request_path=request_path,
            body=body_text,
        )
        url = self.base_url + request_path
        resp = self.session.request(
            method=method.upper(),
            url=url,
            headers=headers,
            data=body_text if body_text else None,
            timeout=self.settings.request_timeout,
        )
        if resp.status_code >= 400:
            logger.error("OKX API HTTP error path=%s status=%s body=%s", request_path, resp.status_code, resp.text)
            raise OkxAPIError(f"{resp.status_code} {resp.text}")
        payload = resp.json()
        code = str(payload.get("code", ""))
        if code not in {"0", ""}:
            raise OkxAPIError(
                f"OKX API error code={code} msg={payload.get('msg')}",
                code=code,
                data=payload.get("data"),
            )
        return payload


class OkxExchange(ExchangeClient):
    """OKX read-only exchange adapter (v6.5.1 — no order placement)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = OkxRestClient(settings)

    @property
    def name(self) -> str:
        return "okx"

    @staticmethod
    def _normalize_position_row(row: dict[str, Any], internal_symbol: str) -> dict[str, Any]:
        pos = row.get("pos", "0")
        return {
            **row,
            "symbol": internal_symbol,
            "instId": row.get("instId"),
            "positionAmt": pos,
            "pos": pos,
        }

    def get_positions(self, symbol: str) -> list[dict[str, Any]]:
        inst_id = symbol_to_inst_id(symbol)
        internal_symbol = inst_id_to_symbol(inst_id)
        payload = self._client.request(
            "GET",
            "/api/v5/account/positions",
            params={"instId": inst_id},
        )
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            raise OkxAPIError(f"Unexpected OKX positions payload: {type(rows)!r}")
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("instId", "")) != inst_id:
                continue
            result.append(self._normalize_position_row(row, internal_symbol))
        return result

    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        inst_id = symbol_to_inst_id(symbol)
        payload = self._client.request(
            "GET",
            "/api/v5/trade/orders-pending",
            params={
                "instType": self.settings.okx_inst_type,
                "instId": inst_id,
            },
        )
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            raise OkxAPIError(f"Unexpected OKX open orders payload: {type(rows)!r}")
        return [row for row in rows if isinstance(row, dict)]

    def get_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        inst_id = symbol_to_inst_id(symbol)
        payload = self._client.request(
            "GET",
            "/api/v5/trade/orders-algo-pending",
            params={
                "ordType": "conditional",
                "instType": self.settings.okx_inst_type,
                "instId": inst_id,
            },
        )
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            raise OkxAPIError(f"Unexpected OKX algo orders payload: {type(rows)!r}")
        return [row for row in rows if isinstance(row, dict)]

    def cancel_open_orders(self, symbol: str) -> Any:
        raise OkxReadOnlyUnsupportedError(
            f"OKX read-only mode: cancel_open_orders is not supported for {symbol}"
        )

    def cancel_algo_orders(self, symbol: str) -> Any:
        raise OkxReadOnlyUnsupportedError(
            f"OKX read-only mode: cancel_algo_orders is not supported for {symbol}"
        )

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
        _ = (cancel_before_close, cancel_after_close, wait_seconds)
        inst_id = symbol_to_inst_id(symbol)
        internal_symbol = inst_id_to_symbol(inst_id)
        position_rows = self.get_positions(internal_symbol)
        position_before_row = position_rows[0] if position_rows else None
        return {
            "symbol": internal_symbol,
            "instId": inst_id,
            "position_before": position_before_row,
            "close_side": None,
            "close_quantity": None,
            "close_order": None,
            "position_after": position_before_row,
            "success": False,
            "status": "okx_readonly_not_supported",
            "skip_reason": "okx_readonly_not_supported",
            "message": "OKX read-only mode: close_position is not supported",
            "reason": reason,
            "operator": operator,
        }

    def reconcile(self, *, trigger: str = "manual") -> dict[str, Any]:
        inst_ids = sorted(self.settings.okx_allowed_inst_id_set) or sorted(SYMBOL_TO_INST_ID.values())
        open_position_count = 0
        residual_order_symbol_count = 0
        error_count = 0
        warn_count = 0
        symbol_reports: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for inst_id in inst_ids:
            internal_symbol = inst_id_to_symbol(inst_id)
            symbol_report: dict[str, Any] = {
                "instId": inst_id,
                "symbol": internal_symbol,
            }
            try:
                positions = self.get_positions(internal_symbol)
                open_orders = self.get_open_orders(internal_symbol)
                algo_orders = self.get_algo_orders(internal_symbol)
                non_flat = 0
                for row in positions:
                    amt = Decimal(str(row.get("pos", row.get("positionAmt", "0"))))
                    if abs(amt) > 0:
                        non_flat += 1
                open_position_count += non_flat
                if open_orders or algo_orders:
                    residual_order_symbol_count += 1
                symbol_report.update(
                    {
                        "position_count": len(positions),
                        "non_flat_position_count": non_flat,
                        "open_order_count": len(open_orders),
                        "algo_order_count": len(algo_orders),
                        "level": "OK",
                    }
                )
            except Exception as exc:
                error_count += 1
                message = str(exc)[:500]
                symbol_report.update({"level": "ERROR", "error": message})
                errors.append({"instId": inst_id, "symbol": internal_symbol, "error": message})
            symbol_reports.append(symbol_report)

        level = "ERROR" if error_count else ("WARN" if warn_count else "OK")
        return {
            "success": error_count == 0,
            "level": level,
            "trigger": trigger,
            "exchange": "okx",
            "readonly_mode": bool(self.settings.okx_readonly_mode),
            "summary": {
                "symbols_checked": len(inst_ids),
                "open_position_count": open_position_count,
                "residual_order_symbol_count": residual_order_symbol_count,
                "error_count": error_count,
                "warn_count": warn_count,
            },
            "symbols": symbol_reports,
            "errors": errors,
        }

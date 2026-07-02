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
from ..okx_error_observability import log_okx_order_error
from .base import ExchangeClient
from .okx_sizing import OkxInstrumentMeta, parse_instrument_row
from .okx_symbols import inst_id_to_symbol, symbol_to_inst_id, SYMBOL_TO_INST_ID

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)


class OkxAPIError(RuntimeError):
    """OKX REST API returned an error response."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        code: str | None = None,
        msg: str | None = None,
        data: Any = None,
        s_code: str | None = None,
        s_msg: str | None = None,
        method: str | None = None,
        request_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.msg = msg
        self.data = data
        self.s_code = s_code
        self.s_msg = s_msg
        self.method = method
        self.request_path = request_path

    @staticmethod
    def _first_data_item(payload: dict[str, Any]) -> dict[str, Any] | None:
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None

    @classmethod
    def from_response(
        cls,
        *,
        method: str,
        request_path: str,
        payload: dict[str, Any],
        http_status: int | None = None,
    ) -> OkxAPIError:
        code = str(payload.get("code", "")) if payload.get("code") is not None else None
        msg = payload.get("msg")
        if msg is not None:
            msg = str(msg)
        data = payload.get("data")
        first = cls._first_data_item(payload)
        s_code = None
        s_msg = None
        if first is not None:
            if first.get("sCode") is not None:
                s_code = str(first.get("sCode"))
            if first.get("sMsg") is not None:
                s_msg = str(first.get("sMsg"))

        if s_code not in {None, "", "0"}:
            message = f"OKX API item error sCode={s_code} sMsg={s_msg}"
        elif code not in {None, "", "0"}:
            message = f"OKX API error code={code} msg={msg}"
        else:
            message = f"OKX API error http_status={http_status} code={code} msg={msg}"

        return cls(
            message,
            http_status=http_status,
            code=code,
            msg=msg,
            data=data,
            s_code=s_code,
            s_msg=s_msg,
            method=method.upper(),
            request_path=request_path,
        )

    @classmethod
    def from_http_error(
        cls,
        *,
        method: str,
        request_path: str,
        http_status: int,
        body_text: str,
    ) -> OkxAPIError:
        payload: dict[str, Any] | None = None
        try:
            parsed = json.loads(body_text)
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = None
        if payload is not None:
            err = cls.from_response(
                method=method,
                request_path=request_path,
                payload=payload,
                http_status=http_status,
            )
            if err.code in {None, "", "0"} and err.s_code in {None, "", "0"}:
                err.msg = err.msg or body_text[:500]
            return err
        return cls(
            f"{http_status} {body_text[:500]}",
            http_status=http_status,
            method=method.upper(),
            request_path=request_path,
        )


def _raise_if_okx_payload_error(
    *,
    method: str,
    request_path: str,
    payload: dict[str, Any],
    http_status: int,
) -> None:
    code = str(payload.get("code", ""))
    first = OkxAPIError._first_data_item(payload)
    s_code = str(first.get("sCode")) if first and first.get("sCode") is not None else None
    if code not in {"0", ""}:
        raise OkxAPIError.from_response(
            method=method,
            request_path=request_path,
            payload=payload,
            http_status=http_status,
        )
    if s_code not in {None, "", "0"}:
        raise OkxAPIError.from_response(
            method=method,
            request_path=request_path,
            payload=payload,
            http_status=http_status,
        )


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
            logger.error(
                "OKX API HTTP error method=%s path=%s status=%s",
                method.upper(),
                request_path,
                resp.status_code,
            )
            raise OkxAPIError.from_http_error(
                method=method,
                request_path=request_path,
                http_status=resp.status_code,
                body_text=resp.text,
            )
        payload = resp.json()
        if not isinstance(payload, dict):
            raise OkxAPIError(
                f"Unexpected OKX response type: {type(payload)!r}",
                method=method.upper(),
                request_path=request_path,
                http_status=resp.status_code,
            )
        _raise_if_okx_payload_error(
            method=method,
            request_path=request_path,
            payload=payload,
            http_status=resp.status_code,
        )
        return payload


class OkxExchange(ExchangeClient):
    """OKX adapter: read-only queries + v6.5.3 minimal canary write paths."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = OkxRestClient(settings)
        self._instrument_cache: dict[str, OkxInstrumentMeta] = {}

    def get_instrument(self, inst_id: str) -> OkxInstrumentMeta:
        key = inst_id.upper()
        cached = self._instrument_cache.get(key)
        if cached is not None:
            return cached
        payload = self._client.request(
            "GET",
            "/api/v5/public/instruments",
            params={
                "instType": self.settings.okx_inst_type,
                "instId": key,
            },
        )
        rows = payload.get("data") or []
        if not isinstance(rows, list) or not rows:
            raise OkxAPIError(f"Instrument metadata not found for instId={inst_id}")
        meta = parse_instrument_row(rows[0])
        self._instrument_cache[key] = meta
        return meta

    def get_mark_price(self, inst_id: str) -> Decimal:
        payload = self._client.request(
            "GET",
            "/api/v5/market/ticker",
            params={"instId": inst_id},
        )
        rows = payload.get("data") or []
        if not isinstance(rows, list) or not rows:
            raise OkxAPIError(f"Ticker not found for instId={inst_id}")
        last = rows[0].get("last") or rows[0].get("markPx")
        if last in {None, ""}:
            raise OkxAPIError(f"Mark price missing for instId={inst_id}")
        return Decimal(str(last))

    def place_market_order_minimal(
        self,
        *,
        inst_id: str,
        side: str,
        sz: Decimal,
        td_mode: str,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if self.settings.okx_readonly_mode:
            raise OkxReadOnlyUnsupportedError("OKX_READONLY_MODE=true blocks place_market_order_minimal")
        body: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side.lower(),
            "ordType": "market",
            "sz": format(sz, "f"),
        }
        if client_order_id:
            body["clOrdId"] = client_order_id[:32]
        try:
            payload = self._client.request("POST", "/api/v5/trade/order", body=body)
        except OkxAPIError as exc:
            log_okx_order_error(
                error_stage="open_order",
                exc=exc,
                inst_id=inst_id,
                td_mode=td_mode,
                side=side.lower(),
                sz=sz,
                cl_ord_id=client_order_id,
            )
            raise
        rows = payload.get("data") or []
        return rows[0] if isinstance(rows, list) and rows else payload

    def close_position_market(
        self,
        *,
        inst_id: str,
        td_mode: str,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if self.settings.okx_readonly_mode:
            raise OkxReadOnlyUnsupportedError("OKX_READONLY_MODE=true blocks close_position_market")
        body: dict[str, Any] = {
            "instId": inst_id,
            "mgnMode": td_mode,
            "posSide": "net",
            "autoCxl": True,
        }
        if client_order_id:
            body["clOrdId"] = client_order_id[:32]
        try:
            payload = self._client.request("POST", "/api/v5/trade/close-position", body=body)
        except OkxAPIError as exc:
            log_okx_order_error(
                error_stage="close_order",
                exc=exc,
                inst_id=inst_id,
                td_mode=td_mode,
                side=None,
                sz=None,
                cl_ord_id=client_order_id,
            )
            raise
        rows = payload.get("data") or []
        return rows[0] if isinstance(rows, list) and rows else payload

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
        if self.settings.okx_readonly_mode:
            raise OkxReadOnlyUnsupportedError(
                f"OKX read-only mode: cancel_open_orders is not supported for {symbol}"
            )
        raise OkxReadOnlyUnsupportedError(
            f"OKX v6.5.3 minimal canary does not implement cancel_open_orders for {symbol}"
        )

    def cancel_algo_orders(self, symbol: str) -> Any:
        if self.settings.okx_readonly_mode:
            raise OkxReadOnlyUnsupportedError(
                f"OKX read-only mode: cancel_algo_orders is not supported for {symbol}"
            )
        raise OkxReadOnlyUnsupportedError(
            f"OKX v6.5.3 minimal canary does not implement cancel_algo_orders for {symbol}"
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
        _ = (cancel_before_close, cancel_after_close, wait_seconds, reason, operator)
        inst_id = symbol_to_inst_id(symbol)
        internal_symbol = inst_id_to_symbol(inst_id)
        position_rows = self.get_positions(internal_symbol)
        position_before_row = position_rows[0] if position_rows else None
        if self.settings.okx_readonly_mode:
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
        try:
            close_order = self.close_position_market(
                inst_id=inst_id,
                td_mode=self.settings.okx_td_mode.strip().lower(),
            )
            position_after_rows = self.get_positions(internal_symbol)
            position_after_row = position_after_rows[0] if position_after_rows else None
            pos_amt = Decimal(str((position_after_row or {}).get("pos", "0")))
            closed = pos_amt == 0
            return {
                "symbol": internal_symbol,
                "instId": inst_id,
                "position_before": position_before_row,
                "close_side": "sell",
                "close_quantity": None,
                "close_order": close_order,
                "position_after": position_after_row,
                "success": closed,
                "status": "position_closed" if closed else "position_close_incomplete",
                "reason": reason,
                "operator": operator,
            }
        except Exception as exc:
            return {
                "symbol": internal_symbol,
                "instId": inst_id,
                "position_before": position_before_row,
                "success": False,
                "status": "close_order_failed",
                "close_error": str(exc),
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

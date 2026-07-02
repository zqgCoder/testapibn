"""Offline tests for v6.5.5 OKX canary error observability."""

from __future__ import annotations

import io
import json
import logging
import sys
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.exchanges.okx import OkxAPIError, OkxExchange, OkxRestClient, _raise_if_okx_payload_error
from app.journal import ExecutionStatus, TradeJournal, resolve_execution_status
from app.okx_live_canary import execute_okx_minimal_open_close
from app.stats import TradeStatsService

_TEST_SECRET = "test-secret-for-okx-error-observability"
_CONFIRM = "I_UNDERSTAND_THIS_IS_REAL_MONEY"
_OKX_CREDS = {
    "OKX_API_KEY": "test-okx-key",
    "OKX_API_SECRET": "test-okx-secret",
    "OKX_API_PASSPHRASE": "test-passphrase",
}


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "WEBHOOK_SECRET": _TEST_SECRET,
        "EXCHANGE": "okx",
        "OKX_READONLY_MODE": "false",
        "OKX_LIVE_TRADING_ENABLED": "true",
        "OKX_CONFIRM_PHRASE": _CONFIRM,
        "OKX_EXPECTED_CONFIRM_PHRASE": _CONFIRM,
        "OKX_REQUIRE_ONE_SHOT": "true",
        "OKX_ALLOWED_INST_IDS": "BTC-USDT-SWAP",
        "OKX_TD_MODE": "isolated",
        "OKX_MAX_MARGIN_USDT": "20",
        "OKX_MAX_NOTIONAL_USDT": "100",
        **_OKX_CREDS,
    }
    base.update(overrides)
    return Settings(**base)


def _runtime_control() -> MagicMock:
    rc = MagicMock()
    rc.is_execution_blocked.return_value = (False, {})
    rc.status_payload.return_value = {
        "one_shot": {"enabled": True, "remaining": 1, "consumed_at": None}
    }
    return rc


def _signal_and_payload():
    from app.schemas import TradingViewSignal

    raw = {
        "secret": _TEST_SECRET,
        "source": "local_canary",
        "signal_id": "V655-OKX-ERROR-TEST",
        "symbol": "BTCUSDT",
        "side": "buy",
        "entry_type": "market",
        "risk_mode": "manual",
        "margin_usdt": 20,
        "leverage": 1,
        "close": 90000,
        "position_strategy": "replace",
    }
    return TradingViewSignal.model_validate(raw), raw


class OkxAPIErrorStructureTests(unittest.TestCase):
    def test_http_401_structured_error(self) -> None:
        err = OkxAPIError.from_http_error(
            method="POST",
            request_path="/api/v5/trade/order",
            http_status=401,
            body_text='{"code":"50111","msg":"Invalid OK-ACCESS-KEY"}',
        )
        self.assertEqual(err.http_status, 401)
        self.assertEqual(err.code, "50111")
        self.assertEqual(err.msg, "Invalid OK-ACCESS-KEY")
        self.assertEqual(err.method, "POST")
        self.assertEqual(err.request_path, "/api/v5/trade/order")

    def test_http_403_structured_error(self) -> None:
        err = OkxAPIError.from_http_error(
            method="GET",
            request_path="/api/v5/account/balance",
            http_status=403,
            body_text='{"code":"50113","msg":"Invalid Sign"}',
        )
        self.assertEqual(err.http_status, 403)
        self.assertEqual(err.code, "50113")
        self.assertIn("Invalid Sign", err.msg or "")

    def test_top_level_code_not_zero(self) -> None:
        payload = {"code": "1", "msg": "All operations failed", "data": []}
        with self.assertRaises(OkxAPIError) as ctx:
            _raise_if_okx_payload_error(
                method="POST",
                request_path="/api/v5/trade/order",
                payload=payload,
                http_status=200,
            )
        err = ctx.exception
        self.assertEqual(err.code, "1")
        self.assertEqual(err.msg, "All operations failed")
        self.assertEqual(err.data, [])

    def test_data_scode_not_zero(self) -> None:
        payload = {
            "code": "1",
            "msg": "All operations failed",
            "data": [{"sCode": "51008", "sMsg": "Order failed"}],
        }
        with self.assertRaises(OkxAPIError) as ctx:
            _raise_if_okx_payload_error(
                method="POST",
                request_path="/api/v5/trade/order",
                payload=payload,
                http_status=200,
            )
        err = ctx.exception
        self.assertEqual(err.s_code, "51008")
        self.assertEqual(err.s_msg, "Order failed")


class OkxCanaryFailureResultTests(unittest.TestCase):
    def _exchange(self) -> MagicMock:
        from app.exchanges.okx_sizing import OkxInstrumentMeta

        exchange = MagicMock(spec=OkxExchange)
        exchange.get_mark_price.return_value = Decimal("61500")
        exchange.get_instrument.return_value = OkxInstrumentMeta(
            inst_id="BTC-USDT-SWAP",
            inst_type="SWAP",
            lot_sz=Decimal("0.01"),
            min_sz=Decimal("0.01"),
            ct_val=Decimal("0.01"),
            ct_mult=Decimal("1"),
            tick_sz=Decimal("0.1"),
        )
        exchange.reconcile.return_value = {
            "success": True,
            "summary": {
                "open_position_count": 0,
                "residual_order_symbol_count": 0,
                "error_count": 0,
                "warn_count": 0,
            },
        }
        return exchange

    def test_open_order_failure_returns_okx_canary_open_failed(self) -> None:
        settings = _settings()
        signal, payload = _signal_and_payload()
        exchange = self._exchange()
        exchange.place_market_order_minimal.side_effect = OkxAPIError.from_response(
            method="POST",
            request_path="/api/v5/trade/order",
            payload={
                "code": "1",
                "msg": "All operations failed",
                "data": [{"sCode": "51008", "sMsg": "Insufficient USDT margin"}],
            },
            http_status=200,
        )
        result = execute_okx_minimal_open_close(
            exchange,
            settings,
            signal,
            payload,
            signal_key="open-fail-key",
            runtime_control=_runtime_control(),
        )
        self.assertEqual(result["okx_canary"]["status"], ExecutionStatus.OKX_CANARY_OPEN_FAILED)
        self.assertEqual(result["error_stage"], "open_order")
        self.assertEqual(result["okx_error"]["okx_scode"], "51008")
        self.assertEqual(result["okx_error"]["okx_smsg"], "Insufficient USDT margin")
        self.assertEqual(
            resolve_execution_status(result),
            ExecutionStatus.OKX_CANARY_OPEN_FAILED,
        )
        exchange.close_position_market.assert_not_called()

    def test_close_order_failure_returns_okx_canary_close_failed(self) -> None:
        settings = _settings()
        signal, payload = _signal_and_payload()
        exchange = self._exchange()
        exchange.place_market_order_minimal.return_value = {"ordId": "open-1"}
        exchange.close_position_market.side_effect = OkxAPIError.from_response(
            method="POST",
            request_path="/api/v5/trade/close-position",
            payload={
                "code": "1",
                "msg": "All operations failed",
                "data": [{"sCode": "51119", "sMsg": "No position to close"}],
            },
            http_status=200,
        )
        result = execute_okx_minimal_open_close(
            exchange,
            settings,
            signal,
            payload,
            signal_key="close-fail-key",
            runtime_control=_runtime_control(),
        )
        self.assertEqual(result["okx_canary"]["status"], ExecutionStatus.OKX_CANARY_CLOSE_FAILED)
        self.assertEqual(result["error_stage"], "close_order")
        self.assertEqual(result["okx_error"]["okx_scode"], "51119")
        self.assertEqual(
            resolve_execution_status(result),
            ExecutionStatus.OKX_CANARY_CLOSE_FAILED,
        )

    def test_reconcile_failure_returns_okx_canary_reconcile_failed(self) -> None:
        settings = _settings()
        signal, payload = _signal_and_payload()
        exchange = self._exchange()
        exchange.place_market_order_minimal.return_value = {"ordId": "open-1"}
        exchange.close_position_market.return_value = {"ordId": "close-1"}
        exchange.reconcile.return_value = {
            "success": False,
            "summary": {
                "open_position_count": 1,
                "residual_order_symbol_count": 0,
                "error_count": 0,
                "warn_count": 0,
            },
        }
        result = execute_okx_minimal_open_close(
            exchange,
            settings,
            signal,
            payload,
            signal_key="reconcile-fail-key",
            runtime_control=_runtime_control(),
        )
        self.assertEqual(result["okx_canary"]["status"], ExecutionStatus.OKX_CANARY_RECONCILE_FAILED)
        self.assertEqual(result["error_stage"], "reconcile")
        self.assertEqual(
            resolve_execution_status(result),
            ExecutionStatus.OKX_CANARY_RECONCILE_FAILED,
        )


class OkxOrderLoggingTests(unittest.TestCase):
    def test_open_order_failure_logs_safe_summary_without_secret(self) -> None:
        settings = _settings()
        exchange = OkxExchange(settings)
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        logger = logging.getLogger("app.okx_error_observability")
        logger.addHandler(handler)
        logger.setLevel(logging.ERROR)
        try:
            with patch.object(
                exchange._client,
                "request",
                side_effect=OkxAPIError.from_response(
                    method="POST",
                    request_path="/api/v5/trade/order",
                    payload={
                        "code": "1",
                        "msg": "All operations failed",
                        "data": [{"sCode": "51008", "sMsg": "Insufficient USDT margin"}],
                    },
                    http_status=200,
                ),
            ):
                with self.assertRaises(OkxAPIError):
                    exchange.place_market_order_minimal(
                        inst_id="BTC-USDT-SWAP",
                        side="buy",
                        sz=Decimal("0.01"),
                        td_mode="isolated",
                        client_order_id="okxcanaryopen-test123",
                    )
        finally:
            logger.removeHandler(handler)

        output = log_stream.getvalue()
        self.assertIn("error_stage=open_order", output)
        self.assertIn("instId=BTC-USDT-SWAP", output)
        self.assertIn("okx_scode=51008", output)
        self.assertIn("request_path=/api/v5/trade/order", output)
        self.assertNotIn("test-okx-secret", output)
        self.assertNotIn("test-passphrase", output)
        self.assertNotIn("OK-ACCESS-SIGN", output)


class JournalDashboardObservabilityTests(unittest.TestCase):
    def test_execution_brief_includes_okx_error_fields_without_secret(self) -> None:
        result = {
            "orders": {},
            "skipped": False,
            "exchange": "okx",
            "error_stage": "open_order",
            "error_summary": "open_order: 51008 Insufficient USDT margin",
            "okx_error": {
                "error_stage": "open_order",
                "error_summary": "open_order: 51008 Insufficient USDT margin",
                "okx_code": "1",
                "okx_msg": "All operations failed",
                "okx_scode": "51008",
                "okx_smsg": "Insufficient USDT margin",
                "request_path": "/api/v5/trade/order",
            },
            "okx_canary": {
                "status": ExecutionStatus.OKX_CANARY_OPEN_FAILED,
                "success": False,
            },
        }
        row = {
            "id": 1,
            "signal_key": "k1",
            "signal_id": "S1",
            "symbol": "BTCUSDT",
            "side": "buy",
            "status": ExecutionStatus.OKX_CANARY_OPEN_FAILED,
            "error_message": "open_order: 51008 Insufficient USDT margin",
            "result_json": json.dumps(result),
            "raw_signal_json": json.dumps({"secret": _TEST_SECRET}),
        }
        brief = TradeStatsService.execution_brief(row)
        detail = TradeStatsService.execution_detail(row)
        combined = json.dumps(brief) + json.dumps(detail)
        self.assertEqual(brief["error_stage"], "open_order")
        self.assertEqual(brief["okx_scode"], "51008")
        self.assertEqual(brief["okx_smsg"], "Insufficient USDT margin")
        self.assertNotIn(_TEST_SECRET, combined)
        self.assertNotIn("test-okx-secret", combined)

    def test_journal_persist_execution_sets_error_message_from_okx_error(self) -> None:
        signal, payload = _signal_and_payload()
        store = MagicMock()
        store.insert_execution.return_value = 42
        journal = TradeJournal(store)
        result = {
            "orders": {},
            "skipped": False,
            "error_stage": "open_order",
            "error_summary": "open_order: 51008 Insufficient USDT margin",
            "okx_error": {
                "error_stage": "open_order",
                "error_summary": "open_order: 51008 Insufficient USDT margin",
                "okx_code": "1",
                "okx_msg": "All operations failed",
                "okx_scode": "51008",
                "okx_smsg": "Insufficient USDT margin",
            },
            "okx_canary": {
                "status": ExecutionStatus.OKX_CANARY_OPEN_FAILED,
                "success": False,
            },
            "entry_summary": {"filled_qty": "0", "entry_type": "market"},
            "protection_summary": {},
        }
        journal.persist_execution(signal, "persist-key", payload, result)
        insert_payload = store.insert_execution.call_args.args[0]
        self.assertEqual(insert_payload["status"], ExecutionStatus.OKX_CANARY_OPEN_FAILED)
        self.assertIn("51008", insert_payload["error_message"] or "")


if __name__ == "__main__":
    unittest.main()

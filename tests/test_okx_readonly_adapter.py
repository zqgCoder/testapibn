"""Offline unit tests for OKX read-only adapter (no network, no real .env)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import unittest
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.exchanges import OkxExchange, create_exchange_client
from app.exchanges.okx import OkxRestClient
from app.exchanges.okx_symbols import symbol_to_inst_id
from app.okx_auth import build_okx_prehash, sign_okx_request
from app.trader import Trader

_TEST_SECRET = "test-secret-for-okx-readonly-unit-tests"
_OKX_CREDS = {
    "OKX_API_KEY": "test-okx-key",
    "OKX_API_SECRET": "test-okx-secret",
    "OKX_API_PASSPHRASE": "test-passphrase",
}


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "WEBHOOK_SECRET": _TEST_SECRET,
        "EXCHANGE": "okx",
        **_OKX_CREDS,
    }
    base.update(overrides)
    return Settings(**base)


class OkxAuthTests(unittest.TestCase):
    def test_prehash_includes_query_string_in_request_path(self) -> None:
        timestamp = "2020-12-08T09:08:57.715Z"
        request_path = "/api/v5/account/positions?instId=BTC-USDT-SWAP"
        prehash = build_okx_prehash(timestamp, "GET", request_path, "")
        self.assertEqual(
            prehash,
            "2020-12-08T09:08:57.715ZGET/api/v5/account/positions?instId=BTC-USDT-SWAP",
        )

    def test_sign_matches_hmac_sha256_base64(self) -> None:
        secret = "super-secret"
        timestamp = "2020-12-08T09:08:57.715Z"
        request_path = "/api/v5/account/balance?ccy=BTC"
        prehash = build_okx_prehash(timestamp, "GET", request_path, "")
        expected = base64.b64encode(
            hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        self.assertEqual(
            sign_okx_request(secret, timestamp, "GET", request_path, ""),
            expected,
        )

    def test_method_is_uppercased_in_prehash(self) -> None:
        prehash = build_okx_prehash("2020-12-08T09:08:57.715Z", "get", "/api/v5/account/balance", "")
        self.assertTrue(prehash.startswith("2020-12-08T09:08:57.715ZGET"))


class OkxRestClientHeaderTests(unittest.TestCase):
    def test_simulated_trading_header_present_when_enabled(self) -> None:
        settings = _settings(OKX_SIMULATED_TRADING="true")
        client = OkxRestClient(settings)
        headers = client._build_headers(
            timestamp="2020-12-08T09:08:57.715Z",
            method="GET",
            request_path="/api/v5/account/balance",
            body="",
        )
        self.assertEqual(headers["x-simulated-trading"], "1")

    def test_simulated_trading_header_absent_when_disabled(self) -> None:
        settings = _settings(OKX_SIMULATED_TRADING="false")
        client = OkxRestClient(settings)
        headers = client._build_headers(
            timestamp="2020-12-08T09:08:57.715Z",
            method="GET",
            request_path="/api/v5/account/balance",
            body="",
        )
        self.assertNotIn("x-simulated-trading", headers)


class OkxSymbolMappingTests(unittest.TestCase):
    def test_btcusdt_maps_to_btc_usdt_swap(self) -> None:
        self.assertEqual(symbol_to_inst_id("BTCUSDT"), "BTC-USDT-SWAP")

    def test_ethusdt_maps_to_eth_usdt_swap(self) -> None:
        self.assertEqual(symbol_to_inst_id("ETHUSDT"), "ETH-USDT-SWAP")


class OkxExchangeFactoryTests(unittest.TestCase):
    def test_factory_returns_okx_exchange(self) -> None:
        settings = _settings()
        adapter = create_exchange_client(settings)
        self.assertIsInstance(adapter, OkxExchange)
        self.assertEqual(adapter.name, "okx")


class OkxReadOnlyExchangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = _settings()
        self.adapter = OkxExchange(self.settings)

    def test_cancel_open_orders_rejected(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self.adapter.cancel_open_orders("BTCUSDT")
        self.assertIn("read-only", str(ctx.exception).lower())

    def test_close_position_returns_readonly_not_supported(self) -> None:
        with patch.object(self.adapter, "get_positions", return_value=[]):
            result = self.adapter.close_position("BTCUSDT", reason="t", operator="u")
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "okx_readonly_not_supported")
        self.assertEqual(result["skip_reason"], "okx_readonly_not_supported")

    def test_reconcile_counts_errors_on_api_failure(self) -> None:
        settings = _settings(OKX_ALLOWED_INST_IDS="BTC-USDT-SWAP")
        adapter = OkxExchange(settings)
        with patch.object(adapter._client, "request", side_effect=RuntimeError("api down")):
            report = adapter.reconcile(trigger="unit_test")
        summary = report["summary"]
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["symbols_checked"], 1)
        self.assertFalse(report["success"])
        self.assertEqual(report["level"], "ERROR")
        self.assertEqual(report["exchange"], "okx")
        self.assertEqual(len(report["errors"]), 1)
        error_row = report["errors"][0]
        self.assertEqual(error_row["instId"], "BTC-USDT-SWAP")
        self.assertEqual(error_row["symbol"], "BTCUSDT")
        self.assertEqual(error_row["error"], "api down")
        self.assertEqual(len(report["symbols"]), 1)
        symbol_row = report["symbols"][0]
        self.assertEqual(symbol_row["instId"], "BTC-USDT-SWAP")
        self.assertEqual(symbol_row["symbol"], "BTCUSDT")
        self.assertEqual(symbol_row["level"], "ERROR")
        self.assertEqual(symbol_row["error"], "api down")

    def test_reconcile_clean_account_zero_errors(self) -> None:
        def fake_request(method: str, path: str, *, params=None, body=None):
            if path == "/api/v5/account/positions":
                return {"code": "0", "data": [{"instId": params["instId"], "pos": "0"}]}
            return {"code": "0", "data": []}

        with patch.object(self.adapter._client, "request", side_effect=fake_request):
            report = self.adapter.reconcile(trigger="unit_test")
        self.assertEqual(report["summary"]["error_count"], 0)
        self.assertTrue(report["success"])
        self.assertEqual(report["summary"]["open_position_count"], 0)
        self.assertEqual(report["summary"]["residual_order_symbol_count"], 0)

    def test_get_positions_uses_inst_id_query(self) -> None:
        with patch.object(self.adapter._client, "request") as mock_request:
            mock_request.return_value = {
                "code": "0",
                "data": [{"instId": "BTC-USDT-SWAP", "pos": "0.01"}],
            }
            rows = self.adapter.get_positions("BTCUSDT")
        mock_request.assert_called_once()
        self.assertEqual(
            mock_request.call_args.kwargs["params"]["instId"],
            "BTC-USDT-SWAP",
        )
        self.assertEqual(rows[0]["symbol"], "BTCUSDT")
        self.assertEqual(rows[0]["positionAmt"], "0.01")


class OkxTraderExecutionTests(unittest.TestCase):
    def test_execute_rejects_okx_with_skip_reason(self) -> None:
        settings = _settings()
        client = MagicMock()
        rules = MagicMock()
        trader = Trader(settings, client, rules)
        signal = MagicMock()
        signal.signal_id = "OKX-TEST-001"
        result = trader.execute(signal, "okx-test-key", {"symbol": "BTCUSDT", "side": "buy"})
        self.assertTrue(result["skipped"])
        self.assertEqual(result["skip_reason"], "okx_execution_not_implemented")
        client.new_market_order.assert_not_called()

    def test_binance_execute_not_blocked_by_okx_guard(self) -> None:
        settings = Settings(
            WEBHOOK_SECRET=_TEST_SECRET,
            EXCHANGE="binance",
            BINANCE_BASE_URL="https://demo-fapi.binance.com",
            ENABLE_TRADING="false",
        )
        client = MagicMock()
        rules = MagicMock()
        runtime = MagicMock()
        runtime.is_execution_blocked.return_value = (True, {"locked": True})
        trader = Trader(settings, client, rules, runtime_control=runtime)
        signal = MagicMock()
        result = trader.execute(signal, "binance-test-key", {})
        self.assertEqual(result.get("skip_reason"), "runtime_locked")
        self.assertNotEqual(result.get("skip_reason"), "okx_execution_not_implemented")


if __name__ == "__main__":
    unittest.main()

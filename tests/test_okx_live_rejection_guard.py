"""Offline unit tests for app.okx_guard (no network, no real .env)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import Settings
from app.exchanges import OkxExchange
from app.okx_guard import (
    build_okx_guard_status,
    okx_guard_applies,
    validate_okx_guard_before_plan,
)
from app.schemas import TradingViewSignal
from app.trader import Trader

_TEST_SECRET = "test-secret-for-okx-guard-unit-tests"
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
        "OKX_READONLY_MODE": "true",
        "OKX_LIVE_TRADING_ENABLED": "false",
        "OKX_ALLOWED_INST_IDS": "BTC-USDT-SWAP",
        **_OKX_CREDS,
    }
    base.update(overrides)
    return Settings(**base)


def _runtime_control(*, one_shot_enabled: bool, remaining: int = 1) -> MagicMock:
    rc = MagicMock()
    rc.settings = SimpleNamespace(runtime_control_enabled=True)
    rc.status_payload.return_value = {
        "one_shot": {
            "enabled": one_shot_enabled,
            "remaining": remaining,
            "consumed_at": None,
        }
    }
    return rc


def _signal_and_payload(**overrides: object) -> tuple[TradingViewSignal, dict]:
    payload: dict = {
        "secret": _TEST_SECRET,
        "source": "local_canary",
        "signal_id": "V652-OKX-GUARD-TEST-001",
        "symbol": "BTCUSDT",
        "side": "buy",
        "entry_type": "market",
        "risk_mode": "fixed_usdt",
        "risk_usdt": 1,
        "margin_usdt": 20,
        "sl": 59000,
        "tps": [{"price": 60000, "qty_pct": 1}],
        "position_strategy": "replace",
    }
    payload.update(overrides)
    return TradingViewSignal.model_validate(payload), payload


class OkxGuardValidationTests(unittest.TestCase):
    def test_readonly_mode_rejects(self) -> None:
        settings = _settings(OKX_READONLY_MODE="true")
        signal, payload = _signal_and_payload()
        rejection = validate_okx_guard_before_plan(settings, signal, payload)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "okx_readonly_mode")

    def test_live_trading_disabled_rejects(self) -> None:
        settings = _settings(OKX_READONLY_MODE="false", OKX_LIVE_TRADING_ENABLED="false")
        signal, payload = _signal_and_payload()
        rejection = validate_okx_guard_before_plan(settings, signal, payload)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "okx_live_trading_disabled")

    def test_confirm_phrase_invalid_rejects(self) -> None:
        settings = _settings(
            OKX_READONLY_MODE="false",
            OKX_LIVE_TRADING_ENABLED="true",
            OKX_CONFIRM_PHRASE="wrong",
            OKX_EXPECTED_CONFIRM_PHRASE=_CONFIRM,
        )
        signal, payload = _signal_and_payload()
        rejection = validate_okx_guard_before_plan(settings, signal, payload)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "okx_confirm_phrase_invalid")

    def test_one_shot_required_rejects(self) -> None:
        settings = _settings(
            OKX_READONLY_MODE="false",
            OKX_LIVE_TRADING_ENABLED="true",
            OKX_CONFIRM_PHRASE=_CONFIRM,
            OKX_EXPECTED_CONFIRM_PHRASE=_CONFIRM,
            OKX_REQUIRE_ONE_SHOT="true",
        )
        signal, payload = _signal_and_payload()
        rejection = validate_okx_guard_before_plan(
            settings,
            signal,
            payload,
            runtime_control=_runtime_control(one_shot_enabled=False),
        )
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "okx_one_shot_required")

    def test_symbol_not_allowed_rejects(self) -> None:
        settings = _settings(
            OKX_READONLY_MODE="false",
            OKX_LIVE_TRADING_ENABLED="true",
            OKX_CONFIRM_PHRASE=_CONFIRM,
            OKX_EXPECTED_CONFIRM_PHRASE=_CONFIRM,
            OKX_REQUIRE_ONE_SHOT="false",
            OKX_ALLOWED_INST_IDS="BTC-USDT-SWAP",
        )
        signal, payload = _signal_and_payload(symbol="ETHUSDT")
        rejection = validate_okx_guard_before_plan(settings, signal, payload)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "okx_symbol_not_allowed")

    def test_source_not_allowed_when_other_checks_pass(self) -> None:
        settings = _settings(
            OKX_READONLY_MODE="false",
            OKX_LIVE_TRADING_ENABLED="true",
            OKX_CONFIRM_PHRASE=_CONFIRM,
            OKX_EXPECTED_CONFIRM_PHRASE=_CONFIRM,
            OKX_REQUIRE_ONE_SHOT="false",
        )
        signal, payload = _signal_and_payload(source="tradingview")
        rejection = validate_okx_guard_before_plan(settings, signal, payload)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "okx_source_not_allowed")

    def test_binance_exchange_not_guarded(self) -> None:
        settings = Settings(
            WEBHOOK_SECRET=_TEST_SECRET,
            EXCHANGE="binance",
            BINANCE_BASE_URL="https://demo-fapi.binance.com",
        )
        self.assertFalse(okx_guard_applies(settings))
        signal, payload = _signal_and_payload()
        self.assertIsNone(validate_okx_guard_before_plan(settings, signal, payload))


class OkxGuardTraderIntegrationTests(unittest.TestCase):
    def test_execute_rejection_does_not_call_okx_write_apis(self) -> None:
        settings = _settings()
        client = MagicMock()
        rules = MagicMock()
        exchange = MagicMock(spec=OkxExchange)
        exchange.name = "okx"
        trader = Trader(settings, client, rules, exchange_client=exchange)
        signal, payload = _signal_and_payload()
        result = trader.execute(signal, "okx-guard-test", payload)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["skip_reason"], "okx_readonly_mode")
        exchange.cancel_open_orders.assert_not_called()
        exchange.cancel_algo_orders.assert_not_called()
        exchange.close_position.assert_not_called()
        client.new_market_order.assert_not_called()

    def test_binance_execute_not_affected_by_okx_guard(self) -> None:
        settings = Settings(
            WEBHOOK_SECRET=_TEST_SECRET,
            EXCHANGE="binance",
            BINANCE_BASE_URL="https://demo-fapi.binance.com",
        )
        client = MagicMock()
        rules = MagicMock()
        runtime = MagicMock()
        runtime.is_execution_blocked.return_value = (True, {"locked": True})
        trader = Trader(settings, client, rules, runtime_control=runtime)
        signal, payload = _signal_and_payload()
        result = trader.execute(signal, "binance-test", payload)
        self.assertEqual(result.get("skip_reason"), "runtime_locked")
        self.assertNotIn("okx_guard", result)


class OkxGuardStatusTests(unittest.TestCase):
    def test_build_status_contains_required_fields(self) -> None:
        settings = _settings()
        status = build_okx_guard_status(settings, _runtime_control(one_shot_enabled=False))
        self.assertEqual(status["exchange"], "okx")
        self.assertTrue(status["readonly_mode"])
        self.assertFalse(status["okx_live_trading_enabled"])
        self.assertIn("BTC-USDT-SWAP", status["okx_allowed_inst_ids"])


if __name__ == "__main__":
    unittest.main()

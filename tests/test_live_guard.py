"""Offline unit tests for app.live_guard (no Binance, no network, no real .env)."""

from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import Settings
from app.live_guard import (
    build_live_guard_status,
    live_guard_applies,
    validate_live_guard_after_plan,
    validate_live_guard_before_plan,
)
from app.risk import TradePlan
from app.schemas import TradingViewSignal

_TEST_SECRET = "test-secret-for-live-guard-unit-tests"
_LIVE_URL = "https://fapi.binance.com"
_DEMO_URL = "https://demo-fapi.binance.com"
_CONFIRM = "I_UNDERSTAND_THIS_IS_REAL_MONEY"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "WEBHOOK_SECRET": _TEST_SECRET,
        "BINANCE_BASE_URL": _LIVE_URL,
        "LIVE_TRADING_ENABLED": "true",
        "LIVE_CONFIRM_PHRASE": _CONFIRM,
        "LIVE_EXPECTED_CONFIRM_PHRASE": _CONFIRM,
        "LIVE_ALLOWED_SYMBOLS": "BTCUSDT",
        "LIVE_MAX_RISK_USDT": "1",
        "LIVE_MAX_MARGIN_USDT": "20",
        "LIVE_MAX_POSITION_NOTIONAL_USDT": "100",
        "LIVE_REQUIRE_ONE_SHOT": "true",
        "LIVE_REJECT_TRADINGVIEW_BY_DEFAULT": "true",
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
        "source": "canary_manual",
        "signal_id": "CANARY-OFFLINE-001",
        "symbol": "BTCUSDT",
        "side": "sell",
        "entry_type": "market",
        "risk_mode": "fixed_usdt",
        "risk_usdt": 0.5,
        "margin_usdt": 10,
        "sl": 59000,
        "tps": [{"price": 58000, "qty_pct": 1}],
    }
    payload.update(overrides)
    return TradingViewSignal.model_validate(payload), payload


def _sample_plan(*, notional: str = "50") -> TradePlan:
    return TradePlan(
        symbol="BTCUSDT",
        side="SELL",
        close_side="BUY",
        entry_ref_price=Decimal("60000"),
        notional_usdt=Decimal(notional),
        margin_usdt=Decimal("10"),
        quantity=Decimal("0.001"),
        leverage=2,
        stop_loss_price=Decimal("61000"),
        take_profits=[],
        working_type="MARK_PRICE",
        dry_run=False,
        risk_mode="fixed_usdt",
        target_risk_usdt=Decimal("0.5"),
    )


class LiveGuardOfflineTests(unittest.TestCase):
    def test_demo_env_guard_inactive(self) -> None:
        settings = _settings(BINANCE_BASE_URL=_DEMO_URL)
        signal, payload = _signal_and_payload()

        self.assertFalse(live_guard_applies(settings))
        self.assertIsNone(validate_live_guard_before_plan(settings, signal, payload))
        self.assertIsNone(
            validate_live_guard_after_plan(settings, signal, _sample_plan(notional="9999"))
        )

        status = build_live_guard_status(settings, None)
        self.assertFalse(status["guard_active"])
        self.assertEqual(status["binance_env"], "demo")
        summary = status["would_allow_execution"]
        self.assertFalse(summary["applies"])
        self.assertTrue(summary["allowed"])

    def test_live_trading_disabled(self) -> None:
        settings = _settings(LIVE_TRADING_ENABLED="false")
        signal, payload = _signal_and_payload()
        rejection = validate_live_guard_before_plan(settings, signal, payload)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "live_guard_trading_disabled")

    def test_confirm_phrase_invalid(self) -> None:
        settings = _settings(LIVE_CONFIRM_PHRASE="WRONG_PHRASE")
        signal, payload = _signal_and_payload()
        rejection = validate_live_guard_before_plan(settings, signal, payload)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "live_guard_confirm_phrase_invalid")

    def test_symbol_not_allowed(self) -> None:
        settings = _settings()
        signal, payload = _signal_and_payload(symbol="ETHUSDT")
        rc = _runtime_control(one_shot_enabled=True)
        rejection = validate_live_guard_before_plan(settings, signal, payload, runtime_control=rc)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "live_guard_symbol_not_allowed")

    def test_risk_too_large(self) -> None:
        settings = _settings()
        signal, payload = _signal_and_payload(risk_usdt=3)
        rc = _runtime_control(one_shot_enabled=True)
        rejection = validate_live_guard_before_plan(settings, signal, payload, runtime_control=rc)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "live_guard_risk_too_large")

    def test_margin_too_large(self) -> None:
        settings = _settings()
        signal, payload = _signal_and_payload(margin_usdt=25)
        rc = _runtime_control(one_shot_enabled=True)
        rejection = validate_live_guard_before_plan(settings, signal, payload, runtime_control=rc)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "live_guard_margin_too_large")

    def test_tradingview_rejected(self) -> None:
        settings = _settings()
        signal, payload = _signal_and_payload(source="tradingview", signal_id="TV-LIVE-GUARD-TEST")
        rc = _runtime_control(one_shot_enabled=True)
        rejection = validate_live_guard_before_plan(settings, signal, payload, runtime_control=rc)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "live_guard_tradingview_rejected")

    def test_one_shot_required(self) -> None:
        settings = _settings()
        signal, payload = _signal_and_payload()
        rc = _runtime_control(one_shot_enabled=False, remaining=0)
        rejection = validate_live_guard_before_plan(settings, signal, payload, runtime_control=rc)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "live_guard_one_shot_required")

    def test_notional_too_large_after_plan(self) -> None:
        settings = _settings()
        signal, _payload = _signal_and_payload()
        rejection = validate_live_guard_after_plan(settings, signal, _sample_plan(notional="150"))
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "live_guard_notional_too_large")

    def test_live_passes_when_all_guards_satisfied(self) -> None:
        settings = _settings()
        signal, payload = _signal_and_payload()
        rc = _runtime_control(one_shot_enabled=True)
        self.assertIsNone(
            validate_live_guard_before_plan(settings, signal, payload, runtime_control=rc)
        )
        self.assertIsNone(
            validate_live_guard_after_plan(settings, signal, _sample_plan(notional="50"))
        )


if __name__ == "__main__":
    unittest.main()

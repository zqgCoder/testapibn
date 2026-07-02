"""Offline tests for app.live_canary_preflight (no Binance, no network, no real .env)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import Settings
from app.live_canary_preflight import (
    CanaryMarketSnapshot,
    build_live_canary_preflight,
    evaluate_canary_blocking_reasons,
)
from tests.test_live_guard import _CONFIRM, _DEMO_URL, _LIVE_URL, _settings

_EMPTY_RECONCILE = {"summary": {"error_count": 0, "warn_count": 0}}


def _runtime(*, locked: bool = True, one_shot_enabled: bool = False) -> MagicMock:
    rc = MagicMock()
    rc.settings = SimpleNamespace(runtime_control_enabled=True)
    rc.status_payload.return_value = {
        "enabled": True,
        "locked": locked,
        "effective_locked": locked and not one_shot_enabled,
        "reason": "test lock" if locked else None,
        "one_shot": {
            "enabled": one_shot_enabled,
            "remaining": 1 if one_shot_enabled else 0,
            "consumed_at": None,
        },
    }
    return rc


def _empty_market() -> CanaryMarketSnapshot:
    return CanaryMarketSnapshot(positions=[], algo_orders=[], open_orders=[])


def _ready_preflight(**settings_overrides: object):
    settings = _settings(**settings_overrides)
    rc = _runtime(locked=True, one_shot_enabled=False)
    return build_live_canary_preflight(
        settings,
        rc,
        market=_empty_market(),
        reconcile_report=_EMPTY_RECONCILE,
    )


class LiveCanaryPreflightTests(unittest.TestCase):
    def test_demo_not_live_environment(self) -> None:
        settings = _settings(BINANCE_BASE_URL=_DEMO_URL)
        result = build_live_canary_preflight(settings, _runtime(), market=_empty_market())
        self.assertFalse(result["canary_ready"])
        self.assertIn("not_live_environment", result["blocking_reasons"])

    def test_live_trading_disabled(self) -> None:
        result = _ready_preflight(LIVE_TRADING_ENABLED="false")
        self.assertIn("live_trading_disabled", result["blocking_reasons"])

    def test_confirm_phrase_invalid(self) -> None:
        result = _ready_preflight(LIVE_CONFIRM_PHRASE="WRONG")
        self.assertIn("live_confirm_phrase_invalid", result["blocking_reasons"])

    def test_live_max_risk_too_high(self) -> None:
        result = _ready_preflight(LIVE_MAX_RISK_USDT="2")
        self.assertIn("live_max_risk_too_high", result["blocking_reasons"])

    def test_live_max_margin_too_high(self) -> None:
        result = _ready_preflight(LIVE_MAX_MARGIN_USDT="25")
        self.assertIn("live_max_margin_too_high", result["blocking_reasons"])

    def test_live_max_notional_too_high(self) -> None:
        result = _ready_preflight(LIVE_MAX_POSITION_NOTIONAL_USDT="150")
        self.assertIn("live_max_notional_too_high", result["blocking_reasons"])

    def test_runtime_not_locked(self) -> None:
        settings = _settings()
        rc = _runtime(locked=False)
        result = build_live_canary_preflight(
            settings, rc, market=_empty_market(), reconcile_report=_EMPTY_RECONCILE
        )
        self.assertIn("runtime_not_locked", result["blocking_reasons"])

    def test_one_shot_active(self) -> None:
        settings = _settings()
        rc = _runtime(locked=True, one_shot_enabled=True)
        result = build_live_canary_preflight(
            settings, rc, market=_empty_market(), reconcile_report=_EMPTY_RECONCILE
        )
        self.assertIn("one_shot_active", result["blocking_reasons"])

    def test_btcusdt_position_not_flat(self) -> None:
        settings = _settings()
        rc = _runtime(locked=True)
        market = CanaryMarketSnapshot(positions=[{"positionAmt": "0.001"}])
        result = build_live_canary_preflight(
            settings, rc, market=market, reconcile_report=_EMPTY_RECONCILE
        )
        self.assertIn("btcusdt_position_not_flat", result["blocking_reasons"])

    def test_btcusdt_algo_orders_exist(self) -> None:
        settings = _settings()
        rc = _runtime(locked=True)
        market = CanaryMarketSnapshot(algo_orders=[{"algoId": 1}])
        result = build_live_canary_preflight(
            settings, rc, market=market, reconcile_report=_EMPTY_RECONCILE
        )
        self.assertIn("btcusdt_algo_orders_exist", result["blocking_reasons"])

    def test_btcusdt_open_orders_exist(self) -> None:
        settings = _settings()
        rc = _runtime(locked=True)
        market = CanaryMarketSnapshot(open_orders=[{"orderId": 1}])
        result = build_live_canary_preflight(
            settings, rc, market=market, reconcile_report=_EMPTY_RECONCILE
        )
        self.assertIn("btcusdt_open_orders_exist", result["blocking_reasons"])

    def test_reconcile_error(self) -> None:
        settings = _settings()
        rc = _runtime(locked=True)
        report = {"summary": {"error_count": 2, "warn_count": 1}}
        result = build_live_canary_preflight(
            settings, rc, market=_empty_market(), reconcile_report=report
        )
        self.assertIn("reconcile_error", result["blocking_reasons"])

    def test_all_conditions_met_canary_ready(self) -> None:
        result = _ready_preflight()
        self.assertTrue(result["canary_ready"])
        self.assertEqual(result["blocking_reasons"], [])
        self.assertTrue(result["is_live"])
        self.assertTrue(result["live_guard"]["guard_active"])
        self.assertTrue(result["live_confirm_phrase_valid"])
        self.assertEqual(result["phase"], "preflight_only")

    def test_no_secrets_in_payload(self) -> None:
        settings = _settings(
            LIVE_CONFIRM_PHRASE=_CONFIRM,
            WEBHOOK_SECRET="super-secret-webhook",
            BINANCE_API_KEY="key123",
            BINANCE_API_SECRET="sec456",
        )
        rc = _runtime(locked=True)
        result = build_live_canary_preflight(
            settings, rc, market=_empty_market(), reconcile_report=_EMPTY_RECONCILE
        )
        blob = str(result)
        self.assertNotIn("super-secret-webhook", blob)
        self.assertNotIn("key123", blob)
        self.assertNotIn("sec456", blob)
        self.assertNotIn(_CONFIRM, blob)
        self.assertIn("live_confirm_phrase_configured", result)
        self.assertIn("live_confirm_phrase_valid", result)

    def test_tradingview_not_rejected_blocking(self) -> None:
        result = _ready_preflight(LIVE_REJECT_TRADINGVIEW_BY_DEFAULT="false")
        self.assertIn("tradingview_live_not_rejected", result["blocking_reasons"])

    def test_btcusdt_not_allowed(self) -> None:
        result = _ready_preflight(LIVE_ALLOWED_SYMBOLS="ETHUSDT")
        self.assertIn("btcusdt_not_allowed", result["blocking_reasons"])

    def test_evaluate_blocking_reasons_demo(self) -> None:
        settings = _settings(BINANCE_BASE_URL=_DEMO_URL)
        reasons = evaluate_canary_blocking_reasons(settings, _runtime(), market=_empty_market())
        self.assertIn("not_live_environment", reasons)


if __name__ == "__main__":
    unittest.main()

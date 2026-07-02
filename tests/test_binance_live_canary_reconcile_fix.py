"""Tests for v6.5.6 Binance live canary reconcile/preflight fix."""

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
from app.reconcile import (
    RECONCILE_CONTEXT_DEFAULT,
    RECONCILE_CONTEXT_LIVE_CANARY_PREFLIGHT,
    compute_reconcile_summary_for_context,
)
from app.trader import Trader
from tests.test_live_guard import _CONFIRM, _DEMO_URL, _LIVE_URL, _settings

_EMPTY_MARKET = CanaryMarketSnapshot(positions=[], algo_orders=[], open_orders=[])


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


def _ready_live_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "LIVE_TRADING_ENABLED": "true",
        "LIVE_CONFIRM_PHRASE": _CONFIRM,
        "LIVE_CANARY_MODE": "true",
    }
    base.update(overrides)
    return _settings(**base)


def _reconcile_report_live_demo_env_error(**summary_overrides: object) -> dict:
    summary = {
        "symbols_checked": 3,
        "open_position_count": 0,
        "unprotected_position_count": 0,
        "residual_order_symbol_count": 0,
        "error_count": 1,
        "warn_count": 0,
    }
    summary.update(summary_overrides)
    return {
        "success": False,
        "level": "ERROR",
        "binance_env": "live",
        "summary": summary,
        "checks": [
            {
                "name": "binance_demo_environment",
                "level": "ERROR",
                "message": "Binance 环境不是 demo/testnet (binance_env=live)",
            },
            {"name": "runtime_locked", "level": "OK", "message": "Runtime 当前已锁定"},
        ],
        "symbols": [],
        "trigger": "dashboard_manual",
    }


def _reconcile_report_with_symbol_error(
    *,
    name: str = "symbol_data",
    level: str = "ERROR",
    message: str = "查询失败",
) -> dict:
    return {
        "success": False,
        "level": "ERROR",
        "binance_env": "live",
        "summary": {
            "symbols_checked": 1,
            "open_position_count": 0,
            "unprotected_position_count": 0,
            "residual_order_symbol_count": 0,
            "error_count": 1,
            "warn_count": 0,
        },
        "checks": [
            {
                "name": "binance_demo_environment",
                "level": "ERROR",
                "message": "Binance 环境不是 demo/testnet (binance_env=live)",
            }
        ],
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "checks": [{"name": name, "level": level, "message": message, "symbol": "BTCUSDT"}],
            }
        ],
        "trigger": "dashboard_manual",
    }


class ReconcileContextSummaryTests(unittest.TestCase):
    def test_default_context_counts_live_demo_environment_as_error(self) -> None:
        settings = _ready_live_settings()
        report = _reconcile_report_live_demo_env_error()
        summary = compute_reconcile_summary_for_context(
            report,
            settings,
            context=RECONCILE_CONTEXT_DEFAULT,
        )
        self.assertEqual(summary["error_count"], 1)

    def test_live_canary_preflight_context_ignores_demo_environment_error(self) -> None:
        settings = _ready_live_settings()
        report = _reconcile_report_live_demo_env_error()
        summary = compute_reconcile_summary_for_context(
            report,
            settings,
            context=RECONCILE_CONTEXT_LIVE_CANARY_PREFLIGHT,
        )
        self.assertEqual(summary["error_count"], 0)

    def test_demo_environment_reconcile_unchanged_on_demo_env(self) -> None:
        settings = _settings(BINANCE_BASE_URL=_DEMO_URL, LIVE_CANARY_MODE="true")
        report = {
            "summary": {"error_count": 0, "warn_count": 0},
            "checks": [
                {
                    "name": "binance_demo_environment",
                    "level": "OK",
                    "message": "Binance 环境为 demo/testnet",
                }
            ],
            "symbols": [],
        }
        summary = compute_reconcile_summary_for_context(
            report,
            settings,
            context=RECONCILE_CONTEXT_DEFAULT,
        )
        self.assertEqual(summary["error_count"], 0)


class LiveCanaryPreflightReconcileFixTests(unittest.TestCase):
    def test_live_flat_clean_not_blocked_by_binance_demo_environment(self) -> None:
        settings = _ready_live_settings()
        rc = _runtime(locked=True, one_shot_enabled=False)
        result = build_live_canary_preflight(
            settings,
            rc,
            market=_EMPTY_MARKET,
            reconcile_report=_reconcile_report_live_demo_env_error(),
        )
        self.assertEqual(result["reconcile_summary"]["error_count"], 0)
        self.assertNotIn("reconcile_error", result["blocking_reasons"])
        self.assertTrue(result["canary_ready"])

    def test_live_canary_mode_false_still_blocks_on_demo_environment_reconcile(self) -> None:
        settings = _ready_live_settings(LIVE_CANARY_MODE="false")
        rc = _runtime(locked=True)
        result = build_live_canary_preflight(
            settings,
            rc,
            market=_EMPTY_MARKET,
            reconcile_report=_reconcile_report_live_demo_env_error(),
        )
        self.assertEqual(result["reconcile_summary"]["error_count"], 1)
        self.assertIn("reconcile_error", result["blocking_reasons"])

    def test_fetch_error_still_blocks(self) -> None:
        settings = _ready_live_settings()
        rc = _runtime(locked=True)
        result = build_live_canary_preflight(
            settings,
            rc,
            market=_EMPTY_MARKET,
            reconcile_report=_reconcile_report_live_demo_env_error(),
            fetch_error="401 Unauthorized",
        )
        self.assertIn("btcusdt_fetch_error", result["blocking_reasons"])
        self.assertFalse(result["canary_ready"])

    def test_reconcile_symbol_fetch_error_still_blocks(self) -> None:
        settings = _ready_live_settings()
        rc = _runtime(locked=True)
        report = _reconcile_report_with_symbol_error(name="open_orders_fetch", message="api down")
        result = build_live_canary_preflight(
            settings,
            rc,
            market=_EMPTY_MARKET,
            reconcile_report=report,
        )
        self.assertGreater(result["reconcile_summary"]["error_count"], 0)
        self.assertIn("reconcile_error", result["blocking_reasons"])

    def test_reconcile_open_positions_still_blocks(self) -> None:
        settings = _ready_live_settings()
        rc = _runtime(locked=True)
        report = _reconcile_report_live_demo_env_error(open_position_count=1)
        result = build_live_canary_preflight(
            settings,
            rc,
            market=_EMPTY_MARKET,
            reconcile_report=report,
        )
        self.assertIn("reconcile_open_positions", result["blocking_reasons"])

    def test_btcusdt_open_orders_still_blocks(self) -> None:
        settings = _ready_live_settings()
        rc = _runtime(locked=True)
        market = CanaryMarketSnapshot(open_orders=[{"orderId": 1}])
        result = build_live_canary_preflight(
            settings,
            rc,
            market=market,
            reconcile_report=_reconcile_report_live_demo_env_error(),
        )
        self.assertIn("btcusdt_open_orders_exist", result["blocking_reasons"])

    def test_btcusdt_algo_orders_still_blocks(self) -> None:
        settings = _ready_live_settings()
        rc = _runtime(locked=True)
        market = CanaryMarketSnapshot(algo_orders=[{"algoId": 1}])
        result = build_live_canary_preflight(
            settings,
            rc,
            market=market,
            reconcile_report=_reconcile_report_live_demo_env_error(),
        )
        self.assertIn("btcusdt_algo_orders_exist", result["blocking_reasons"])

    def test_btcusdt_position_not_flat_still_blocks(self) -> None:
        settings = _ready_live_settings()
        rc = _runtime(locked=True)
        market = CanaryMarketSnapshot(positions=[{"positionAmt": "0.001"}])
        result = build_live_canary_preflight(
            settings,
            rc,
            market=market,
            reconcile_report=_reconcile_report_live_demo_env_error(),
        )
        self.assertIn("btcusdt_position_not_flat", result["blocking_reasons"])

    def test_reconcile_residual_orders_still_blocks(self) -> None:
        settings = _ready_live_settings()
        rc = _runtime(locked=True)
        report = _reconcile_report_live_demo_env_error(residual_order_symbol_count=1)
        result = build_live_canary_preflight(
            settings,
            rc,
            market=_EMPTY_MARKET,
            reconcile_report=report,
        )
        self.assertIn("reconcile_residual_orders", result["blocking_reasons"])

    def test_not_live_environment_still_blocks(self) -> None:
        settings = _settings(BINANCE_BASE_URL=_DEMO_URL, LIVE_CANARY_MODE="true")
        rc = _runtime(locked=True)
        result = build_live_canary_preflight(
            settings,
            rc,
            market=_EMPTY_MARKET,
            reconcile_report={"summary": {"error_count": 0, "warn_count": 0}, "checks": [], "symbols": []},
        )
        self.assertIn("not_live_environment", result["blocking_reasons"])


class BinanceUnaffectedTests(unittest.TestCase):
    def test_trader_binance_path_unaffected(self) -> None:
        settings = Settings(
            WEBHOOK_SECRET="test-secret",
            EXCHANGE="binance",
            BINANCE_BASE_URL=_LIVE_URL,
        )
        client = MagicMock()
        rules = MagicMock()
        runtime = MagicMock()
        runtime.is_execution_blocked.return_value = (True, {"locked": True})
        trader = Trader(settings, client, rules, runtime_control=runtime)
        signal = MagicMock()
        result = trader.execute(signal, "binance-reconcile-fix", {})
        self.assertEqual(result.get("skip_reason"), "runtime_locked")


if __name__ == "__main__":
    unittest.main()

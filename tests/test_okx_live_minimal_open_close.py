"""Offline tests for v6.5.3 OKX minimal open-close canary."""

from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.exchanges.okx import OkxExchange
from app.exchanges.okx_sizing import OkxInstrumentMeta, compute_minimal_contract_sz
from app.okx_guard import validate_okx_canary_before_execute, validate_okx_guard_before_plan
from app.okx_live_canary import execute_okx_minimal_open_close
from app.schemas import TradingViewSignal
from app.trader import Trader

_TEST_SECRET = "test-secret-for-okx-canary-unit-tests"
_CONFIRM = "I_UNDERSTAND_THIS_IS_REAL_MONEY"
_OKX_CREDS = {
    "OKX_API_KEY": "test-okx-key",
    "OKX_API_SECRET": "test-okx-secret",
    "OKX_API_PASSPHRASE": "test-passphrase",
}


def _canary_settings(**overrides: object) -> Settings:
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
        "OKX_MAX_RISK_USDT": "1",
        "OKX_MAX_MARGIN_USDT": "20",
        "OKX_MAX_NOTIONAL_USDT": "100",
        **_OKX_CREDS,
    }
    base.update(overrides)
    return Settings(**base)


def _runtime_control(*, one_shot_enabled: bool, remaining: int = 1) -> MagicMock:
    rc = MagicMock()
    rc.settings = SimpleNamespace(runtime_control_enabled=True)
    rc.is_execution_blocked.return_value = (False, {})
    rc.status_payload.return_value = {
        "one_shot": {
            "enabled": one_shot_enabled,
            "remaining": remaining,
            "consumed_at": None,
        }
    }
    return rc


def _payload(**overrides: object) -> tuple[TradingViewSignal, dict]:
    raw: dict = {
        "secret": _TEST_SECRET,
        "source": "local_canary",
        "signal_id": "V653-OKX-CANARY-TEST-001",
        "symbol": "BTCUSDT",
        "side": "buy",
        "entry_type": "market",
        "risk_mode": "manual",
        "margin_usdt": 20,
        "leverage": 1,
        "close": 90000,
        "position_strategy": "replace",
    }
    raw.update(overrides)
    return TradingViewSignal.model_validate(raw), raw


class OkxSizingTests(unittest.TestCase):
    def test_compute_minimal_contract_sz_uses_min_sz_and_ct_val(self) -> None:
        meta = OkxInstrumentMeta(
            inst_id="BTC-USDT-SWAP",
            inst_type="SWAP",
            lot_sz=Decimal("1"),
            min_sz=Decimal("1"),
            ct_val=Decimal("0.01"),
            ct_mult=Decimal("1"),
            tick_sz=Decimal("0.1"),
        )
        sz, notional, margin = compute_minimal_contract_sz(
            meta,
            mark_price=Decimal("900"),
            margin_usdt=Decimal("20"),
        )
        self.assertEqual(sz, Decimal("1"))
        self.assertEqual(notional, Decimal("9"))
        self.assertEqual(margin, Decimal("9"))


class OkxCanaryGuardTests(unittest.TestCase):
    def test_guard_passes_for_local_canary_when_config_ready(self) -> None:
        settings = _canary_settings()
        signal, payload = _payload()
        runtime = _runtime_control(one_shot_enabled=True)
        self.assertIsNone(
            validate_okx_guard_before_plan(
                settings,
                signal,
                payload,
                runtime_control=runtime,
            )
        )
        self.assertIsNone(
            validate_okx_canary_before_execute(
                settings,
                signal,
                payload,
                runtime_control=runtime,
            )
        )

    def test_non_local_canary_source_rejected(self) -> None:
        settings = _canary_settings()
        signal, payload = _payload(source="tradingview")
        runtime = _runtime_control(one_shot_enabled=True)
        rejection = validate_okx_guard_before_plan(
            settings,
            signal,
            payload,
            runtime_control=runtime,
        )
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "okx_source_not_allowed")

    def test_sell_side_rejected(self) -> None:
        settings = _canary_settings()
        signal, payload = _payload(side="sell")
        runtime = _runtime_control(one_shot_enabled=True)
        rejection = validate_okx_canary_before_execute(
            settings,
            signal,
            payload,
            runtime_control=runtime,
        )
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "okx_side_not_allowed")

    def test_tpsl_rejected(self) -> None:
        settings = _canary_settings()
        signal, payload = _payload(sl=89000, tps=[{"price": 91000, "qty_pct": 1}])
        runtime = _runtime_control(one_shot_enabled=True)
        rejection = validate_okx_canary_before_execute(
            settings,
            signal,
            payload,
            runtime_control=runtime,
        )
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "okx_tpsl_not_supported")


class OkxCanaryExecuteTests(unittest.TestCase):
    def _exchange(self, settings: Settings) -> MagicMock:
        exchange = MagicMock(spec=OkxExchange)
        exchange.name = "okx"
        exchange.get_mark_price.return_value = Decimal("900")
        exchange.get_instrument.return_value = OkxInstrumentMeta(
            inst_id="BTC-USDT-SWAP",
            inst_type="SWAP",
            lot_sz=Decimal("1"),
            min_sz=Decimal("1"),
            ct_val=Decimal("0.01"),
            ct_mult=Decimal("1"),
            tick_sz=Decimal("0.1"),
        )
        exchange.place_market_order_minimal.return_value = {"ordId": "open-1"}
        exchange.close_position_market.return_value = {"ordId": "close-1"}
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

    def test_execute_open_then_close_without_tpsl(self) -> None:
        settings = _canary_settings()
        signal, payload = _payload()
        runtime = _runtime_control(one_shot_enabled=True)
        exchange = self._exchange(settings)
        result = execute_okx_minimal_open_close(
            exchange,
            settings,
            signal,
            payload,
            signal_key="canary-key",
            runtime_control=runtime,
        )
        self.assertFalse(result.get("skipped"))
        self.assertTrue(result["okx_canary"]["success"])
        exchange.place_market_order_minimal.assert_called_once()
        exchange.close_position_market.assert_called_once()
        open_kwargs = exchange.place_market_order_minimal.call_args.kwargs
        self.assertEqual(open_kwargs["inst_id"], "BTC-USDT-SWAP")
        self.assertEqual(open_kwargs["side"], "buy")
        self.assertEqual(open_kwargs["td_mode"], "isolated")
        self.assertEqual(open_kwargs["sz"], Decimal("1"))

    def test_trader_okx_path_delegates_to_canary_executor(self) -> None:
        settings = _canary_settings()
        client = MagicMock()
        rules = MagicMock()
        exchange = self._exchange(settings)
        runtime = _runtime_control(one_shot_enabled=True)
        trader = Trader(settings, client, rules, runtime_control=runtime, exchange_client=exchange)
        signal, payload = _payload()
        result = trader.execute(signal, "trader-canary", payload)
        self.assertFalse(result.get("skipped"))
        exchange.place_market_order_minimal.assert_called_once()
        exchange.close_position_market.assert_called_once()
        client.new_market_order.assert_not_called()

    def test_binance_unaffected(self) -> None:
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
        signal, payload = _payload()
        result = trader.execute(signal, "binance-key", payload)
        self.assertEqual(result.get("skip_reason"), "runtime_locked")


if __name__ == "__main__":
    unittest.main()

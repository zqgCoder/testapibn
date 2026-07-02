"""Offline tests for v6.5.6 OKX posSide-aware minimal canary."""

from __future__ import annotations

import io
import logging
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.exchanges.okx import OkxExchange
from app.exchanges.okx_sizing import OkxInstrumentMeta
from app.okx_guard import validate_okx_canary_before_execute
from app.okx_live_canary import execute_okx_minimal_open_close
from app.okx_pos_side import (
    OKX_POS_MODE_LONG_SHORT,
    OKX_POS_MODE_NET,
    resolve_canary_pos_side,
)
from app.trader import Trader

_TEST_SECRET = "test-secret-for-okx-posside-unit-tests"
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
        "OKX_POS_SIDE": "long",
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


def _signal_and_payload(**overrides: object):
    from app.schemas import TradingViewSignal

    raw = {
        "secret": _TEST_SECRET,
        "source": "local_canary",
        "signal_id": "V656-OKX-POSSIDE-TEST",
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


def _instrument_meta() -> OkxInstrumentMeta:
    return OkxInstrumentMeta(
        inst_id="BTC-USDT-SWAP",
        inst_type="SWAP",
        lot_sz=Decimal("0.01"),
        min_sz=Decimal("0.01"),
        ct_val=Decimal("0.01"),
        ct_mult=Decimal("1"),
        tick_sz=Decimal("0.1"),
    )


class OkxPosSideResolutionTests(unittest.TestCase):
    def test_long_short_mode_resolves_pos_side_long(self) -> None:
        settings = _settings()
        self.assertEqual(resolve_canary_pos_side(settings, OKX_POS_MODE_LONG_SHORT), "long")

    def test_net_mode_resolves_no_pos_side(self) -> None:
        settings = _settings()
        self.assertIsNone(resolve_canary_pos_side(settings, OKX_POS_MODE_NET))


class OkxExchangePosSideOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = _settings()
        self.exchange = OkxExchange(self.settings)

    def test_long_short_open_includes_pos_side_long(self) -> None:
        with patch.object(self.exchange._client, "request") as mock_request:
            mock_request.return_value = {"code": "0", "data": [{"ordId": "1"}]}
            self.exchange.place_market_order_minimal(
                inst_id="BTC-USDT-SWAP",
                side="buy",
                sz=Decimal("0.01"),
                td_mode="isolated",
                pos_side="long",
                client_order_id="open-test",
            )
        body = mock_request.call_args.kwargs["body"]
        self.assertEqual(body["side"], "buy")
        self.assertEqual(body["posSide"], "long")
        self.assertEqual(body["tdMode"], "isolated")

    def test_long_short_close_uses_sell_with_pos_side_long(self) -> None:
        with patch.object(self.exchange._client, "request") as mock_request:
            mock_request.return_value = {"code": "0", "data": [{"ordId": "2"}]}
            self.exchange.close_position_market(
                inst_id="BTC-USDT-SWAP",
                td_mode="isolated",
                pos_side="long",
                sz=Decimal("0.01"),
                client_order_id="close-test",
            )
        self.assertEqual(mock_request.call_args.args[1], "/api/v5/trade/order")
        body = mock_request.call_args.kwargs["body"]
        self.assertEqual(body["side"], "sell")
        self.assertEqual(body["posSide"], "long")
        self.assertTrue(body["reduceOnly"])

    def test_net_mode_close_uses_close_position_without_long_pos_side(self) -> None:
        with patch.object(self.exchange._client, "request") as mock_request:
            mock_request.return_value = {"code": "0", "data": [{"ordId": "3"}]}
            self.exchange.close_position_market(
                inst_id="BTC-USDT-SWAP",
                td_mode="isolated",
                pos_side=None,
                client_order_id="close-net",
            )
        self.assertEqual(mock_request.call_args.args[1], "/api/v5/trade/close-position")
        body = mock_request.call_args.kwargs["body"]
        self.assertEqual(body["posSide"], "net")
        self.assertNotIn("side", body)


class OkxCanaryExecutePosSideTests(unittest.TestCase):
    def _exchange(self, *, pos_mode: str) -> MagicMock:
        exchange = MagicMock(spec=OkxExchange)
        exchange.get_account_config.return_value = {"posMode": pos_mode}
        exchange.get_mark_price.return_value = Decimal("61500")
        exchange.get_instrument.return_value = _instrument_meta()
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

    def test_long_short_mode_open_passes_pos_side_long(self) -> None:
        settings = _settings()
        signal, payload = _signal_and_payload()
        exchange = self._exchange(pos_mode=OKX_POS_MODE_LONG_SHORT)
        execute_okx_minimal_open_close(
            exchange,
            settings,
            signal,
            payload,
            signal_key="posside-open",
            runtime_control=_runtime_control(),
        )
        open_kwargs = exchange.place_market_order_minimal.call_args.kwargs
        close_kwargs = exchange.close_position_market.call_args.kwargs
        self.assertEqual(open_kwargs["pos_side"], "long")
        self.assertEqual(close_kwargs["pos_side"], "long")
        self.assertEqual(close_kwargs["sz"], open_kwargs["sz"])

    def test_net_mode_open_close_without_pos_side(self) -> None:
        settings = _settings()
        signal, payload = _signal_and_payload()
        exchange = self._exchange(pos_mode=OKX_POS_MODE_NET)
        execute_okx_minimal_open_close(
            exchange,
            settings,
            signal,
            payload,
            signal_key="posside-net",
            runtime_control=_runtime_control(),
        )
        open_kwargs = exchange.place_market_order_minimal.call_args.kwargs
        close_kwargs = exchange.close_position_market.call_args.kwargs
        self.assertIsNone(open_kwargs["pos_side"])
        self.assertIsNone(close_kwargs["pos_side"])

    def test_pre_order_log_includes_pos_side_without_secret(self) -> None:
        settings = _settings()
        signal, payload = _signal_and_payload()
        exchange = self._exchange(pos_mode=OKX_POS_MODE_LONG_SHORT)
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        logger = logging.getLogger("app.okx_error_observability")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            execute_okx_minimal_open_close(
                exchange,
                settings,
                signal,
                payload,
                signal_key="posside-log",
                runtime_control=_runtime_control(),
            )
        finally:
            logger.removeHandler(handler)
        output = log_stream.getvalue()
        self.assertIn("posMode=long_short_mode", output)
        self.assertIn("posSide=long", output)
        self.assertIn("side=buy", output)
        self.assertIn("side=sell", output)
        self.assertNotIn(_TEST_SECRET, output)


class OkxGuardPosSideTests(unittest.TestCase):
    def test_sell_side_still_rejected(self) -> None:
        settings = _settings()
        signal, payload = _signal_and_payload(side="sell")
        rejection = validate_okx_canary_before_execute(
            settings,
            signal,
            payload,
            runtime_control=_runtime_control(),
        )
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "okx_side_not_allowed")

    def test_non_long_pos_side_config_rejected(self) -> None:
        settings = _settings(OKX_POS_SIDE="short")
        signal, payload = _signal_and_payload()
        rejection = validate_okx_canary_before_execute(
            settings,
            signal,
            payload,
            runtime_control=_runtime_control(),
        )
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.skip_reason, "okx_pos_side_not_allowed")


class BinanceUnaffectedTests(unittest.TestCase):
    def test_binance_execute_unaffected(self) -> None:
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
        result = trader.execute(signal, "binance-posside", payload)
        self.assertEqual(result.get("skip_reason"), "runtime_locked")


if __name__ == "__main__":
    unittest.main()

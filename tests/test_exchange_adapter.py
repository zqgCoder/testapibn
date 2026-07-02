"""Offline unit tests for v6.5.0 exchange adapter (no Binance, no network, no real .env)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.config import Settings
from app.exchanges import BinanceFuturesExchange, create_exchange_client
from app.exchanges.binance_futures import BinanceFuturesExchange as BinanceFuturesExchangeCls
from app.trader import Trader

_TEST_SECRET = "test-secret-for-exchange-adapter-unit-tests"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "WEBHOOK_SECRET": _TEST_SECRET,
        "EXCHANGE": "binance",
        "BINANCE_BASE_URL": "https://demo-fapi.binance.com",
    }
    base.update(overrides)
    return Settings(**base)


class ExchangeAdapterFactoryTests(unittest.TestCase):
    def test_default_exchange_is_binance(self) -> None:
        settings = _settings()
        self.assertEqual(settings.exchange, "binance")

    def test_create_exchange_client_returns_binance_adapter(self) -> None:
        settings = _settings()
        client = MagicMock()
        adapter = create_exchange_client(settings, client)
        self.assertIsInstance(adapter, BinanceFuturesExchange)
        self.assertEqual(adapter.name, "binance")

    def test_unsupported_exchange_raises(self) -> None:
        settings = _settings(EXCHANGE="kraken")
        with self.assertRaises(RuntimeError) as ctx:
            create_exchange_client(settings, MagicMock())
        self.assertIn("kraken", str(ctx.exception).lower())

    def test_validate_runtime_rejects_unsupported_exchange(self) -> None:
        settings = _settings(EXCHANGE="kraken")
        with self.assertRaises(RuntimeError) as ctx:
            settings.validate_runtime()
        self.assertIn("EXCHANGE", str(ctx.exception))


class BinanceFuturesExchangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = _settings()
        self.client = MagicMock()
        self.adapter = BinanceFuturesExchangeCls(self.settings, self.client)

    def test_get_positions_filters_symbol(self) -> None:
        self.client.position_risk.return_value = [
            {"symbol": "BTCUSDT", "positionAmt": "0.010"},
            {"symbol": "ETHUSDT", "positionAmt": "1"},
        ]
        rows = self.adapter.get_positions("BTCUSDT")
        self.client.position_risk.assert_called_once_with("BTCUSDT")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "BTCUSDT")

    def test_get_open_orders_delegates(self) -> None:
        self.client.open_orders.return_value = [{"symbol": "BTCUSDT", "orderId": 1}]
        rows = self.adapter.get_open_orders("BTCUSDT")
        self.client.open_orders.assert_called_once_with("BTCUSDT")
        self.assertEqual(rows[0]["orderId"], 1)

    def test_get_algo_orders_delegates(self) -> None:
        self.client.open_algo_orders.return_value = [{"symbol": "BTCUSDT", "algoId": 9}]
        rows = self.adapter.get_algo_orders("BTCUSDT")
        self.client.open_algo_orders.assert_called_once_with("BTCUSDT")
        self.assertEqual(rows[0]["algoId"], 9)

    def test_cancel_open_orders_delegates(self) -> None:
        self.client.cancel_all_open_orders.return_value = {"code": 200}
        result = self.adapter.cancel_open_orders("BTCUSDT")
        self.client.cancel_all_open_orders.assert_called_once_with("BTCUSDT")
        self.assertEqual(result["code"], 200)

    def test_cancel_algo_orders_delegates(self) -> None:
        self.client.cancel_all_algo_open_orders.return_value = {"code": 200}
        result = self.adapter.cancel_algo_orders("BTCUSDT")
        self.client.cancel_all_algo_open_orders.assert_called_once_with("BTCUSDT")
        self.assertEqual(result["code"], 200)

    def test_close_position_no_position_cleanup(self) -> None:
        self.client.position_risk.return_value = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self.client.cancel_all_open_orders.return_value = {"code": 200}
        self.client.cancel_all_algo_open_orders.return_value = {"code": 200}
        result = self.adapter.close_position(
            "BTCUSDT",
            reason="test",
            operator="unit-test",
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "no_position_cleanup_done")
        self.client.close_position_market.assert_not_called()

    def test_reconcile_delegates_to_service(self) -> None:
        reconcile = MagicMock()
        reconcile.run_audit.return_value = {"success": True, "level": "OK"}
        adapter = BinanceFuturesExchangeCls(
            self.settings,
            self.client,
            reconcile_service=reconcile,
        )
        report = adapter.reconcile(trigger="unit_test")
        reconcile.run_audit.assert_called_once_with(trigger="unit_test")
        self.assertTrue(report["success"])


class TraderExchangeIntegrationTests(unittest.TestCase):
    def test_trader_defaults_to_binance_exchange_adapter(self) -> None:
        settings = _settings()
        client = MagicMock()
        rules = MagicMock()
        trader = Trader(settings, client, rules)
        self.assertIsInstance(trader.exchange_client, BinanceFuturesExchange)
        self.assertEqual(trader.exchange_client.name, "binance")

    def test_cancel_symbol_open_orders_uses_exchange_adapter(self) -> None:
        settings = _settings()
        client = MagicMock()
        rules = MagicMock()
        exchange = MagicMock()
        exchange.cancel_open_orders.return_value = {"regular": True}
        exchange.cancel_algo_orders.return_value = {"algo": True}
        trader = Trader(settings, client, rules, exchange_client=exchange)
        result = trader.cancel_symbol_open_orders("BTCUSDT")
        exchange.cancel_open_orders.assert_called_once_with("BTCUSDT")
        exchange.cancel_algo_orders.assert_called_once_with("BTCUSDT")
        client.cancel_all_open_orders.assert_not_called()
        client.cancel_all_algo_open_orders.assert_not_called()
        self.assertEqual(result["regular"], {"regular": True})
        self.assertEqual(result["algo"], {"algo": True})

    def test_position_row_uses_exchange_adapter(self) -> None:
        settings = _settings()
        client = MagicMock()
        rules = MagicMock()
        exchange = MagicMock()
        exchange.get_positions.return_value = [{"symbol": "BTCUSDT", "positionAmt": "0.5"}]
        trader = Trader(settings, client, rules, exchange_client=exchange)
        row = trader._position_row_for_symbol("BTCUSDT")
        exchange.get_positions.assert_called_once_with("BTCUSDT")
        client.position_risk.assert_not_called()
        self.assertEqual(row["positionAmt"], "0.5")

    def test_close_position_maintenance_delegates_to_exchange(self) -> None:
        settings = _settings()
        client = MagicMock()
        rules = MagicMock()
        exchange = MagicMock()
        exchange.close_position.return_value = {"success": True, "status": "no_position_cleanup_done"}
        trader = Trader(settings, client, rules, exchange_client=exchange)
        result = trader.close_position_maintenance(
            "BTCUSDT",
            reason="adapter-test",
            operator="unit",
        )
        exchange.close_position.assert_called_once_with(
            "BTCUSDT",
            reason="adapter-test",
            operator="unit",
            cancel_before_close=True,
            cancel_after_close=True,
            wait_seconds=10,
        )
        client.close_position_market.assert_not_called()
        self.assertTrue(result["success"])

    def test_cleanup_symbol_orders_uses_exchange_adapter(self) -> None:
        settings = _settings()
        client = MagicMock()
        rules = MagicMock()
        exchange = MagicMock()
        exchange.cancel_open_orders.return_value = {"code": 200}
        exchange.cancel_algo_orders.return_value = {"code": 200}
        exchange.get_positions.return_value = []
        exchange.get_open_orders.return_value = []
        exchange.get_algo_orders.return_value = []
        trader = Trader(settings, client, rules, exchange_client=exchange)
        trader.cleanup_symbol_orders("BTCUSDT", reason="cleanup", operator="unit")
        exchange.get_open_orders.assert_called_once_with("BTCUSDT")
        exchange.get_algo_orders.assert_called_once_with("BTCUSDT")
        client.open_orders.assert_not_called()
        client.open_algo_orders.assert_not_called()

    def test_trader_keeps_binance_client_for_order_execution_path(self) -> None:
        settings = _settings()
        client = MagicMock()
        rules = MagicMock()
        trader = Trader(settings, client, rules)
        self.assertIs(trader.client, client)
        self.assertIsNot(trader.client, trader.exchange_client)
        self.assertEqual(trader.exchange_client.name, "binance")


if __name__ == "__main__":
    unittest.main()

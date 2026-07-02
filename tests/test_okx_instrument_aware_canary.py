"""Offline tests for v6.5.4 OKX instrument-aware canary guard."""

from __future__ import annotations

import io
import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.exchanges.okx_sizing import OkxInstrumentMeta
from app.okx_canary_feasibility import (
    evaluate_okx_canary_feasibility,
    feasibility_from_settings,
    print_feasibility_report,
)
from app.trader import Trader


def _load_script_module(module_name: str, filename: str):
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load script module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


payload_script = _load_script_module(
    "build_local_okx_live_canary_payload",
    "build_local_okx_live_canary_payload.py",
)
feasibility_script = _load_script_module(
    "check_okx_instrument_canary_feasibility",
    "check_okx_instrument_canary_feasibility.py",
)

_TEST_SECRET = "test-secret-for-okx-feasibility-unit-tests"
_CONFIRM = "I_UNDERSTAND_THIS_IS_REAL_MONEY"
_OKX_CREDS = {
    "OKX_API_KEY": "test-okx-key",
    "OKX_API_SECRET": "test-okx-secret",
    "OKX_API_PASSPHRASE": "test-passphrase",
}


def _btc_swap_meta() -> OkxInstrumentMeta:
    return OkxInstrumentMeta(
        inst_id="BTC-USDT-SWAP",
        inst_type="SWAP",
        lot_sz=Decimal("1"),
        min_sz=Decimal("1"),
        ct_val=Decimal("0.01"),
        ct_mult=Decimal("1"),
        tick_sz=Decimal("0.1"),
    )


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
        "OKX_MAX_MARGIN_USDT": "20",
        "OKX_MAX_NOTIONAL_USDT": "100",
        **_OKX_CREDS,
    }
    base.update(overrides)
    return Settings(**base)


class OkxCanaryFeasibilityUnitTests(unittest.TestCase):
    def test_required_margin_exceeds_budget_rejects(self) -> None:
        result = evaluate_okx_canary_feasibility(
            inst_id="BTC-USDT-SWAP",
            meta=_btc_swap_meta(),
            mark_price=Decimal("60000"),
            margin_usdt=Decimal("20"),
            max_notional_usdt=Decimal("100"),
            leverage=1,
        )
        self.assertFalse(result.feasible)
        self.assertEqual(result.rejection_reason, "sizing_infeasible")
        self.assertIsNone(result.required_margin)

    def test_required_notional_exceeds_max_rejects(self) -> None:
        result = evaluate_okx_canary_feasibility(
            inst_id="BTC-USDT-SWAP",
            meta=_btc_swap_meta(),
            mark_price=Decimal("900"),
            margin_usdt=Decimal("20"),
            max_notional_usdt=Decimal("5"),
            leverage=1,
        )
        self.assertFalse(result.feasible)
        self.assertEqual(result.rejection_reason, "required_notional_exceeds_max")
        self.assertEqual(result.required_notional, Decimal("9"))

    def test_feasible_when_within_limits(self) -> None:
        result = evaluate_okx_canary_feasibility(
            inst_id="BTC-USDT-SWAP",
            meta=_btc_swap_meta(),
            mark_price=Decimal("900"),
            margin_usdt=Decimal("20"),
            max_notional_usdt=Decimal("100"),
            leverage=1,
        )
        self.assertTrue(result.feasible)
        self.assertEqual(result.sz, Decimal("1"))
        self.assertEqual(result.required_notional, Decimal("9"))
        self.assertEqual(result.required_margin, Decimal("9"))


class BuildPayloadScriptTests(unittest.TestCase):
    def _run_build(
        self,
        *,
        close: str,
        output: Path,
        settings: Settings | None = None,
    ) -> tuple[int, str, str]:
        settings = settings or _canary_settings()
        argv = [
            "--close",
            close,
            "--margin-usdt",
            "20",
            "--leverage",
            "1",
            "--output",
            str(output),
        ]
        with patch.object(payload_script, "Settings", return_value=settings):
            with patch.object(payload_script, "fetch_public_instrument", return_value=_btc_swap_meta()):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = payload_script.main(argv)
                return code, stdout.getvalue(), stderr.getvalue()

    def test_sizing_failure_does_not_create_payload_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "canary_payload.json"
            code, _stdout, stderr = self._run_build(close="60000", output=output)
            self.assertNotEqual(code, 0)
            self.assertFalse(output.exists())
            self.assertIn("REJECTED: OKX canary payload not generated", stderr)
            self.assertIn("instId=BTC-USDT-SWAP", stderr)
            self.assertIn("close=60000", stderr)
            self.assertIn("minSz=1", stderr)
            self.assertIn("lotSz=1", stderr)
            self.assertIn("ctVal=0.01", stderr)
            self.assertIn("configured_margin_usdt=20", stderr)
            self.assertIn("configured_max_notional=100", stderr)
            self.assertNotIn(_TEST_SECRET, stderr)

    def test_required_notional_exceeds_max_rejects_without_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "canary_payload.json"
            settings = _canary_settings(OKX_MAX_NOTIONAL_USDT="5")
            code, _stdout, stderr = self._run_build(
                close="900",
                output=output,
                settings=settings,
            )
            self.assertEqual(code, 2)
            self.assertFalse(output.exists())
            self.assertIn("required_notional_exceeds_max", stderr)
            self.assertIn("required_notional=9", stderr)

    def test_feasible_generates_payload_without_printing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "canary_payload.json"
            code, stdout, stderr = self._run_build(close="900", output=output)
            self.assertEqual(code, 0)
            self.assertTrue(output.exists())
            body = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(body["source"], "local_canary")
            self.assertEqual(body["secret"], _TEST_SECRET)
            combined = stdout + stderr
            self.assertNotIn(_TEST_SECRET, combined)
            self.assertNotIn(json.dumps(body), combined)
            self.assertIn("payload_body=not printed", stdout)


class FeasibilityScriptTests(unittest.TestCase):
    def test_feasibility_script_does_not_print_secret(self) -> None:
        settings = _canary_settings()
        with patch.object(feasibility_script, "Settings", return_value=settings):
            with patch.object(feasibility_script, "fetch_public_instrument", return_value=_btc_swap_meta()):
                with patch.object(
                    feasibility_script,
                    "fetch_public_mark_price",
                    return_value=Decimal("60000"),
                ):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = feasibility_script.main([])
                    combined = stdout.getvalue() + stderr.getvalue()
        self.assertNotEqual(code, 0)
        self.assertNotIn(_TEST_SECRET, combined)
        self.assertNotIn("test-okx-secret", combined)
        self.assertNotIn("test-passphrase", combined)
        self.assertIn("canary_suitability=NOT_SUITABLE", combined)

    def test_feasibility_script_suitable_when_within_limits(self) -> None:
        settings = _canary_settings()
        with patch.object(feasibility_script, "Settings", return_value=settings):
            with patch.object(feasibility_script, "fetch_public_instrument", return_value=_btc_swap_meta()):
                with patch.object(
                    feasibility_script,
                    "fetch_public_mark_price",
                    return_value=Decimal("900"),
                ):
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        code = feasibility_script.main([])
        self.assertEqual(code, 0)
        self.assertIn("canary_feasible=true", stdout.getvalue())
        self.assertIn("canary_suitability=SUITABLE", stdout.getvalue())


class BinanceUnaffectedTests(unittest.TestCase):
    def test_binance_trader_execute_unaffected(self) -> None:
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
        signal = MagicMock()
        result = trader.execute(signal, "binance-feasibility-test", {"source": "local_canary"})
        self.assertEqual(result.get("skip_reason"), "runtime_locked")


class FeasibilityReportTests(unittest.TestCase):
    def test_report_contains_required_fields(self) -> None:
        settings = _canary_settings()
        result = feasibility_from_settings(
            settings,
            inst_id="BTC-USDT-SWAP",
            meta=_btc_swap_meta(),
            mark_price=Decimal("900"),
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            print_feasibility_report(result)
        out = stdout.getvalue()
        self.assertIn("instId=BTC-USDT-SWAP", out)
        self.assertIn("required_sz=1", out)
        self.assertIn("configured_margin_usdt=20", out)
        self.assertIn("configured_max_notional=100", out)


if __name__ == "__main__":
    unittest.main()

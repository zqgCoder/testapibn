#!/usr/bin/env python3
"""Build local OKX minimal open-close canary payload (v6.5.3+ / v6.5.4 guard).

Never prints WEBHOOK_SECRET or payload body.
Rejects payload generation when instrument sizing is infeasible for configured limits.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.exchanges.okx_symbols import symbol_to_inst_id
from app.okx_canary_feasibility import (
    evaluate_okx_canary_feasibility,
    fetch_public_instrument,
    format_decimal,
    print_feasibility_rejection,
)
from app.okx_guard import okx_confirm_phrase_valid

DEFAULT_FILENAME = "_v653_okx_live_canary_payload.json"
DEFAULT_OUTPUT = ROOT / DEFAULT_FILENAME


def validate_canary_environment(settings: Settings) -> str | None:
    if settings.exchange.strip().lower() != "okx":
        return "EXCHANGE must be okx for OKX live canary payload."
    if settings.okx_readonly_mode:
        return "OKX_READONLY_MODE must be false for live canary payload generation."
    if not settings.okx_live_trading_enabled:
        return "OKX_LIVE_TRADING_ENABLED must be true for live canary payload generation."
    if not okx_confirm_phrase_valid(settings):
        return "OKX_CONFIRM_PHRASE must match OKX_EXPECTED_CONFIRM_PHRASE."
    if not settings.webhook_secret.strip():
        return "WEBHOOK_SECRET is not configured in .env"
    return None


def build_payload(
    settings: Settings,
    *,
    close: Decimal,
    symbol: str = "BTCUSDT",
    margin_usdt: Decimal = Decimal("20"),
    leverage: int = 1,
) -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return {
        "secret": settings.webhook_secret,
        "source": "local_canary",
        "signal_id": f"V654-OKX-CANARY-{ts}",
        "symbol": symbol.strip().upper(),
        "side": "buy",
        "entry_type": "market",
        "risk_mode": "manual",
        "margin_usdt": format(margin_usdt, "f"),
        "leverage": leverage,
        "close": format(close.quantize(Decimal("0.01")), "f"),
        "position_strategy": "replace",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build OKX minimal canary payload (v6.5.4 guard)")
    parser.add_argument("--close", required=True, help="BTCUSDT reference mark/close price")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--margin-usdt", default="20")
    parser.add_argument("--leverage", type=int, default=1)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    try:
        close = Decimal(str(args.close))
        margin_usdt = Decimal(str(args.margin_usdt))
    except (InvalidOperation, ValueError):
        print("ERROR: numeric arguments must be valid decimals", file=sys.stderr)
        return 1

    settings = Settings()
    env_error = validate_canary_environment(settings)
    if env_error:
        print(f"ERROR: {env_error}", file=sys.stderr)
        return 1

    inst_id = symbol_to_inst_id(args.symbol)
    max_notional = Decimal(str(settings.okx_max_position_notional_usdt))

    try:
        meta = fetch_public_instrument(settings, inst_id)
    except Exception as exc:
        print(f"ERROR: failed to fetch instrument metadata: {exc}", file=sys.stderr)
        return 1

    feasibility = evaluate_okx_canary_feasibility(
        inst_id=inst_id,
        meta=meta,
        mark_price=close,
        margin_usdt=margin_usdt,
        max_notional_usdt=max_notional,
        leverage=args.leverage,
    )
    if not feasibility.feasible:
        print_feasibility_rejection(feasibility, price_label="close")
        return 2

    payload = build_payload(
        settings,
        close=close,
        symbol=args.symbol,
        margin_usdt=margin_usdt,
        leverage=args.leverage,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"payload_file={args.output.resolve()}")
    print(f"signal_id={payload['signal_id']}")
    print(f"instId={inst_id}")
    print("side=buy")
    print(f"margin_usdt={payload['margin_usdt']}")
    print(f"leverage={payload['leverage']}")
    print(f"estimated_sz={format_decimal(feasibility.sz)}")
    print(f"estimated_notional_usdt={format_decimal(feasibility.required_notional)}")
    print(f"estimated_margin_usdt={format_decimal(feasibility.required_margin)}")
    print("webhook_secret=configured (value not printed)")
    print("payload_body=not printed (see payload_file)")
    print(f"cleanup=delete {args.output.name} after canary test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

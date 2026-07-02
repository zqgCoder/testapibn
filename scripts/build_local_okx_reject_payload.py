#!/usr/bin/env python3
"""Build a local controlled POST payload for v6.5.2 OKX live rejection guard runbook.

Reads WEBHOOK_SECRET from .env (never printed). Writes payload JSON without echoing secret
or payload body to stdout.
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

DEFAULT_FILENAME = "_v652_okx_guard_reject_payload.json"
DEFAULT_OUTPUT = ROOT / DEFAULT_FILENAME


def validate_okx_rejection_environment(settings: Settings) -> str | None:
    if settings.exchange.strip().lower() != "okx":
        return "OKX rejection runbook requires EXCHANGE=okx."
    if not settings.webhook_secret.strip():
        return "WEBHOOK_SECRET is not configured in .env"
    return None


def build_payload(
    settings: Settings,
    *,
    close: Decimal,
    side: str = "buy",
    symbol: str = "BTCUSDT",
) -> dict:
    normalized_side = side.strip().lower()
    if normalized_side not in {"buy", "sell", "long", "short"}:
        raise ValueError("side must be buy/sell/long/short")

    is_long = normalized_side in {"buy", "long"}
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    signal_id = f"V652-OKX-GUARD-REJECT-{ts}"
    inst_id = symbol_to_inst_id(symbol)

    if is_long:
        sl = close * Decimal("0.995")
        tp_prices = (
            close * Decimal("1.005"),
            close * Decimal("1.010"),
            close * Decimal("1.015"),
        )
    else:
        sl = close * Decimal("1.005")
        tp_prices = (
            close * Decimal("0.995"),
            close * Decimal("0.990"),
            close * Decimal("0.985"),
        )

    def _round_price(value: Decimal) -> str:
        return format(value.quantize(Decimal("0.01")), "f")

    tps = [
        {"price": _round_price(tp_prices[0]), "qty_pct": 0.5},
        {"price": _round_price(tp_prices[1]), "qty_pct": 0.3},
        {"price": _round_price(tp_prices[2]), "qty_pct": 0.2},
    ]

    return {
        "secret": settings.webhook_secret,
        "source": "local_canary",
        "signal_id": signal_id,
        "symbol": symbol.strip().upper(),
        "side": "buy" if is_long else "sell",
        "entry_type": "market",
        "risk_mode": "fixed_usdt",
        "risk_usdt": 1,
        "margin_usdt": 20,
        "close": _round_price(close),
        "sl": _round_price(sl),
        "tps": tps,
        "position_strategy": "replace",
    }


def _format_tp_summary(tps: list[dict]) -> str:
    return ", ".join(f"{tp['price']}@{tp['qty_pct']}" for tp in tps)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build local OKX rejection guard test payload for v6.5.2."
    )
    parser.add_argument("--close", required=True, help="BTCUSDT reference price for sl/tps")
    parser.add_argument("--side", default="buy", help="buy or sell (default: buy)")
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol (default: BTCUSDT)")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: ./{DEFAULT_FILENAME})",
    )
    args = parser.parse_args(argv)

    try:
        close = Decimal(str(args.close))
    except (InvalidOperation, ValueError):
        print("ERROR: --close must be a valid decimal price", file=sys.stderr)
        return 1
    if close <= 0:
        print("ERROR: --close must be > 0", file=sys.stderr)
        return 1

    settings = Settings()
    env_error = validate_okx_rejection_environment(settings)
    if env_error:
        print(f"ERROR: {env_error}", file=sys.stderr)
        return 1

    try:
        payload = build_payload(settings, close=close, side=args.side, symbol=args.symbol)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    inst_id = symbol_to_inst_id(args.symbol)
    print(f"payload_file={args.output.resolve()}")
    print(f"signal_id={payload['signal_id']}")
    print(f"symbol={payload['symbol']}")
    print(f"okx_inst_id={inst_id}")
    print(f"side={payload['side']}")
    print(f"close={payload['close']}")
    print(f"sl={payload['sl']}")
    print(f"tp={_format_tp_summary(payload['tps'])}")
    print("webhook_secret=configured (value not printed)")
    print("payload_body=not printed (see payload_file)")
    print(f"cleanup=delete {args.output.name} after OKX rejection test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

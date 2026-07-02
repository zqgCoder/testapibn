#!/usr/bin/env python3
"""Read-only OKX instrument canary feasibility check (v6.5.4).

Fetches live instrument metadata and mark price, evaluates whether current .env
canary limits can support minimum contract size. Does not place orders.
Never prints WEBHOOK_SECRET or API credentials.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.exchanges.okx_symbols import symbol_to_inst_id
from app.okx_canary_feasibility import (
    feasibility_from_settings,
    fetch_public_instrument,
    fetch_public_mark_price,
    print_feasibility_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check OKX instrument canary feasibility against .env limits (read-only)"
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="Internal symbol, default BTCUSDT")
    parser.add_argument(
        "--margin-usdt",
        default=None,
        help="Override margin budget (default: OKX_MAX_MARGIN_USDT from .env)",
    )
    parser.add_argument(
        "--max-notional",
        default=None,
        help="Override max notional (default: OKX_MAX_NOTIONAL_USDT from .env)",
    )
    parser.add_argument("--leverage", type=int, default=1)
    args = parser.parse_args(argv)

    settings = Settings()
    if settings.exchange.strip().lower() != "okx":
        print("ERROR: EXCHANGE must be okx for OKX canary feasibility check.", file=sys.stderr)
        return 1

    margin_usdt: Decimal | None = None
    max_notional: Decimal | None = None
    try:
        if args.margin_usdt is not None:
            margin_usdt = Decimal(str(args.margin_usdt))
        if args.max_notional is not None:
            max_notional = Decimal(str(args.max_notional))
    except (InvalidOperation, ValueError):
        print("ERROR: numeric arguments must be valid decimals", file=sys.stderr)
        return 1

    inst_id = symbol_to_inst_id(args.symbol)
    try:
        meta = fetch_public_instrument(settings, inst_id)
        mark_price = fetch_public_mark_price(settings, inst_id)
    except Exception as exc:
        print(f"ERROR: failed to fetch OKX public market data: {exc}", file=sys.stderr)
        return 1

    result = feasibility_from_settings(
        settings,
        inst_id=inst_id,
        meta=meta,
        mark_price=mark_price,
        margin_usdt=margin_usdt,
        max_notional_usdt=max_notional,
        leverage=args.leverage,
    )
    print_feasibility_report(result)
    return 0 if result.feasible else 2


if __name__ == "__main__":
    raise SystemExit(main())

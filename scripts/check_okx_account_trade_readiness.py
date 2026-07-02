#!/usr/bin/env python3
"""Read-only OKX account trade readiness check (v6.5.5).

Queries account config/balance to help diagnose canary open-order failures.
Does not place orders. Never prints API secrets or passphrases.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.exchanges.okx import OkxExchange, OkxRestClient

ACCOUNT_LEVEL_LABELS = {
    "1": "Simple",
    "2": "Single-currency margin",
    "3": "Multi-currency margin",
    "4": "Portfolio margin",
}


def _first_row(payload: dict) -> dict:
    rows = payload.get("data") or []
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    return {}


def _available_usdt(balance_payload: dict) -> str:
    rows = balance_payload.get("data") or []
    if not isinstance(rows, list):
        return "n/a"
    for row in rows:
        if not isinstance(row, dict):
            continue
        details = row.get("details") or []
        if not isinstance(details, list):
            continue
        for detail in details:
            if not isinstance(detail, dict):
                continue
            if str(detail.get("ccy", "")).upper() == "USDT":
                for key in ("availBal", "availEq", "cashBal"):
                    value = detail.get(key)
                    if value not in {None, ""}:
                        return format(Decimal(str(value)), "f")
    return "n/a"


def _tdmode_suggestion(pos_mode: str, configured_td_mode: str) -> str:
    pos_mode = pos_mode.strip().lower()
    configured = configured_td_mode.strip().lower()
    if configured in {"isolated", "cross"}:
        base = configured
    else:
        base = "isolated"
    if pos_mode == "long_short_mode":
        return f"{base} (account posMode=long_short_mode; ensure order posSide/tdMode match account setup)"
    if pos_mode == "net_mode":
        return f"{base} (account posMode=net_mode; canary uses net close-position)"
    return base


def main(argv: list[str] | None = None) -> int:
    _ = argv
    settings = Settings()
    if settings.exchange.strip().lower() != "okx":
        print("ERROR: EXCHANGE must be okx for OKX account trade readiness check.", file=sys.stderr)
        return 1
    if not settings.okx_api_key or not settings.okx_api_secret or not settings.okx_api_passphrase:
        print("ERROR: OKX API credentials must be configured in .env", file=sys.stderr)
        return 1

    client = OkxRestClient(settings)
    exchange = OkxExchange(settings)
    configured_td_mode = settings.okx_td_mode.strip().lower()

    try:
        config_payload = client.request("GET", "/api/v5/account/config")
        balance_payload = client.request("GET", "/api/v5/account/balance")
        _ = exchange.get_positions("BTCUSDT")
        positions_ok = "yes"
    except Exception as exc:
        print(f"ERROR: failed to query OKX account readiness: {exc}", file=sys.stderr)
        return 1

    config_row = _first_row(config_payload)
    acct_lv = str(config_row.get("acctLv") or "unknown")
    pos_mode = str(config_row.get("posMode") or "unknown")
    account_mode = ACCOUNT_LEVEL_LABELS.get(acct_lv, f"level_{acct_lv}")
    available_usdt = _available_usdt(balance_payload)
    tdmode_suggestion = _tdmode_suggestion(pos_mode, configured_td_mode)

    print(f"account_mode={account_mode}")
    print(f"pos_mode={pos_mode}")
    print(f"available_usdt={available_usdt}")
    print(f"okx_td_mode={configured_td_mode}")
    print(f"tdmode_suggestion={tdmode_suggestion}")
    print(f"positions_query_ok={positions_ok}")
    print(f"okx_simulated_trading={str(settings.okx_simulated_trading).lower()}")
    print(f"okx_readonly_mode={str(settings.okx_readonly_mode).lower()}")
    print(f"okx_live_trading_enabled={str(settings.okx_live_trading_enabled).lower()}")
    print("api_secret=configured (value not printed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

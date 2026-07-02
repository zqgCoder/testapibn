from __future__ import annotations

"""OKX symbol <-> instId mapping helpers."""

SYMBOL_TO_INST_ID: dict[str, str] = {
    "BTCUSDT": "BTC-USDT-SWAP",
    "ETHUSDT": "ETH-USDT-SWAP",
    "SOLUSDT": "SOL-USDT-SWAP",
}

INST_ID_TO_SYMBOL: dict[str, str] = {inst: sym for sym, inst in SYMBOL_TO_INST_ID.items()}


def clean_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace("BINANCE:", "").replace(".P", "")


def symbol_to_inst_id(symbol: str) -> str:
    """Map internal symbol (e.g. BTCUSDT) to OKX instId (e.g. BTC-USDT-SWAP)."""
    cleaned = clean_symbol(symbol)
    if cleaned in SYMBOL_TO_INST_ID:
        return SYMBOL_TO_INST_ID[cleaned]
    if cleaned in INST_ID_TO_SYMBOL or cleaned.endswith("-SWAP"):
        return cleaned
    raise ValueError(
        f"Unsupported OKX symbol {symbol!r}; supported: {sorted(SYMBOL_TO_INST_ID)}"
    )


def inst_id_to_symbol(inst_id: str) -> str:
    """Map OKX instId back to internal symbol when known."""
    return INST_ID_TO_SYMBOL.get(inst_id, inst_id)

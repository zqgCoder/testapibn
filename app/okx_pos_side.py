from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings

OKX_POS_MODE_LONG_SHORT = "long_short_mode"
OKX_POS_MODE_NET = "net_mode"
OKX_CANARY_POS_SIDE_LONG = "long"
OKX_CANARY_OPEN_SIDE = "buy"
OKX_CANARY_CLOSE_SIDE = "sell"


def normalize_okx_pos_mode(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if value in {OKX_POS_MODE_LONG_SHORT, OKX_POS_MODE_NET}:
        return value
    return OKX_POS_MODE_NET


def configured_canary_pos_side(settings: Settings) -> str:
    return settings.okx_pos_side.strip().lower()


def resolve_canary_pos_side(settings: Settings, account_pos_mode: str | None) -> str | None:
    """Return posSide for long_short_mode canary orders; None for net_mode."""
    pos_mode = normalize_okx_pos_mode(account_pos_mode)
    pos_side = configured_canary_pos_side(settings)
    if pos_side != OKX_CANARY_POS_SIDE_LONG:
        return None
    if pos_mode == OKX_POS_MODE_LONG_SHORT:
        return OKX_CANARY_POS_SIDE_LONG
    return None


def resolve_canary_close_side(pos_side: str | None) -> str:
    if pos_side == OKX_CANARY_POS_SIDE_LONG:
        return OKX_CANARY_CLOSE_SIDE
    if pos_side == "short":
        return OKX_CANARY_OPEN_SIDE
    return OKX_CANARY_CLOSE_SIDE


def canary_order_shape(account_pos_mode: str | None, pos_side: str | None) -> str:
    pos_mode = normalize_okx_pos_mode(account_pos_mode)
    if pos_mode == OKX_POS_MODE_LONG_SHORT and pos_side == OKX_CANARY_POS_SIDE_LONG:
        return "open buy long / close sell long"
    return "open buy / close net"


def recommended_pos_side(account_pos_mode: str | None, settings: Settings) -> str:
    pos_mode = normalize_okx_pos_mode(account_pos_mode)
    configured = configured_canary_pos_side(settings)
    if pos_mode == OKX_POS_MODE_LONG_SHORT and configured == OKX_CANARY_POS_SIDE_LONG:
        return OKX_CANARY_POS_SIDE_LONG
    return "net"

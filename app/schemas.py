from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Side = Literal["buy", "sell", "long", "short", "BUY", "SELL", "LONG", "SHORT"]
WorkingType = Literal["MARK_PRICE", "CONTRACT_PRICE"]
RiskMode = Literal["manual", "fixed_pct", "fixed_usdt"]
PositionPolicy = Literal["replace", "reverse_only", "ignore_same_side", "add", "reject"]
EntryType = Literal["market", "limit", "MARKET", "LIMIT"]


class TakeProfitLevel(BaseModel):
    price: Decimal = Field(gt=Decimal("0"), description="Take-profit trigger price")
    qty_pct: Decimal = Field(gt=Decimal("0"), le=Decimal("1"), description="Position fraction to close")


class TradingViewSignal(BaseModel):
    """
    Signal sent by TradingView Alert Message.

    risk_mode="manual":
      - Use margin_usdt * leverage or notional_usdt.

    risk_mode="fixed_pct" / "fixed_usdt":
      - Use the real futures account balance and stop-loss distance to calculate quantity.
      - Fees are included in the worst-case stop-loss estimate.
      - leverage may be omitted or set to 0; the bot will calculate it from the selected margin budget.
      - If margin_usdt is omitted, the bot uses AUTO_MARGIN_BALANCE_PCT from .env.
    """

    secret: str
    source: str | None = Field(default=None, description="Signal source, e.g. tradingview or tv_sandbox")
    signal_id: str | None = Field(default=None, description="Optional idempotency key from TradingView/Pine")
    symbol: str
    side: Side

    risk_mode: RiskMode = Field(default="manual")

    # Manual mode fields
    margin_usdt: Decimal | None = Field(default=None, gt=Decimal("0"))
    notional_usdt: Decimal | None = Field(default=None, gt=Decimal("0"))
    leverage: int | None = Field(default=1, ge=0, le=125)

    # Risk-based sizing fields
    risk_pct: Decimal | None = Field(default=None, gt=Decimal("0"), le=Decimal("1"), description="0.01 means 1% of account balance")
    risk_usdt: Decimal | None = Field(default=None, gt=Decimal("0"), description="Fixed max loss in USDT")
    fee_rate: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("0.01"), description="Optional taker fee override, e.g. 0.0005")
    max_leverage: int | None = Field(default=None, ge=1, le=125, description="Optional signal-level leverage cap")

    sl: Decimal | None = Field(default=None, gt=Decimal("0"), description="Stop-loss trigger price")
    tps: list[TakeProfitLevel] = Field(default_factory=list)

    cancel_before_open: bool = Field(default=True, description="Cancel old open regular/algo orders for this symbol before opening")
    dry_run: bool = Field(default=False, description="Override: validate and log without submitting orders")
    working_type: WorkingType = Field(default="MARK_PRICE")
    position_policy: PositionPolicy | None = Field(
        default=None,
        description="Existing-position handling: replace, reject, add, reverse_only, ignore_same_side. Defaults to DEFAULT_POSITION_POLICY in .env.",
    )
    position_strategy: str | None = Field(
        default=None,
        description="Production alias for position_policy: replace, reject, add.",
    )
    sent_at: str | int | float | None = Field(default=None, description="Signal send time (ISO or Unix)")
    timestamp: str | int | float | None = Field(default=None, description="Alias for sent_at")
    time: str | int | float | None = Field(default=None, description="Alias for sent_at")
    entry_price: Decimal | None = Field(default=None, gt=Decimal("0"), description="Reference entry price for SL/TP validation")
    price: Decimal | None = Field(default=None, gt=Decimal("0"), description="Alias reference price")
    close: Decimal | None = Field(default=None, gt=Decimal("0"), description="Alias reference price from bar close")

    # ===== V4 entry execution fields =====
    entry_type: EntryType | None = Field(
        default=None,
        description="Entry execution type: market or limit. Defaults to DEFAULT_ENTRY_TYPE in .env.",
    )
    signal_price: Decimal | None = Field(
        default=None,
        gt=Decimal("0"),
        description="Optional TradingView/Pine reference price used for market slippage checks.",
    )
    limit_price: Decimal | None = Field(
        default=None,
        gt=Decimal("0"),
        description="Limit order price. Required when entry_type=limit.",
    )
    limit_timeout_sec: int | None = Field(
        default=None,
        ge=0,
        le=3600,
        description="Seconds to wait for a limit entry to fill before canceling.",
    )
    limit_fallback_to_market: bool | None = Field(
        default=None,
        description="If true, convert unfilled limit entry to market entry after timeout.",
    )
    max_slippage_pct: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=Decimal("10"),
        description="Max allowed market-entry slippage percent between signal_price and Binance latest price. Example: 0.3 = 0.3%.",
    )

    @field_validator("symbol")
    @classmethod
    def clean_symbol(cls, value: str) -> str:
        s = value.strip().upper().replace("BINANCE:", "").replace(".P", "")
        if not s:
            raise ValueError("symbol is empty")
        return s

    @field_validator("entry_type")
    @classmethod
    def clean_entry_type(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @model_validator(mode="after")
    def validate_amounts(self):
        total = sum(tp.qty_pct for tp in self.tps)

        effective_entry_type = (self.entry_type or "market").lower()
        if effective_entry_type == "limit" and self.limit_price is None:
            raise ValueError("entry_type=limit requires limit_price")
        if effective_entry_type == "market" and self.limit_price is not None:
            # Keep this strict to catch accidentally stale limit fields in TradingView JSON.
            raise ValueError("entry_type=market should not include limit_price")
        if total > Decimal("1"):
            raise ValueError(f"Sum of tps.qty_pct cannot exceed 1, got {total}")

        if self.risk_mode == "manual":
            if self.margin_usdt is None and self.notional_usdt is None:
                raise ValueError("manual mode requires either margin_usdt or notional_usdt")
            if self.margin_usdt is not None and self.notional_usdt is not None:
                raise ValueError("manual mode: use only one of margin_usdt or notional_usdt, not both")
            if self.leverage is None or self.leverage < 1:
                raise ValueError("manual mode requires leverage >= 1")
            if self.risk_pct is not None or self.risk_usdt is not None:
                raise ValueError("manual mode should not include risk_pct or risk_usdt")

        if self.risk_mode == "fixed_pct":
            if self.sl is None:
                raise ValueError("fixed_pct mode requires sl")
            if self.risk_pct is None:
                raise ValueError("fixed_pct mode requires risk_pct, e.g. 0.01 for 1%")
            if self.risk_usdt is not None:
                raise ValueError("fixed_pct mode should not include risk_usdt")
            if self.notional_usdt is not None:
                raise ValueError("risk mode calculates notional_usdt automatically; do not include notional_usdt")

        if self.risk_mode == "fixed_usdt":
            if self.sl is None:
                raise ValueError("fixed_usdt mode requires sl")
            if self.risk_usdt is None:
                raise ValueError("fixed_usdt mode requires risk_usdt")
            if self.risk_pct is not None:
                raise ValueError("fixed_usdt mode should not include risk_pct")
            if self.notional_usdt is not None:
                raise ValueError("risk mode calculates notional_usdt automatically; do not include notional_usdt")

        return self


def normalize_side(side: str) -> str:
    s = side.upper()
    if s in {"BUY", "LONG"}:
        return "BUY"
    if s in {"SELL", "SHORT"}:
        return "SELL"
    raise ValueError(f"Unsupported side: {side}")


def opposite_side(side: str) -> str:
    return "SELL" if side == "BUY" else "BUY"

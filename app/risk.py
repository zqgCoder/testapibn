from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from .exchange_rules import SymbolRules, floor_to_step
from .schemas import TakeProfitLevel


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    side: str
    close_side: str
    entry_ref_price: Decimal
    notional_usdt: Decimal
    margin_usdt: Decimal | None
    quantity: Decimal
    leverage: int
    stop_loss_price: Decimal | None
    take_profits: list[tuple[Decimal, Decimal]]  # [(trigger_price, quantity)]
    working_type: str
    dry_run: bool
    risk_mode: str

    # V4 entry and optional audit fields (defaults must follow non-default fields).
    entry_type: str = "market"
    limit_price: Decimal | None = None
    account_asset: str | None = None
    account_balance: Decimal | None = None
    account_available_balance: Decimal | None = None
    selected_balance: Decimal | None = None
    target_risk_usdt: Decimal | None = None
    estimated_price_loss_usdt: Decimal | None = None
    estimated_fees_usdt: Decimal | None = None
    estimated_total_loss_at_sl: Decimal | None = None
    fee_rate_used: Decimal | None = None
    max_leverage_allowed: int | None = None


def ceil_decimal_to_int(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def calculate_notional(margin_usdt: Decimal | None, notional_usdt: Decimal | None, leverage: int) -> Decimal:
    if notional_usdt is not None:
        return notional_usdt
    assert margin_usdt is not None
    return margin_usdt * Decimal(leverage)


def validate_price_levels(side: str, entry_price: Decimal, sl: Decimal | None, tps: list[TakeProfitLevel]) -> None:
    if side == "BUY":
        if sl is not None and sl >= entry_price:
            raise ValueError(f"做多止损价必须低于当前价：entry={entry_price}, sl={sl}")
        for tp in tps:
            if tp.price <= entry_price:
                raise ValueError(f"做多止盈价必须高于当前价：entry={entry_price}, tp={tp.price}")
    else:
        if sl is not None and sl <= entry_price:
            raise ValueError(f"做空止损价必须高于当前价：entry={entry_price}, sl={sl}")
        for tp in tps:
            if tp.price >= entry_price:
                raise ValueError(f"做空止盈价必须低于当前价：entry={entry_price}, tp={tp.price}")


def allocate_tp_quantities(total_qty: Decimal, tps: list[TakeProfitLevel], step: Decimal) -> list[Decimal]:
    """
    Round TP quantities down to exchange step size.
    If TP percentages sum exactly to 1, assign the rounding remainder to the last TP level.
    """
    if not tps:
        return []

    total_pct = sum(tp.qty_pct for tp in tps)
    quantities: list[Decimal] = []
    used = Decimal("0")

    for idx, tp in enumerate(tps):
        is_last = idx == len(tps) - 1
        if is_last and total_pct == Decimal("1"):
            qty = floor_to_step(total_qty - used, step)
        else:
            qty = floor_to_step(total_qty * tp.qty_pct, step)
        quantities.append(qty)
        used += qty

    return quantities


def estimate_stop_loss_loss(quantity: Decimal, entry_price: Decimal, sl_price: Decimal | None, fee_rate: Decimal) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """
    Estimate worst-case loss if stop loss is triggered by a market close.
    Includes both opening taker fee and stop-loss closing taker fee.
    It does not include slippage, funding fee, or partial TP effects.
    """
    if sl_price is None:
        return None, None, None
    price_loss = quantity * abs(entry_price - sl_price)
    fees = quantity * (entry_price + sl_price) * fee_rate
    total = price_loss + fees
    return price_loss, fees, total


def _validate_quantity_notional(quantity: Decimal, entry_ref_price: Decimal, rules: SymbolRules) -> None:
    if quantity <= 0:
        raise ValueError(f"计算出的下单数量无效：{quantity}")
    if rules.min_qty and quantity < rules.min_qty:
        raise ValueError(f"下单数量小于交易所最小数量：quantity={quantity}, min_qty={rules.min_qty}")
    if rules.min_notional and quantity * entry_ref_price < rules.min_notional:
        raise ValueError(
            f"名义价值小于交易所最小限制：notional={quantity * entry_ref_price}, min_notional={rules.min_notional}"
        )


def build_manual_trade_plan(
    symbol: str,
    side: str,
    close_side: str,
    entry_ref_price: Decimal,
    margin_usdt: Decimal | None,
    notional_usdt: Decimal | None,
    leverage: int,
    sl: Decimal | None,
    tps: list[TakeProfitLevel],
    rules: SymbolRules,
    working_type: str,
    dry_run: bool,
    fee_rate_used: Decimal | None = None,
    entry_type: str = "market",
    limit_price: Decimal | None = None,
) -> TradePlan:
    validate_price_levels(side, entry_ref_price, sl, tps)

    notional = calculate_notional(margin_usdt, notional_usdt, leverage)
    quantity = floor_to_step(notional / entry_ref_price, rules.market_qty_step)
    _validate_quantity_notional(quantity, entry_ref_price, rules)

    sl_price = floor_to_step(sl, rules.price_tick) if sl is not None else None
    price_loss, fees, total_loss = estimate_stop_loss_loss(quantity, entry_ref_price, sl_price, fee_rate_used or Decimal("0"))

    tp_quantities = allocate_tp_quantities(quantity, tps, rules.market_qty_step)
    take_profits = []
    for tp, qty in zip(tps, tp_quantities):
        if qty <= 0:
            continue
        take_profits.append((floor_to_step(tp.price, rules.price_tick), qty))

    actual_margin = margin_usdt if margin_usdt is not None else floor_to_step(notional / Decimal(leverage), Decimal("0.00000001"))

    return TradePlan(
        symbol=symbol,
        side=side,
        close_side=close_side,
        entry_ref_price=entry_ref_price,
        notional_usdt=notional,
        margin_usdt=actual_margin,
        quantity=quantity,
        leverage=leverage,
        stop_loss_price=sl_price,
        take_profits=take_profits,
        working_type=working_type,
        dry_run=dry_run,
        risk_mode="manual",
        entry_type=entry_type,
        limit_price=limit_price,
        estimated_price_loss_usdt=price_loss,
        estimated_fees_usdt=fees,
        estimated_total_loss_at_sl=total_loss,
        fee_rate_used=fee_rate_used,
    )


def build_risk_based_trade_plan(
    symbol: str,
    side: str,
    close_side: str,
    entry_ref_price: Decimal,
    risk_mode: str,
    risk_pct: Decimal | None,
    risk_usdt: Decimal | None,
    margin_usdt: Decimal | None,
    sl: Decimal,
    tps: list[TakeProfitLevel],
    rules: SymbolRules,
    working_type: str,
    dry_run: bool,
    account_asset: str,
    account_balance: Decimal,
    account_available_balance: Decimal,
    selected_balance: Decimal,
    fee_rate_used: Decimal,
    max_leverage_allowed: int,
    auto_margin_balance_pct: Decimal,
    entry_type: str = "market",
    limit_price: Decimal | None = None,
) -> TradePlan:
    validate_price_levels(side, entry_ref_price, sl, tps)

    sl_price = floor_to_step(sl, rules.price_tick)
    target_risk = risk_usdt if risk_mode == "fixed_usdt" else selected_balance * (risk_pct or Decimal("0"))
    assert target_risk is not None

    per_unit_price_loss = abs(entry_ref_price - sl_price)
    per_unit_fees = (entry_ref_price + sl_price) * fee_rate_used
    per_unit_total_loss = per_unit_price_loss + per_unit_fees
    if per_unit_total_loss <= 0:
        raise ValueError("止损距离或手续费计算异常，无法计算仓位")

    raw_qty = target_risk / per_unit_total_loss
    quantity = floor_to_step(raw_qty, rules.market_qty_step)
    _validate_quantity_notional(quantity, entry_ref_price, rules)

    notional = quantity * entry_ref_price
    margin_budget = margin_usdt
    if margin_budget is None:
        margin_budget = selected_balance * auto_margin_balance_pct
    if margin_budget <= 0:
        raise ValueError("保证金预算无效，请设置 margin_usdt 或 AUTO_MARGIN_BALANCE_PCT")
    if margin_budget > account_available_balance:
        raise ValueError(f"保证金预算超过可用余额：margin_usdt={margin_budget}, available={account_available_balance}")

    leverage = max(1, ceil_decimal_to_int(notional / margin_budget))
    if leverage > max_leverage_allowed:
        raise ValueError(
            f"当前止损和风险设置需要约 {leverage}x 杠杆，但允许上限是 {max_leverage_allowed}x。"
            f"请降低 risk、放宽止损、增加 margin_usdt，或提高 MAX_AUTO_LEVERAGE。"
        )

    price_loss, fees, total_loss = estimate_stop_loss_loss(quantity, entry_ref_price, sl_price, fee_rate_used)

    tp_quantities = allocate_tp_quantities(quantity, tps, rules.market_qty_step)
    take_profits = []
    for tp, qty in zip(tps, tp_quantities):
        if qty <= 0:
            continue
        take_profits.append((floor_to_step(tp.price, rules.price_tick), qty))

    actual_margin = floor_to_step(notional / Decimal(leverage), Decimal("0.00000001"))

    return TradePlan(
        symbol=symbol,
        side=side,
        close_side=close_side,
        entry_ref_price=entry_ref_price,
        notional_usdt=notional,
        margin_usdt=actual_margin,
        quantity=quantity,
        leverage=leverage,
        stop_loss_price=sl_price,
        take_profits=take_profits,
        working_type=working_type,
        dry_run=dry_run,
        risk_mode=risk_mode,
        entry_type=entry_type,
        limit_price=limit_price,
        account_asset=account_asset,
        account_balance=account_balance,
        account_available_balance=account_available_balance,
        selected_balance=selected_balance,
        target_risk_usdt=target_risk,
        estimated_price_loss_usdt=price_loss,
        estimated_fees_usdt=fees,
        estimated_total_loss_at_sl=total_loss,
        fee_rate_used=fee_rate_used,
        max_leverage_allowed=max_leverage_allowed,
    )

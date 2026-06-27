from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, replace
from decimal import Decimal

from .binance_client import BinanceAPIError, BinanceClient
from .config import Settings
from .exchange_rules import ExchangeRules, floor_to_step, normalize_market_quantity
from .risk import (
    TradePlan,
    allocate_tp_quantities,
    build_manual_trade_plan,
    build_risk_based_trade_plan,
    validate_price_levels,
)
from .schemas import TradingViewSignal, normalize_side, opposite_side

logger = logging.getLogger(__name__)


class Trader:
    def __init__(self, settings: Settings, client: BinanceClient, rules: ExchangeRules):
        self.settings = settings
        self.client = client
        self.rules = rules

    def _check_symbol_allowed(self, symbol: str) -> None:
        allowed = self.settings.allowed_symbol_set
        if allowed and symbol not in allowed:
            raise ValueError(f"Symbol {symbol} is not in ALLOWED_SYMBOLS={sorted(allowed)}")

    def _account_snapshot(self) -> tuple[str, Decimal, Decimal, Decimal]:
        """
        Return (asset, balance, available_balance, selected_balance).
        selected_balance is controlled by RISK_BALANCE_FIELD.
        """
        asset = self.settings.risk_balance_asset.upper()
        data = self.client.asset_balance(asset)
        balance = Decimal(str(data.get("balance", "0")))
        available = Decimal(str(data.get("availableBalance", "0")))
        selected = available if self.settings.risk_balance_field == "availableBalance" else balance
        if selected <= 0:
            raise ValueError(f"账户 {asset} 余额不足或读取异常：balance={balance}, available={available}")
        return asset, balance, available, selected

    def _account_snapshot_if_possible(self) -> tuple[str, Decimal, Decimal, Decimal] | None:
        if not self.settings.binance_api_key or not self.settings.binance_api_secret:
            return None
        try:
            return self._account_snapshot()
        except Exception as exc:
            logger.warning("Failed to read account balance for audit: %s", exc)
            return None

    def _fee_rate(self, symbol: str, override: Decimal | None) -> Decimal:
        if override is not None:
            base = override
        else:
            try:
                data = self.client.commission_rate(symbol)
                base = Decimal(str(data.get("takerCommissionRate", self.settings.default_taker_fee_rate)))
            except Exception as exc:
                logger.warning(
                    "Failed to read commission rate for %s, fallback to DEFAULT_TAKER_FEE_RATE=%s: %s",
                    symbol,
                    self.settings.default_taker_fee_rate,
                    exc,
                )
                base = Decimal(str(self.settings.default_taker_fee_rate))

        return base * Decimal(str(self.settings.fee_safety_multiplier))

    def _exchange_max_leverage(self, symbol: str, notional: Decimal) -> int:
        """
        Read Binance notional/leverage brackets and find the max initial leverage for this planned notional.
        Falls back to 125 if the endpoint is unavailable, but final POST /fapi/v1/leverage can still reject it.
        """
        try:
            data = self.client.leverage_brackets(symbol)
            items = data if isinstance(data, list) else [data]
            symbol_item = None
            for item in items:
                if str(item.get("symbol", "")).upper() == symbol.upper():
                    symbol_item = item
                    break
            if not symbol_item and items:
                symbol_item = items[0]
            brackets = symbol_item.get("brackets", []) if symbol_item else []
            if not brackets:
                return 125
            for bracket in brackets:
                floor = Decimal(str(bracket.get("notionalFloor", "0")))
                cap = Decimal(str(bracket.get("notionalCap", "999999999999")))
                if notional >= floor and notional <= cap:
                    return int(bracket.get("initialLeverage", 125))
            return int(brackets[-1].get("initialLeverage", 1))
        except Exception as exc:
            logger.warning("Failed to read leverage brackets for %s, fallback max=125: %s", symbol, exc)
            return 125

    @staticmethod
    def _client_order_id(prefix: str) -> str:
        return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"[:36]

    @staticmethod
    def _position_side_from_amount(position_amt: Decimal) -> str | None:
        if position_amt > 0:
            return "BUY"
        if position_amt < 0:
            return "SELL"
        return None

    @staticmethod
    def _decimal_from_order(order: dict, *keys: str) -> Decimal:
        for key in keys:
            value = order.get(key)
            if value not in {None, "", "0", "0.0", "0.00"}:
                try:
                    return Decimal(str(value))
                except Exception:
                    continue
        return Decimal("0")

    def _cancel_symbol_orders(self, symbol: str, responses: dict) -> None:
        try:
            responses["orders"]["cancel_regular"] = self.client.cancel_all_open_orders(symbol)
        except Exception as exc:
            logger.warning("Failed to cancel regular open orders for %s: %s", symbol, exc)
            responses["orders"]["cancel_regular_error"] = str(exc)
        try:
            responses["orders"]["cancel_algo"] = self.client.cancel_all_algo_open_orders(symbol)
        except Exception as exc:
            logger.warning("Failed to cancel algo open orders for %s: %s", symbol, exc)
            responses["orders"]["cancel_algo_error"] = str(exc)

    def _entry_type(self, signal: TradingViewSignal) -> str:
        value = (signal.entry_type or self.settings.default_entry_type or "market").lower()
        if value not in {"market", "limit"}:
            raise ValueError(f"entry_type 只支持 market 或 limit，当前={value}")
        if value == "market" and not self.settings.allow_market_entry:
            raise ValueError("当前配置 ALLOW_MARKET_ENTRY=false，禁止市价进场")
        if value == "limit" and not self.settings.allow_limit_entry:
            raise ValueError("当前配置 ALLOW_LIMIT_ENTRY=false，禁止限价进场")
        return value

    def _limit_timeout_sec(self, signal: TradingViewSignal) -> int:
        return int(signal.limit_timeout_sec if signal.limit_timeout_sec is not None else self.settings.default_limit_timeout_sec)

    def _limit_fallback_to_market(self, signal: TradingViewSignal) -> bool:
        if signal.limit_fallback_to_market is not None:
            return bool(signal.limit_fallback_to_market)
        return bool(self.settings.default_limit_fallback_to_market)

    def _max_slippage_pct(self, signal: TradingViewSignal) -> Decimal:
        if signal.max_slippage_pct is not None:
            return Decimal(str(signal.max_slippage_pct))
        return Decimal(str(self.settings.default_max_slippage_pct))

    def _new_entry_summary(self, plan: TradePlan) -> dict:
        return {
            "entry_type": plan.entry_type,
            "requested_qty": format(plan.quantity, "f"),
            "filled_qty": "0",
            "remaining_qty": format(plan.quantity, "f"),
            "fallback_attempted": False,
            "fallback_executed": False,
            "skip_reason": None,
            "latest_price_used": None,
        }

    def _check_market_slippage(
        self,
        side: str,
        signal_price: Decimal | None,
        latest_price: Decimal,
        max_slippage_pct: Decimal,
        *,
        context: str = "market_entry",
    ) -> None:
        if signal_price is None:
            logger.info(
                "no_signal_price_slippage_check_skipped: context=%s latest=%s",
                context,
                latest_price,
            )
            return
        if max_slippage_pct <= 0:
            return

        threshold = max_slippage_pct / Decimal("100")
        if side == "BUY":
            max_allowed = signal_price * (Decimal("1") + threshold)
            if latest_price > max_allowed:
                raise ValueError(
                    f"市价进场不利滑点过大：signal_price={signal_price}, latest={latest_price}, "
                    f"max_allowed={max_allowed}, max_slippage_pct={max_slippage_pct}%"
                )
            if latest_price < signal_price:
                logger.info(
                    "Favorable slippage for BUY: signal_price=%s latest=%s context=%s",
                    signal_price,
                    latest_price,
                    context,
                )
        else:
            min_allowed = signal_price * (Decimal("1") - threshold)
            if latest_price < min_allowed:
                raise ValueError(
                    f"市价进场不利滑点过大：signal_price={signal_price}, latest={latest_price}, "
                    f"min_allowed={min_allowed}, max_slippage_pct={max_slippage_pct}%"
                )
            if latest_price > signal_price:
                logger.info(
                    "Favorable slippage for SELL: signal_price=%s latest=%s context=%s",
                    signal_price,
                    latest_price,
                    context,
                )

    def _validate_entry_levels_at_price(
        self,
        plan: TradePlan,
        signal: TradingViewSignal,
        latest_price: Decimal,
    ) -> None:
        validate_price_levels(plan.side, latest_price, plan.stop_loss_price, signal.tps)

    def _can_fallback_to_market(self, signal: TradingViewSignal) -> bool:
        if self.settings.allow_market_entry:
            return True
        logger.warning("Limit fallback to market blocked: ALLOW_MARKET_ENTRY=false")
        return False

    def _normalize_fallback_remaining_qty(
        self,
        symbol: str,
        remaining_qty: Decimal,
        latest_price: Decimal,
    ) -> Decimal | None:
        rules = self.rules.get(symbol)
        return normalize_market_quantity(remaining_qty, rules, latest_price)

    def _prepare_market_entry(
        self,
        signal: TradingViewSignal,
        plan: TradePlan,
        latest_price: Decimal,
        *,
        context: str = "market_entry",
    ) -> None:
        self._check_market_slippage(
            plan.side,
            signal.signal_price,
            latest_price,
            self._max_slippage_pct(signal),
            context=context,
        )
        self._validate_entry_levels_at_price(plan, signal, latest_price)

    def _execute_market_fallback(
        self,
        signal: TradingViewSignal,
        plan: TradePlan,
        qty: Decimal,
        entry_summary: dict,
        responses: dict,
        response_key: str,
    ) -> tuple[Decimal, bool]:
        """Attempt market fallback after limit entry. Returns (filled_qty, executed)."""
        entry_summary["fallback_attempted"] = True

        if not self._can_fallback_to_market(signal):
            entry_summary["skip_reason"] = "limit_unfilled_market_disabled"
            logger.warning(
                "Limit fallback skipped (market disabled): symbol=%s side=%s qty=%s",
                plan.symbol,
                plan.side,
                qty,
            )
            return Decimal("0"), False

        latest_price = self.client.ticker_price(plan.symbol)
        entry_summary["latest_price_used"] = format(latest_price, "f")

        normalized_qty = self._normalize_fallback_remaining_qty(plan.symbol, qty, latest_price)
        if normalized_qty is None:
            entry_summary["skip_reason"] = "remaining_qty_below_exchange_minimum"
            logger.warning(
                "Limit fallback skipped (qty below exchange minimum): symbol=%s raw_qty=%s latest=%s",
                plan.symbol,
                qty,
                latest_price,
            )
            return Decimal("0"), False

        try:
            self._prepare_market_entry(signal, plan, latest_price, context="limit_fallback")
        except ValueError as exc:
            entry_summary["skip_reason"] = str(exc)
            logger.warning("Limit fallback aborted by pre-check: symbol=%s error=%s", plan.symbol, exc)
            return Decimal("0"), False

        responses["orders"][response_key] = self.client.new_market_order(
            symbol=plan.symbol,
            side=plan.side,
            quantity=normalized_qty,
            client_order_id=self._client_order_id("tvfallback"),
        )
        fallback_qty = self._decimal_from_order(responses["orders"][response_key], "executedQty", "cumQty") or normalized_qty
        entry_summary["fallback_executed"] = True
        logger.info(
            "Limit fallback market order executed: symbol=%s qty=%s filled=%s",
            plan.symbol,
            normalized_qty,
            fallback_qty,
        )
        return fallback_qty, True

    def _resolve_entry_reference_price(
        self,
        signal: TradingViewSignal,
        side: str,
        latest_price: Decimal,
    ) -> tuple[str, Decimal, Decimal | None]:
        """Return (entry_type, entry_ref_price, limit_price)."""
        symbol_rules = self.rules.get(signal.symbol)
        entry_type = self._entry_type(signal)
        if entry_type == "limit":
            if signal.limit_price is None:
                raise ValueError("entry_type=limit requires limit_price")
            limit_price = floor_to_step(signal.limit_price, symbol_rules.price_tick)
            if limit_price <= 0:
                raise ValueError(f"limit_price 无效：{limit_price}")
            return entry_type, limit_price, limit_price

        # Market entry: use Binance latest price for sizing and validate optional TradingView reference price.
        self._check_market_slippage(
            side,
            signal.signal_price,
            latest_price,
            self._max_slippage_pct(signal),
            context="prepare_plan",
        )
        return entry_type, latest_price, None

    def _handle_existing_position(self, signal: TradingViewSignal, plan: TradePlan, responses: dict) -> bool:
        """Handle existing position before opening a new one.

        Returns True when the new order should continue.
        Returns False when the signal should be skipped safely.
        """
        policy = signal.position_policy or self.settings.default_position_policy
        responses["position_policy"] = policy

        current_amt = self.client.current_position_amount(plan.symbol)
        current_side = self._position_side_from_amount(current_amt)
        responses["current_position"] = {
            "symbol": plan.symbol,
            "positionAmt": format(current_amt, "f"),
            "side": current_side,
        }

        if current_side is None:
            if signal.cancel_before_open:
                self._cancel_symbol_orders(plan.symbol, responses)
            return True

        same_side = current_side == plan.side

        if policy == "add":
            logger.warning(
                "position_policy=add: keeping existing position amount=%s side=%s before opening new %s order",
                current_amt,
                current_side,
                plan.side,
            )
            if signal.cancel_before_open:
                self._cancel_symbol_orders(plan.symbol, responses)
            return True

        if policy in {"reverse_only", "ignore_same_side"} and same_side:
            responses["skipped"] = True
            responses["skip_reason"] = "same_side_position_exists"
            logger.warning(
                "Signal skipped because same-side position already exists: symbol=%s current_side=%s positionAmt=%s policy=%s",
                plan.symbol,
                current_side,
                current_amt,
                policy,
            )
            return False

        # replace: close any existing position.
        # reverse_only / ignore_same_side: close opposite position.
        if signal.cancel_before_open:
            self._cancel_symbol_orders(plan.symbol, responses)

        responses["orders"]["close_existing_position"] = self.client.close_position_market(
            symbol=plan.symbol,
            position_amt=current_amt,
            client_order_id=self._client_order_id("tvclose"),
        )
        logger.info(
            "Existing position closed before new entry: symbol=%s amount=%s policy=%s",
            plan.symbol,
            current_amt,
            policy,
        )
        return True

    def prepare_plan(self, signal: TradingViewSignal) -> TradePlan:
        symbol = signal.symbol
        self._check_symbol_allowed(symbol)

        side = normalize_side(signal.side)
        close_side = opposite_side(side)
        latest_price = self.client.ticker_price(symbol)
        symbol_rules = self.rules.get(symbol)
        entry_type, entry_ref_price, limit_price = self._resolve_entry_reference_price(signal, side, latest_price)
        dry_run = signal.dry_run or not self.settings.enable_trading

        if signal.risk_mode == "manual":
            fee_rate = self._fee_rate(symbol, signal.fee_rate) if (signal.fee_rate is not None or self.settings.binance_api_key) else Decimal(str(self.settings.default_taker_fee_rate))
            plan = build_manual_trade_plan(
                symbol=symbol,
                side=side,
                close_side=close_side,
                entry_ref_price=entry_ref_price,
                margin_usdt=signal.margin_usdt,
                notional_usdt=signal.notional_usdt,
                leverage=int(signal.leverage or 1),
                sl=signal.sl,
                tps=signal.tps,
                rules=symbol_rules,
                working_type=signal.working_type,
                dry_run=dry_run,
                fee_rate_used=fee_rate,
                entry_type=entry_type,
                limit_price=limit_price,
            )
            snapshot = self._account_snapshot_if_possible()
            if snapshot:
                asset, balance, available, selected = snapshot
                plan = replace(
                    plan,
                    account_asset=asset,
                    account_balance=balance,
                    account_available_balance=available,
                    selected_balance=selected,
                )
        else:
            asset, balance, available, selected = self._account_snapshot()
            fee_rate = self._fee_rate(symbol, signal.fee_rate)
            bot_cap = int(self.settings.max_auto_leverage)
            signal_cap = int(signal.max_leverage) if signal.max_leverage is not None else bot_cap
            max_leverage_allowed = min(bot_cap, signal_cap)

            plan = build_risk_based_trade_plan(
                symbol=symbol,
                side=side,
                close_side=close_side,
                entry_ref_price=entry_ref_price,
                risk_mode=signal.risk_mode,
                risk_pct=signal.risk_pct,
                risk_usdt=signal.risk_usdt,
                margin_usdt=signal.margin_usdt,
                sl=signal.sl,  # model validator guarantees not None in risk mode
                tps=signal.tps,
                rules=symbol_rules,
                working_type=signal.working_type,
                dry_run=dry_run,
                account_asset=asset,
                account_balance=balance,
                account_available_balance=available,
                selected_balance=selected,
                fee_rate_used=fee_rate,
                max_leverage_allowed=max_leverage_allowed,
                auto_margin_balance_pct=Decimal(str(self.settings.auto_margin_balance_pct)),
                entry_type=entry_type,
                limit_price=limit_price,
            )

        bracket_max = self._exchange_max_leverage(symbol, plan.notional_usdt) if self.settings.binance_api_key else 125
        configured_max = plan.max_leverage_allowed or int(signal.max_leverage or self.settings.max_auto_leverage or 125)
        final_max = min(bracket_max, configured_max)
        if plan.leverage > final_max:
            raise ValueError(
                f"计划杠杆 {plan.leverage}x 超过允许上限 {final_max}x。"
                f"当前名义价值={plan.notional_usdt}，交易所档位上限={bracket_max}x，配置/信号上限={configured_max}x。"
            )
        plan = replace(plan, max_leverage_allowed=final_max)

        return plan

    def _plan_for_filled_quantity(self, signal: TradingViewSignal, plan: TradePlan, filled_qty: Decimal) -> TradePlan:
        """Rebuild TP quantities if a limit order partially fills or falls back."""
        if filled_qty <= 0:
            return plan
        symbol_rules = self.rules.get(plan.symbol)
        tp_quantities = allocate_tp_quantities(filled_qty, signal.tps, symbol_rules.market_qty_step)
        take_profits: list[tuple[Decimal, Decimal]] = []
        for tp, qty in zip(signal.tps, tp_quantities):
            if qty <= 0:
                continue
            take_profits.append((floor_to_step(tp.price, symbol_rules.price_tick), qty))
        return replace(
            plan,
            quantity=filled_qty,
            notional_usdt=filled_qty * plan.entry_ref_price,
            take_profits=take_profits,
        )

    def _wait_for_limit_fill(self, plan: TradePlan, order: dict, timeout_sec: int) -> dict:
        """Poll a Binance limit order until it reaches a final state or times out."""
        final_statuses = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
        order_id = order.get("orderId")
        client_id = order.get("clientOrderId")
        latest = order
        deadline = time.time() + max(0, timeout_sec)

        while True:
            status = str(latest.get("status", "")).upper()
            if status in final_statuses:
                return latest
            if time.time() >= deadline:
                return latest
            time.sleep(min(float(self.settings.limit_poll_interval_sec), max(0.0, deadline - time.time())))
            try:
                latest = self.client.get_order(plan.symbol, order_id=order_id, orig_client_order_id=client_id)
            except Exception as exc:
                logger.warning("Failed to poll limit order status: symbol=%s orderId=%s err=%s", plan.symbol, order_id, exc)
                return latest

    def _submit_entry_order(self, signal: TradingViewSignal, plan: TradePlan, responses: dict) -> tuple[bool, TradePlan]:
        """Submit market or limit entry. Returns (entry_filled, effective_plan)."""
        entry_summary = self._new_entry_summary(plan)
        responses["entry_summary"] = entry_summary

        if plan.entry_type == "market":
            latest_price = self.client.ticker_price(plan.symbol)
            entry_summary["latest_price_used"] = format(latest_price, "f")
            self._prepare_market_entry(signal, plan, latest_price, context="market_submit")

            responses["orders"]["open"] = self.client.new_market_order(
                symbol=plan.symbol,
                side=plan.side,
                quantity=plan.quantity,
                client_order_id=self._client_order_id("tvopen"),
            )
            filled_qty = self._decimal_from_order(responses["orders"]["open"], "executedQty", "cumQty") or plan.quantity
            entry_summary["filled_qty"] = format(filled_qty, "f")
            entry_summary["remaining_qty"] = "0"
            return True, self._plan_for_filled_quantity(signal, plan, filled_qty)

        # Limit entry: submit, wait for fill, then place TP/SL only for filled quantity.
        assert plan.limit_price is not None
        timeout_sec = self._limit_timeout_sec(signal)
        fallback_to_market = self._limit_fallback_to_market(signal)
        limit_order = self.client.new_limit_order(
            symbol=plan.symbol,
            side=plan.side,
            quantity=plan.quantity,
            price=plan.limit_price,
            client_order_id=self._client_order_id("tvlimit"),
        )
        responses["orders"]["open_limit"] = limit_order
        final_order = self._wait_for_limit_fill(plan, limit_order, timeout_sec)
        responses["orders"]["open_limit_final"] = final_order

        status = str(final_order.get("status", "")).upper()
        filled_qty = self._decimal_from_order(final_order, "executedQty", "cumQty")

        if status != "FILLED":
            try:
                responses["orders"]["open_limit_cancel"] = self.client.cancel_order(
                    plan.symbol,
                    order_id=final_order.get("orderId") or limit_order.get("orderId"),
                    orig_client_order_id=final_order.get("clientOrderId") or limit_order.get("clientOrderId"),
                )
                cancel_order = responses["orders"]["open_limit_cancel"]
                filled_qty = max(filled_qty, self._decimal_from_order(cancel_order, "executedQty", "cumQty"))
            except Exception as exc:
                # Order may have filled between polling and cancel. Query once more before giving up.
                responses["orders"]["open_limit_cancel_error"] = str(exc)
                logger.warning("Failed to cancel unfilled limit order for %s: %s", plan.symbol, exc)
                try:
                    checked = self.client.get_order(
                        plan.symbol,
                        order_id=final_order.get("orderId") or limit_order.get("orderId"),
                        orig_client_order_id=final_order.get("clientOrderId") or limit_order.get("clientOrderId"),
                    )
                    responses["orders"]["open_limit_after_cancel_check"] = checked
                    filled_qty = max(filled_qty, self._decimal_from_order(checked, "executedQty", "cumQty"))
                except Exception as check_exc:
                    responses["orders"]["open_limit_after_cancel_check_error"] = str(check_exc)

        remaining_qty = plan.quantity - filled_qty
        if remaining_qty < 0:
            remaining_qty = Decimal("0")

        entry_summary["filled_qty"] = format(filled_qty, "f")
        entry_summary["remaining_qty"] = format(remaining_qty, "f")

        if status == "FILLED":
            return True, self._plan_for_filled_quantity(signal, plan, filled_qty)

        if fallback_to_market:
            if filled_qty <= 0:
                fallback_qty, _ = self._execute_market_fallback(
                    signal,
                    plan,
                    plan.quantity,
                    entry_summary,
                    responses,
                    "open_market_fallback",
                )
                if fallback_qty > 0:
                    filled_qty = fallback_qty
                    remaining_qty = plan.quantity - filled_qty
                    if remaining_qty < 0:
                        remaining_qty = Decimal("0")
                    entry_summary["filled_qty"] = format(filled_qty, "f")
                    entry_summary["remaining_qty"] = format(remaining_qty, "f")
            elif remaining_qty > 0:
                fallback_qty, _ = self._execute_market_fallback(
                    signal,
                    plan,
                    remaining_qty,
                    entry_summary,
                    responses,
                    "open_market_fallback_remaining",
                )
                if fallback_qty > 0:
                    filled_qty += fallback_qty
                    remaining_qty = plan.quantity - filled_qty
                    if remaining_qty < 0:
                        remaining_qty = Decimal("0")
                    entry_summary["filled_qty"] = format(filled_qty, "f")
                    entry_summary["remaining_qty"] = format(remaining_qty, "f")

        if filled_qty <= 0:
            if not entry_summary["skip_reason"]:
                entry_summary["skip_reason"] = "limit_entry_not_filled"
            responses["skipped"] = True
            responses["skip_reason"] = entry_summary["skip_reason"]
            logger.warning(
                "Limit entry not filled. No TP/SL submitted: symbol=%s side=%s price=%s timeout=%ss fallback=%s skip_reason=%s",
                plan.symbol,
                plan.side,
                plan.limit_price,
                timeout_sec,
                fallback_to_market,
                entry_summary["skip_reason"],
            )
            return False, plan

        return True, self._plan_for_filled_quantity(signal, plan, filled_qty)

    def execute(self, signal: TradingViewSignal, signal_key: str) -> dict:
        plan = self.prepare_plan(signal)
        logger.info("Trade plan: %s", json.dumps(asdict(plan), default=str, ensure_ascii=False))

        if plan.dry_run:
            logger.warning("DRY_RUN active. No Binance orders submitted. Set ENABLE_TRADING=true to trade.")
            return {"dry_run": True, "plan": asdict(plan)}

        responses: dict = {"plan": asdict(plan), "orders": {}}

        should_continue = self._handle_existing_position(signal, plan, responses)
        if not should_continue:
            logger.info("Execution skipped safely: %s", json.dumps(responses, default=str, ensure_ascii=False))
            return responses

        responses["orders"]["leverage"] = self.client.change_leverage(plan.symbol, plan.leverage)

        entry_filled, effective_plan = self._submit_entry_order(signal, plan, responses)
        if not entry_filled:
            logger.info("Execution ended without entry fill: %s", json.dumps(responses, default=str, ensure_ascii=False))
            return responses

        # Store updated plan when a limit order partially fills or falls back to market.
        if effective_plan.quantity != plan.quantity or effective_plan.take_profits != plan.take_profits:
            responses["effective_plan"] = asdict(effective_plan)

        if effective_plan.stop_loss_price is not None:
            responses["orders"]["stop_loss"] = self.client.stop_loss_close_position(
                symbol=effective_plan.symbol,
                close_side=effective_plan.close_side,
                trigger_price=effective_plan.stop_loss_price,
                working_type=effective_plan.working_type,
            )

        tp_responses = []
        for idx, (tp_price, tp_qty) in enumerate(effective_plan.take_profits, start=1):
            tp_responses.append(
                self.client.take_profit_market(
                    symbol=effective_plan.symbol,
                    close_side=effective_plan.close_side,
                    trigger_price=tp_price,
                    quantity=tp_qty,
                    working_type=effective_plan.working_type,
                )
            )
            logger.info("TP %s submitted: price=%s qty=%s", idx, tp_price, tp_qty)
        responses["orders"]["take_profits"] = tp_responses

        logger.info("Execution result: %s", json.dumps(responses, default=str, ensure_ascii=False))
        return responses

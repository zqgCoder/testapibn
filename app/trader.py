from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, replace
from decimal import Decimal

from .account_risk import AccountRiskGuard
from .binance_client import BinanceAPIError, BinanceClient
from .config import Settings
from .exchange_rules import ExchangeRules, SymbolRules, floor_to_step, normalize_market_quantity
from .risk import (
    TradePlan,
    allocate_tp_quantities,
    build_manual_trade_plan,
    build_risk_based_trade_plan,
    calculate_quantity_for_target_risk,
    ceil_decimal_to_int,
    estimate_stop_loss_loss,
    estimate_used_risk_at_entry,
    validate_price_levels,
)
from .storage import AccountRiskStore
from .runtime_control import RuntimeControl
from .schemas import TradingViewSignal, normalize_side, opposite_side
from .tv_sandbox import build_tv_skip_result, is_tv_signal, validate_tv_policy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FallbackRecalcResult:
    final_order_qty: Decimal | None
    fallback_plan: TradePlan | None
    skip_reason: str | None
    used_risk_usdt: Decimal | None
    remaining_risk_usdt: Decimal | None
    estimated_total_loss_usdt: Decimal | None
    recalculated_qty: Decimal | None
    recalc_error: str | None = None


class Trader:
    def __init__(
        self,
        settings: Settings,
        client: BinanceClient,
        rules: ExchangeRules,
        account_risk: AccountRiskGuard | None = None,
        account_risk_store: AccountRiskStore | None = None,
        runtime_control: RuntimeControl | None = None,
    ):
        self.settings = settings
        self.client = client
        self.rules = rules
        self.runtime_control = runtime_control
        if account_risk is not None:
            self.account_risk = account_risk
        else:
            store = account_risk_store or AccountRiskStore(settings.sqlite_path)
            self.account_risk = AccountRiskGuard(settings, client, store)

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

    def cancel_symbol_open_orders(self, symbol: str) -> dict:
        """Cancel regular and algo/conditional open orders for a symbol."""
        result: dict = {"regular": None, "algo": None}
        try:
            result["regular"] = self.client.cancel_all_open_orders(symbol)
        except Exception as exc:
            logger.warning("Failed to cancel regular open orders for %s: %s", symbol, exc)
            result["regular_error"] = str(exc)
        try:
            result["algo"] = self.client.cancel_all_algo_open_orders(symbol)
        except Exception as exc:
            logger.warning("Failed to cancel algo open orders for %s: %s", symbol, exc)
            result["algo_error"] = str(exc)
        return result

    def _cancel_symbol_orders(self, symbol: str, responses: dict) -> None:
        cancel_result = self.cancel_symbol_open_orders(symbol)
        responses["orders"]["cancel_regular"] = cancel_result.get("regular")
        if cancel_result.get("regular_error"):
            responses["orders"]["cancel_regular_error"] = cancel_result["regular_error"]
        responses["orders"]["cancel_algo"] = cancel_result.get("algo")
        if cancel_result.get("algo_error"):
            responses["orders"]["cancel_algo_error"] = cancel_result["algo_error"]

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
            "fallback_recalculated": False,
            "fallback_original_entry_ref_price": format(plan.entry_ref_price, "f"),
            "fallback_latest_price": None,
            "fallback_original_requested_qty": format(plan.quantity, "f"),
            "fallback_recalculated_qty": None,
            "fallback_final_order_qty": None,
            "fallback_target_risk_usdt": None,
            "fallback_estimated_total_loss_usdt": None,
            "fallback_remaining_risk_usdt": None,
            "fallback_used_risk_usdt": None,
            "fallback_recalculated_leverage": None,
            "fallback_recalc_error": None,
        }

    @staticmethod
    def _is_exchange_minimum_recalc_error(message: str) -> bool:
        markers = (
            "min_notional",
            "min_qty",
            "最小数量",
            "最小限制",
            "名义价值小于",
            "下单数量无效",
            "下单数量小于",
        )
        return any(marker in message for marker in markers)

    def _map_fallback_skip_reason(self, error: str) -> str:
        if self._is_exchange_minimum_recalc_error(error):
            return "fallback_recalculated_qty_below_exchange_minimum"
        return error

    def _apply_fallback_recalc_failure_to_entry_summary(
        self,
        entry_summary: dict,
        *,
        latest_price: Decimal | None,
        skip_reason: str,
        recalc_error: str | None = None,
    ) -> None:
        """Failure terminal state: never sets fallback_recalculated=true or success-only fields."""
        entry_summary["fallback_recalculated"] = False
        entry_summary["fallback_executed"] = False
        entry_summary["skip_reason"] = skip_reason
        entry_summary["fallback_recalc_error"] = recalc_error
        if latest_price is not None:
            entry_summary["fallback_latest_price"] = format(latest_price, "f")
        entry_summary["fallback_recalculated_qty"] = None
        entry_summary["fallback_final_order_qty"] = None
        entry_summary["fallback_target_risk_usdt"] = None
        entry_summary["fallback_estimated_total_loss_usdt"] = None
        entry_summary["fallback_remaining_risk_usdt"] = None
        entry_summary["fallback_used_risk_usdt"] = None
        entry_summary["fallback_recalculated_leverage"] = None

    def _apply_fallback_recalc_success_to_entry_summary(
        self,
        entry_summary: dict,
        original_plan: TradePlan,
        latest_price: Decimal,
        recalc: FallbackRecalcResult,
        applied_leverage: int,
    ) -> None:
        """Success terminal state: only called after recalc, pre-check, and market order succeed."""
        entry_summary["fallback_recalculated"] = True
        entry_summary["fallback_executed"] = True
        entry_summary["skip_reason"] = None
        entry_summary["fallback_recalc_error"] = None
        entry_summary["fallback_latest_price"] = format(latest_price, "f")
        entry_summary["fallback_recalculated_qty"] = (
            format(recalc.recalculated_qty, "f") if recalc.recalculated_qty is not None else None
        )
        entry_summary["fallback_final_order_qty"] = (
            format(recalc.final_order_qty, "f") if recalc.final_order_qty is not None else None
        )
        entry_summary["fallback_target_risk_usdt"] = (
            format(original_plan.target_risk_usdt, "f") if original_plan.target_risk_usdt is not None else None
        )
        entry_summary["fallback_estimated_total_loss_usdt"] = (
            format(recalc.estimated_total_loss_usdt, "f") if recalc.estimated_total_loss_usdt is not None else None
        )
        entry_summary["fallback_remaining_risk_usdt"] = (
            format(recalc.remaining_risk_usdt, "f") if recalc.remaining_risk_usdt is not None else None
        )
        entry_summary["fallback_used_risk_usdt"] = (
            format(recalc.used_risk_usdt, "f") if recalc.used_risk_usdt is not None else None
        )
        entry_summary["fallback_recalculated_leverage"] = applied_leverage

        logger.info(
            "Limit fallback risk recalculated:\n"
            "original_entry_ref_price=%s\n"
            "latest_price=%s\n"
            "original_qty=%s\n"
            "recalculated_qty=%s\n"
            "final_order_qty=%s\n"
            "target_risk=%s\n"
            "estimated_loss=%s\n"
            "used_risk=%s\n"
            "remaining_risk=%s\n"
            "applied_leverage=%s",
            original_plan.entry_ref_price,
            latest_price,
            original_plan.quantity,
            recalc.recalculated_qty,
            recalc.final_order_qty,
            original_plan.target_risk_usdt,
            recalc.estimated_total_loss_usdt,
            recalc.used_risk_usdt,
            recalc.remaining_risk_usdt,
            applied_leverage,
        )

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

    def _limit_entry_avg_price(self, plan: TradePlan, *orders: dict | None) -> Decimal:
        for order in orders:
            if not order:
                continue
            avg = self._decimal_from_order(order, "avgPrice")
            if avg > 0:
                return avg
        return plan.limit_price or plan.entry_ref_price

    def _build_manual_fallback_plan(
        self,
        signal: TradingViewSignal,
        original_plan: TradePlan,
        latest_price: Decimal,
        rules: SymbolRules,
    ) -> TradePlan:
        return build_manual_trade_plan(
            symbol=original_plan.symbol,
            side=original_plan.side,
            close_side=original_plan.close_side,
            entry_ref_price=latest_price,
            margin_usdt=original_plan.margin_usdt,
            notional_usdt=original_plan.notional_usdt,
            leverage=original_plan.leverage,
            sl=signal.sl,
            tps=signal.tps,
            rules=rules,
            working_type=original_plan.working_type,
            dry_run=original_plan.dry_run,
            fee_rate_used=original_plan.fee_rate_used,
            entry_type="market",
            limit_price=original_plan.limit_price,
        )

    def _build_risk_fallback_plan(
        self,
        signal: TradingViewSignal,
        original_plan: TradePlan,
        latest_price: Decimal,
        rules: SymbolRules,
    ) -> TradePlan:
        assert original_plan.account_asset is not None
        assert original_plan.account_balance is not None
        assert original_plan.account_available_balance is not None
        assert original_plan.selected_balance is not None
        assert original_plan.fee_rate_used is not None
        assert original_plan.max_leverage_allowed is not None
        assert signal.sl is not None

        return build_risk_based_trade_plan(
            symbol=original_plan.symbol,
            side=original_plan.side,
            close_side=original_plan.close_side,
            entry_ref_price=latest_price,
            risk_mode=original_plan.risk_mode,
            risk_pct=signal.risk_pct,
            risk_usdt=signal.risk_usdt,
            margin_usdt=signal.margin_usdt,
            sl=signal.sl,
            tps=signal.tps,
            rules=rules,
            working_type=original_plan.working_type,
            dry_run=original_plan.dry_run,
            account_asset=original_plan.account_asset,
            account_balance=original_plan.account_balance,
            account_available_balance=original_plan.account_available_balance,
            selected_balance=original_plan.selected_balance,
            fee_rate_used=original_plan.fee_rate_used,
            max_leverage_allowed=original_plan.max_leverage_allowed,
            auto_margin_balance_pct=Decimal(str(self.settings.auto_margin_balance_pct)),
            entry_type="market",
            limit_price=original_plan.limit_price,
        )

    def _build_partial_risk_fallback_plan(
        self,
        original_plan: TradePlan,
        latest_price: Decimal,
        fallback_qty: Decimal,
    ) -> TradePlan:
        assert original_plan.stop_loss_price is not None
        assert original_plan.fee_rate_used is not None
        assert original_plan.margin_usdt is not None

        notional = fallback_qty * latest_price
        leverage = max(1, ceil_decimal_to_int(notional / original_plan.margin_usdt))
        if original_plan.max_leverage_allowed is not None:
            leverage = min(leverage, original_plan.max_leverage_allowed)

        price_loss, fees, total_loss = estimate_stop_loss_loss(
            fallback_qty,
            latest_price,
            original_plan.stop_loss_price,
            original_plan.fee_rate_used,
        )
        return replace(
            original_plan,
            entry_ref_price=latest_price,
            quantity=fallback_qty,
            notional_usdt=notional,
            leverage=leverage,
            margin_usdt=floor_to_step(notional / Decimal(leverage), Decimal("0.00000001")),
            estimated_price_loss_usdt=price_loss,
            estimated_fees_usdt=fees,
            estimated_total_loss_at_sl=total_loss,
            entry_type="market",
        )

    def _recalculate_fallback_market_plan(
        self,
        signal: TradingViewSignal,
        original_plan: TradePlan,
        latest_price: Decimal,
        already_filled_qty: Decimal,
        already_filled_avg_price: Decimal,
        rules: SymbolRules,
    ) -> FallbackRecalcResult:
        try:
            validate_price_levels(
                original_plan.side,
                latest_price,
                original_plan.stop_loss_price,
                signal.tps,
            )
        except ValueError as exc:
            detail = str(exc)
            return FallbackRecalcResult(
                None,
                None,
                self._map_fallback_skip_reason(detail),
                None,
                None,
                None,
                None,
                detail,
            )

        fee_rate = original_plan.fee_rate_used or Decimal("0")
        sl_price = original_plan.stop_loss_price
        used_risk = Decimal("0")
        remaining_risk: Decimal | None = None
        target_risk = original_plan.target_risk_usdt

        if (
            already_filled_qty > 0
            and sl_price is not None
            and original_plan.risk_mode in {"fixed_usdt", "fixed_pct"}
            and target_risk is not None
        ):
            used_risk = estimate_used_risk_at_entry(
                already_filled_qty,
                already_filled_avg_price,
                sl_price,
                fee_rate,
            )
            remaining_risk = target_risk - used_risk
            if remaining_risk <= 0:
                logger.warning(
                    "Limit fallback remaining risk exhausted: symbol=%s target_risk=%s used_risk=%s",
                    original_plan.symbol,
                    target_risk,
                    used_risk,
                )
                return FallbackRecalcResult(
                    None,
                    None,
                    "fallback_remaining_risk_exhausted",
                    used_risk,
                    remaining_risk,
                    used_risk,
                    None,
                )

        recalculated_qty: Decimal
        fallback_plan: TradePlan

        try:
            if original_plan.risk_mode == "manual":
                fallback_plan = self._build_manual_fallback_plan(signal, original_plan, latest_price, rules)
                if already_filled_qty > 0:
                    recalculated_qty = fallback_plan.quantity - already_filled_qty
                    if recalculated_qty <= 0:
                        detail = (
                            f"manual fallback remaining qty non-positive after recalculation: "
                            f"recalculated_total={fallback_plan.quantity}, already_filled={already_filled_qty}"
                        )
                        return FallbackRecalcResult(
                            None,
                            None,
                            "fallback_recalculated_qty_below_exchange_minimum",
                            used_risk if used_risk > 0 else None,
                            remaining_risk,
                            None,
                            fallback_plan.quantity,
                            detail,
                        )
                else:
                    recalculated_qty = fallback_plan.quantity
            else:
                assert sl_price is not None
                if already_filled_qty > 0 and remaining_risk is not None:
                    recalculated_qty = calculate_quantity_for_target_risk(
                        remaining_risk,
                        latest_price,
                        sl_price,
                        fee_rate,
                        rules,
                        validate=False,
                    )
                    fallback_plan = original_plan
                else:
                    fallback_plan = self._build_risk_fallback_plan(signal, original_plan, latest_price, rules)
                    recalculated_qty = fallback_plan.quantity
        except ValueError as exc:
            detail = str(exc)
            return FallbackRecalcResult(
                None,
                None,
                self._map_fallback_skip_reason(detail),
                used_risk if used_risk > 0 else None,
                remaining_risk,
                None,
                None,
                detail,
            )

        final_order_qty = normalize_market_quantity(recalculated_qty, rules, latest_price)
        if final_order_qty is None:
            detail = (
                f"recalculated_qty={recalculated_qty} below exchange minimum at latest_price={latest_price}, "
                f"min_qty={rules.min_qty}, min_notional={rules.min_notional}"
            )
            return FallbackRecalcResult(
                None,
                fallback_plan,
                "fallback_recalculated_qty_below_exchange_minimum",
                used_risk if used_risk > 0 else None,
                remaining_risk,
                None,
                recalculated_qty,
                detail,
            )

        if original_plan.risk_mode != "manual" and already_filled_qty > 0 and remaining_risk is not None:
            fallback_plan = self._build_partial_risk_fallback_plan(original_plan, latest_price, final_order_qty)
        elif original_plan.risk_mode == "manual":
            fallback_plan = replace(fallback_plan, quantity=final_order_qty, notional_usdt=final_order_qty * latest_price)
        else:
            fallback_plan = replace(fallback_plan, quantity=final_order_qty, notional_usdt=final_order_qty * latest_price)

        estimated_total: Decimal | None
        if (
            already_filled_qty > 0
            and sl_price is not None
            and original_plan.risk_mode in {"fixed_usdt", "fixed_pct"}
            and target_risk is not None
        ):
            _, _, fallback_loss = estimate_stop_loss_loss(final_order_qty, latest_price, sl_price, fee_rate)
            estimated_total = used_risk + (fallback_loss or Decimal("0"))
            if estimated_total > target_risk:
                logger.warning(
                    "Limit fallback total estimated loss slightly above target due to precision/fees: "
                    "symbol=%s target_risk=%s estimated_total=%s",
                    original_plan.symbol,
                    target_risk,
                    estimated_total,
                )
        else:
            estimated_total = fallback_plan.estimated_total_loss_at_sl

        return FallbackRecalcResult(
            final_order_qty=final_order_qty,
            fallback_plan=fallback_plan,
            skip_reason=None,
            used_risk_usdt=used_risk if used_risk > 0 else None,
            remaining_risk_usdt=remaining_risk,
            estimated_total_loss_usdt=estimated_total,
            recalculated_qty=recalculated_qty,
        )

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
        already_filled_qty: Decimal,
        already_filled_avg_price: Decimal,
        entry_summary: dict,
        responses: dict,
        response_key: str,
    ) -> tuple[Decimal, bool]:
        """Attempt market fallback after limit entry. Returns (filled_qty, executed)."""
        entry_summary["fallback_attempted"] = True

        if not self._can_fallback_to_market(signal):
            self._apply_fallback_recalc_failure_to_entry_summary(
                entry_summary,
                latest_price=None,
                skip_reason="limit_unfilled_market_disabled",
            )
            logger.warning(
                "Limit fallback skipped (market disabled): symbol=%s side=%s already_filled_qty=%s",
                plan.symbol,
                plan.side,
                already_filled_qty,
            )
            return Decimal("0"), False

        latest_price = self.client.ticker_price(plan.symbol)
        entry_summary["latest_price_used"] = format(latest_price, "f")
        rules = self.rules.get(plan.symbol)

        try:
            recalc = self._recalculate_fallback_market_plan(
                signal,
                plan,
                latest_price,
                already_filled_qty,
                already_filled_avg_price,
                rules,
            )
        except ValueError as exc:
            detail = str(exc)
            self._apply_fallback_recalc_failure_to_entry_summary(
                entry_summary,
                latest_price=latest_price,
                skip_reason=self._map_fallback_skip_reason(detail),
                recalc_error=detail,
            )
            logger.warning("Limit fallback recalculation failed: symbol=%s error=%s", plan.symbol, exc)
            return Decimal("0"), False

        if recalc.skip_reason:
            self._apply_fallback_recalc_failure_to_entry_summary(
                entry_summary,
                latest_price=latest_price,
                skip_reason=recalc.skip_reason,
                recalc_error=recalc.recalc_error,
            )
            logger.warning(
                "Limit fallback skipped after recalculation: symbol=%s skip_reason=%s recalc_error=%s",
                plan.symbol,
                recalc.skip_reason,
                recalc.recalc_error,
            )
            return Decimal("0"), False

        assert recalc.final_order_qty is not None
        assert recalc.fallback_plan is not None

        try:
            self._prepare_market_entry(signal, plan, latest_price, context="limit_fallback")
        except ValueError as exc:
            detail = str(exc)
            self._apply_fallback_recalc_failure_to_entry_summary(
                entry_summary,
                latest_price=latest_price,
                skip_reason=self._map_fallback_skip_reason(detail),
                recalc_error=detail,
            )
            logger.warning("Limit fallback aborted by pre-check: symbol=%s error=%s", plan.symbol, exc)
            return Decimal("0"), False

        applied_leverage = max(plan.leverage, recalc.fallback_plan.leverage)
        if applied_leverage != plan.leverage:
            responses["orders"]["fallback_leverage"] = self.client.change_leverage(plan.symbol, applied_leverage)
            logger.info(
                "Limit fallback leverage updated: symbol=%s original=%s recalculated=%s applied=%s",
                plan.symbol,
                plan.leverage,
                recalc.fallback_plan.leverage,
                applied_leverage,
            )

        responses["orders"][response_key] = self.client.new_market_order(
            symbol=plan.symbol,
            side=plan.side,
            quantity=recalc.final_order_qty,
            client_order_id=self._client_order_id("tvfallback"),
        )
        fallback_qty = self._decimal_from_order(responses["orders"][response_key], "executedQty", "cumQty") or recalc.final_order_qty

        self._apply_fallback_recalc_success_to_entry_summary(
            entry_summary,
            plan,
            latest_price,
            recalc,
            applied_leverage,
        )
        logger.info(
            "Limit fallback market order executed: symbol=%s qty=%s filled=%s",
            plan.symbol,
            recalc.final_order_qty,
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
        cancel_order: dict | None = None
        checked_order: dict | None = None

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
                    checked_order = checked
                    responses["orders"]["open_limit_after_cancel_check"] = checked
                    filled_qty = max(filled_qty, self._decimal_from_order(checked, "executedQty", "cumQty"))
                except Exception as check_exc:
                    responses["orders"]["open_limit_after_cancel_check_error"] = str(check_exc)

        limit_avg_price = self._limit_entry_avg_price(plan, final_order, cancel_order, checked_order, limit_order)

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
                    Decimal("0"),
                    limit_avg_price,
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
                    filled_qty,
                    limit_avg_price,
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

    @staticmethod
    def _is_binance_error_code(exc: Exception, code: int) -> bool:
        if not isinstance(exc, BinanceAPIError):
            return False
        text = str(exc)
        return f'"code":{code}' in text or f'"code": {code}' in text

    @staticmethod
    def _position_meets_entry_expectation(
        position_amt: Decimal,
        expected_side: str,
        min_abs_qty: Decimal,
    ) -> bool:
        threshold = min_abs_qty * Decimal("0.8")
        if expected_side == "BUY":
            return position_amt > 0 and abs(position_amt) >= threshold
        return position_amt < 0 and abs(position_amt) >= threshold

    def _wait_for_position_after_entry(
        self,
        symbol: str,
        expected_side: str,
        min_abs_qty: Decimal,
        timeout_sec: float = 5,
        poll_interval_sec: float = 0.5,
    ) -> tuple[bool, Decimal]:
        deadline = time.time() + max(0.0, timeout_sec)
        latest_amt = Decimal("0")

        while True:
            latest_amt = self.client.current_position_amount(symbol)
            if self._position_meets_entry_expectation(latest_amt, expected_side, min_abs_qty):
                return True, latest_amt
            if time.time() >= deadline:
                return False, latest_amt
            time.sleep(min(poll_interval_sec, max(0.0, deadline - time.time())))

    @staticmethod
    def _new_protection_summary() -> dict:
        return {
            "position_confirmed": False,
            "confirmed_position_amt": None,
            "stop_loss_submitted": False,
            "take_profit_submitted_count": 0,
            "protection_skipped_reason": None,
        }

    def _submit_stop_loss_with_retry(self, plan: TradePlan) -> tuple[bool, dict | None, Exception | None]:
        assert plan.stop_loss_price is not None
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                result = self.client.stop_loss_close_position(
                    symbol=plan.symbol,
                    close_side=plan.close_side,
                    trigger_price=plan.stop_loss_price,
                    working_type=plan.working_type,
                )
                return True, result, None
            except BinanceAPIError as exc:
                last_exc = exc
                if not self._is_binance_error_code(exc, -4509) or attempt >= 3:
                    return False, None, exc
                logger.warning(
                    "Stop loss rejected with -4509, waiting 0.5s before retry: symbol=%s attempt=%s/3",
                    plan.symbol,
                    attempt + 1,
                )
                time.sleep(0.5)
                position_amt = self.client.current_position_amount(plan.symbol)
                if not self._position_meets_entry_expectation(position_amt, plan.side, plan.quantity):
                    logger.warning(
                        "Position not visible after -4509: symbol=%s position_amt=%s",
                        plan.symbol,
                        position_amt,
                    )
        return False, None, last_exc

    def _handle_emergency_close_on_protection_fail(
        self,
        plan: TradePlan,
        responses: dict,
        protection_summary: dict,
    ) -> None:
        if not self.settings.emergency_close_on_protection_fail:
            logger.warning(
                "Protection orders incomplete but EMERGENCY_CLOSE_ON_PROTECTION_FAIL=false; "
                "symbol=%s position left open",
                plan.symbol,
            )
            return

        position_amt = self.client.current_position_amount(plan.symbol)
        if position_amt == 0:
            logger.warning(
                "Emergency close skipped (no visible position): symbol=%s",
                plan.symbol,
            )
            return

        logger.error(
            "Emergency close triggered after protection failure: symbol=%s position_amt=%s",
            plan.symbol,
            position_amt,
        )
        responses["orders"]["emergency_close"] = self.client.close_position_market(
            symbol=plan.symbol,
            position_amt=position_amt,
            client_order_id=self._client_order_id("tvemerg"),
        )
        protection_summary["emergency_close_attempted"] = True

    def _submit_protections_after_entry(self, effective_plan: TradePlan, responses: dict) -> None:
        protection_summary = self._new_protection_summary()
        responses["protection_summary"] = protection_summary

        confirmed, position_amt = self._wait_for_position_after_entry(
            effective_plan.symbol,
            effective_plan.side,
            effective_plan.quantity,
        )
        protection_summary["confirmed_position_amt"] = format(position_amt, "f")

        if not confirmed:
            protection_summary["protection_skipped_reason"] = "position_not_available_after_entry"
            responses["protection_skipped_reason"] = "position_not_available_after_entry"
            logger.error(
                "entry may not be filled or position not visible yet: symbol=%s side=%s "
                "expected_qty=%s position_amt=%s",
                effective_plan.symbol,
                effective_plan.side,
                effective_plan.quantity,
                position_amt,
            )
            return

        protection_summary["position_confirmed"] = True

        if effective_plan.stop_loss_price is not None:
            sl_ok, sl_result, sl_exc = self._submit_stop_loss_with_retry(effective_plan)
            if sl_ok and sl_result is not None:
                responses["orders"]["stop_loss"] = sl_result
                protection_summary["stop_loss_submitted"] = True
                logger.info(
                    "Stop loss submitted: symbol=%s trigger=%s",
                    effective_plan.symbol,
                    effective_plan.stop_loss_price,
                )
            else:
                responses["orders"]["stop_loss_error"] = str(sl_exc) if sl_exc else "unknown"
                logger.error(
                    "Stop loss submission failed: symbol=%s error=%s",
                    effective_plan.symbol,
                    sl_exc,
                )

        tp_responses = []
        for idx, (tp_price, tp_qty) in enumerate(effective_plan.take_profits, start=1):
            try:
                tp_responses.append(
                    self.client.take_profit_market(
                        symbol=effective_plan.symbol,
                        close_side=effective_plan.close_side,
                        trigger_price=tp_price,
                        quantity=tp_qty,
                        working_type=effective_plan.working_type,
                    )
                )
                protection_summary["take_profit_submitted_count"] += 1
                logger.info("TP %s submitted: price=%s qty=%s", idx, tp_price, tp_qty)
            except BinanceAPIError as exc:
                logger.error("TP %s submission failed: symbol=%s error=%s", idx, effective_plan.symbol, exc)
                tp_responses.append({"error": str(exc)})
        responses["orders"]["take_profits"] = tp_responses

        sl_required = effective_plan.stop_loss_price is not None
        tp_required = len(effective_plan.take_profits) > 0
        sl_failed = sl_required and not protection_summary["stop_loss_submitted"]
        tp_failed = tp_required and protection_summary["take_profit_submitted_count"] < len(effective_plan.take_profits)
        if sl_failed or tp_failed:
            self._handle_emergency_close_on_protection_fail(effective_plan, responses, protection_summary)

    def execute(
        self,
        signal: TradingViewSignal,
        signal_key: str,
        raw_payload: dict | None = None,
    ) -> dict:
        if self.runtime_control is not None:
            blocked, runtime_summary = self.runtime_control.is_execution_blocked()
            if blocked:
                logger.warning(
                    "Execution skipped by runtime lock: signal_key=%s summary=%s",
                    signal_key,
                    runtime_summary,
                )
                return {
                    "orders": {},
                    "skipped": True,
                    "skip_reason": "runtime_locked",
                    "runtime_summary": runtime_summary,
                }

        payload = raw_payload or {}
        if self.settings.tv_signal_sandbox_enabled and is_tv_signal(payload, self.settings):
            rejection = validate_tv_policy(payload, signal, self.settings)
            if rejection:
                logger.warning(
                    "TV sandbox rejected signal: signal_key=%s skip_reason=%s message=%s",
                    signal_key,
                    rejection.skip_reason,
                    rejection.message,
                )
                return build_tv_skip_result(rejection)

        plan = self.prepare_plan(signal)
        logger.info("Trade plan: %s", json.dumps(asdict(plan), default=str, ensure_ascii=False))

        if plan.dry_run:
            logger.warning("DRY_RUN active. No Binance orders submitted. Set ENABLE_TRADING=true to trade.")
            return {"dry_run": True, "plan": asdict(plan)}

        account_risk_result = self.account_risk.check_before_entry(plan)
        if not account_risk_result.allowed:
            logger.warning(
                "Account risk blocked entry: symbol=%s skip_reason=%s summary=%s",
                plan.symbol,
                account_risk_result.skip_reason,
                AccountRiskGuard.summary_dict(account_risk_result),
            )
            return {
                "plan": asdict(plan),
                "orders": {},
                "skipped": True,
                "skip_reason": account_risk_result.skip_reason,
                "account_risk_summary": AccountRiskGuard.summary_dict(account_risk_result),
            }

        responses: dict = {
            "plan": asdict(plan),
            "orders": {},
            "account_risk_summary": AccountRiskGuard.summary_dict(account_risk_result),
        }

        should_continue = self._handle_existing_position(signal, plan, responses)
        if not should_continue:
            logger.info("Execution skipped safely: %s", json.dumps(responses, default=str, ensure_ascii=False))
            return responses

        responses["orders"]["leverage"] = self.client.change_leverage(plan.symbol, plan.leverage)

        entry_filled, effective_plan = self._submit_entry_order(signal, plan, responses)
        if not entry_filled:
            logger.info("Execution ended without entry fill: %s", json.dumps(responses, default=str, ensure_ascii=False))
            return responses

        self.account_risk.record_successful_open(effective_plan.symbol, effective_plan, signal_key)

        # Store updated plan when a limit order partially fills or falls back to market.
        if effective_plan.quantity != plan.quantity or effective_plan.take_profits != plan.take_profits:
            responses["effective_plan"] = asdict(effective_plan)

        self._submit_protections_after_entry(effective_plan, responses)

        logger.info("Execution result: %s", json.dumps(responses, default=str, ensure_ascii=False))
        return responses

    def _position_row_for_symbol(self, symbol: str) -> dict | None:
        data = self.client.position_risk(symbol)
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if str(row.get("symbol", "")).upper() == symbol.upper():
                return row
        return None

    @staticmethod
    def _position_amount_from_row(row: dict | None) -> Decimal:
        if not row:
            return Decimal("0")
        return Decimal(str(row.get("positionAmt", "0")))

    def _wait_for_position_zero(
        self,
        symbol: str,
        wait_seconds: int,
        poll_interval_sec: float = 0.5,
    ) -> Decimal:
        deadline = time.time() + max(0.0, float(wait_seconds))
        latest_amt = self.client.current_position_amount(symbol)
        while latest_amt != 0 and time.time() < deadline:
            time.sleep(min(poll_interval_sec, max(0.0, deadline - time.time())))
            latest_amt = self.client.current_position_amount(symbol)
        return latest_amt

    def close_position_maintenance(
        self,
        symbol: str,
        *,
        reason: str = "",
        operator: str = "",
        cancel_before_close: bool = True,
        cancel_after_close: bool = True,
        wait_seconds: int = 10,
    ) -> dict:
        self._check_symbol_allowed(symbol)
        position_before_row = self._position_row_for_symbol(symbol)
        position_amt = self._position_amount_from_row(position_before_row)

        cancel_regular_before = None
        cancel_algo_before = None
        cancel_regular_after = None
        cancel_algo_after = None
        close_side: str | None = None
        close_quantity: str | None = None
        close_order = None
        close_error: str | None = None

        if position_amt == 0:
            if cancel_before_close or cancel_after_close:
                cancel_result = self.cancel_symbol_open_orders(symbol)
                cancel_regular_before = cancel_result.get("regular")
                cancel_algo_before = cancel_result.get("algo")
                if cancel_result.get("regular_error"):
                    cancel_regular_before = {"error": cancel_result["regular_error"]}
                if cancel_result.get("algo_error"):
                    cancel_algo_before = {"error": cancel_result["algo_error"]}
            position_after_row = self._position_row_for_symbol(symbol)
            return {
                "symbol": symbol,
                "position_before": position_before_row,
                "close_side": None,
                "close_quantity": None,
                "close_order": None,
                "position_after": position_after_row,
                "cancel_regular_before": cancel_regular_before,
                "cancel_algo_before": cancel_algo_before,
                "cancel_regular_after": cancel_regular_after,
                "cancel_algo_after": cancel_algo_after,
                "success": True,
                "status": "no_position_cleanup_done",
                "reason": reason,
                "operator": operator,
            }

        close_side = "SELL" if position_amt > 0 else "BUY"
        close_quantity = format(abs(position_amt), "f")

        if cancel_before_close:
            cancel_result = self.cancel_symbol_open_orders(symbol)
            cancel_regular_before = cancel_result.get("regular")
            cancel_algo_before = cancel_result.get("algo")
            if cancel_result.get("regular_error"):
                cancel_regular_before = {"error": cancel_result["regular_error"]}
            if cancel_result.get("algo_error"):
                cancel_algo_before = {"error": cancel_result["algo_error"]}

        try:
            close_order = self.client.close_position_market(
                symbol=symbol,
                position_amt=position_amt,
                client_order_id=self._client_order_id("maintclose"),
            )
        except Exception as exc:
            close_error = str(exc)
            logger.error(
                "Maintenance position close failed: symbol=%s operator=%s reason=%s error=%s",
                symbol,
                operator,
                reason,
                exc,
            )
            position_after_row = self._position_row_for_symbol(symbol)
            return {
                "symbol": symbol,
                "position_before": position_before_row,
                "close_side": close_side,
                "close_quantity": close_quantity,
                "close_order": None,
                "close_error": close_error,
                "position_after": position_after_row,
                "cancel_regular_before": cancel_regular_before,
                "cancel_algo_before": cancel_algo_before,
                "cancel_regular_after": cancel_regular_after,
                "cancel_algo_after": cancel_algo_after,
                "success": False,
                "status": "close_order_failed",
                "reason": reason,
                "operator": operator,
            }

        self._wait_for_position_zero(symbol, wait_seconds)
        position_after_row = self._position_row_for_symbol(symbol)
        position_after_amt = self._position_amount_from_row(position_after_row)

        if cancel_after_close:
            cancel_result = self.cancel_symbol_open_orders(symbol)
            cancel_regular_after = cancel_result.get("regular")
            cancel_algo_after = cancel_result.get("algo")
            if cancel_result.get("regular_error"):
                cancel_regular_after = {"error": cancel_result["regular_error"]}
            if cancel_result.get("algo_error"):
                cancel_algo_after = {"error": cancel_result["algo_error"]}

        closed = position_after_amt == 0
        status = "position_closed" if closed else "position_close_incomplete"
        logger.info(
            "Maintenance position close: symbol=%s operator=%s reason=%s status=%s close_side=%s qty=%s",
            symbol,
            operator,
            reason,
            status,
            close_side,
            close_quantity,
        )
        return {
            "symbol": symbol,
            "position_before": position_before_row,
            "close_side": close_side,
            "close_quantity": close_quantity,
            "close_order": close_order,
            "position_after": position_after_row,
            "cancel_regular_before": cancel_regular_before,
            "cancel_algo_before": cancel_algo_before,
            "cancel_regular_after": cancel_regular_after,
            "cancel_algo_after": cancel_algo_after,
            "success": closed,
            "status": status,
            "reason": reason,
            "operator": operator,
        }

    def cleanup_symbol_orders(
        self,
        symbol: str,
        *,
        reason: str = "",
        operator: str = "",
    ) -> dict:
        self._check_symbol_allowed(symbol)
        cancel_result = self.cancel_symbol_open_orders(symbol)
        position_row = self._position_row_for_symbol(symbol)
        open_orders: list | None = None
        algo_orders: list | None = None
        try:
            open_orders = self.client.open_orders(symbol)
        except Exception as exc:
            logger.warning("Failed to fetch open orders after cleanup for %s: %s", symbol, exc)
        try:
            algo_orders = self.client.open_algo_orders(symbol)
        except Exception as exc:
            logger.warning("Failed to fetch algo orders after cleanup for %s: %s", symbol, exc)
        logger.info(
            "Maintenance order cleanup: symbol=%s operator=%s reason=%s",
            symbol,
            operator,
            reason,
        )
        return {
            "symbol": symbol,
            "cancel_regular": cancel_result.get("regular"),
            "cancel_algo": cancel_result.get("algo"),
            "cancel_regular_error": cancel_result.get("regular_error"),
            "cancel_algo_error": cancel_result.get("algo_error"),
            "position": position_row,
            "open_orders": open_orders if isinstance(open_orders, list) else [],
            "algo_orders": algo_orders if isinstance(algo_orders, list) else [],
            "reason": reason,
            "operator": operator,
        }

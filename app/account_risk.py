from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .binance_client import BinanceClient
from .config import Settings
from .risk import TradePlan, estimate_stop_loss_loss
from .storage import AccountRiskStore

logger = logging.getLogger(__name__)

SKIP_DAILY_MAX_LOSS = "daily_max_loss_exceeded"
SKIP_DAILY_MAX_TRADES = "daily_max_trades_exceeded"
SKIP_MAX_OPEN_POSITIONS = "max_open_positions_exceeded"
SKIP_SYMBOL_COOLDOWN = "symbol_cooldown_active"
SKIP_MAX_TOTAL_RISK = "max_total_risk_exceeded"


@dataclass(frozen=True)
class AccountRiskStats:
    daily_realized_loss_usdt: Decimal
    daily_trade_count: int
    open_position_count: int
    open_position_symbols: tuple[str, ...]
    symbol_seconds_since_last_open: int | None
    current_total_estimated_risk_usdt: Decimal
    planned_new_risk_usdt: Decimal
    daily_loss_unavailable: bool = False
    no_sl_penalty_used: bool = False
    no_sl_penalty_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccountRiskCheckResult:
    enabled: bool
    allowed: bool
    skip_reason: str | None
    stats: AccountRiskStats
    limits: dict[str, str | int | float | bool | None]


class AccountRiskGuard:
    def __init__(self, settings: Settings, client: BinanceClient, store: AccountRiskStore):
        self.settings = settings
        self.client = client
        self.store = store

    @staticmethod
    def _day_start_utc(now: datetime | None = None) -> datetime:
        now = now or datetime.now(timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _day_start_for_stats(self, now: datetime | None = None) -> datetime:
        tz_name = (self.settings.account_risk_day_timezone or "UTC").strip()
        now = now or datetime.now(timezone.utc)
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            logger.warning("Invalid ACCOUNT_RISK_DAY_TIMEZONE=%s, fallback to UTC", tz_name)
            tz = timezone.utc
        local_now = now.astimezone(tz)
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_start.astimezone(timezone.utc)

    def _fee_rate_default(self) -> Decimal:
        return Decimal(str(self.settings.default_taker_fee_rate)) * Decimal(str(self.settings.fee_safety_multiplier))

    def _daily_realized_loss_usdt(self, day_start: datetime) -> tuple[Decimal, bool]:
        start_ms = int(day_start.timestamp() * 1000)
        try:
            rows = self.client.income(income_type="REALIZED_PNL", start_time_ms=start_ms)
            loss = Decimal("0")
            for row in rows if isinstance(rows, list) else [rows]:
                income = Decimal(str(row.get("income", "0")))
                if income < 0:
                    loss += abs(income)
            return loss, False
        except Exception as exc:
            logger.warning("Daily realized loss unavailable, rule skipped (fail-open): %s", exc)
            return Decimal("0"), True

    @staticmethod
    def _stop_loss_triggers_by_symbol(algo_orders: list[dict]) -> dict[str, Decimal]:
        by_symbol: dict[str, Decimal] = {}
        for order in algo_orders:
            order_type = str(order.get("orderType", order.get("type", ""))).upper()
            if order_type != "STOP_MARKET":
                continue
            close_position = str(order.get("closePosition", order.get("close_position", "false"))).lower()
            if close_position not in {"true", "1"}:
                continue
            symbol = str(order.get("symbol", "")).upper()
            trigger = order.get("triggerPrice", order.get("stopPrice"))
            if trigger in {None, ""}:
                continue
            by_symbol[symbol] = Decimal(str(trigger))
        return by_symbol

    def _estimate_open_positions_risk(
        self,
        positions: list[dict],
        sl_by_symbol: dict[str, Decimal],
    ) -> tuple[Decimal, bool, tuple[str, ...]]:
        fee_rate = self._fee_rate_default()
        total = Decimal("0")
        penalty_symbols: list[str] = []
        for row in positions:
            symbol = str(row.get("symbol", "")).upper()
            qty = abs(Decimal(str(row.get("positionAmt", "0"))))
            if qty <= 0:
                continue
            entry_price = Decimal(str(row.get("entryPrice", "0")))
            if entry_price <= 0:
                continue
            sl_price = sl_by_symbol.get(symbol)
            if sl_price is not None:
                _, _, est = estimate_stop_loss_loss(qty, entry_price, sl_price, fee_rate)
                total += est or Decimal("0")
            else:
                penalty_pct = Decimal(str(self.settings.account_risk_no_sl_penalty_pct))
                total += qty * entry_price * penalty_pct
                penalty_symbols.append(symbol)
                logger.warning(
                    "Account risk: no SL found for open position %s, using no_sl_penalty_pct=%s",
                    symbol,
                    penalty_pct,
                )
        return total, bool(penalty_symbols), tuple(penalty_symbols)

    def _planned_new_risk_usdt(self, plan: TradePlan) -> Decimal:
        if plan.estimated_total_loss_at_sl is not None:
            return plan.estimated_total_loss_at_sl
        if plan.stop_loss_price is None:
            return Decimal("0")
        fee_rate = plan.fee_rate_used if plan.fee_rate_used is not None else self._fee_rate_default()
        _, _, total = estimate_stop_loss_loss(plan.quantity, plan.entry_ref_price, plan.stop_loss_price, fee_rate)
        return total or Decimal("0")

    def _limits_snapshot(self) -> dict[str, str | int | float | bool | None]:
        return {
            "daily_max_loss_usdt": self.settings.daily_max_loss_usdt,
            "daily_max_trades": self.settings.daily_max_trades,
            "max_open_positions": self.settings.max_open_positions,
            "symbol_cooldown_sec": self.settings.symbol_cooldown_sec,
            "max_total_risk_usdt": self.settings.max_total_risk_usdt,
            "account_risk_no_sl_penalty_pct": self.settings.account_risk_no_sl_penalty_pct,
        }

    def check_before_entry(self, plan: TradePlan) -> AccountRiskCheckResult:
        limits = self._limits_snapshot()
        day_start = self._day_start_for_stats()
        now = datetime.now(timezone.utc)

        if not self.settings.account_risk_enabled:
            empty_stats = AccountRiskStats(
                daily_realized_loss_usdt=Decimal("0"),
                daily_trade_count=0,
                open_position_count=0,
                open_position_symbols=(),
                symbol_seconds_since_last_open=None,
                current_total_estimated_risk_usdt=Decimal("0"),
                planned_new_risk_usdt=Decimal("0"),
            )
            return AccountRiskCheckResult(
                enabled=False,
                allowed=True,
                skip_reason=None,
                stats=empty_stats,
                limits=limits,
            )

        daily_loss, daily_loss_unavailable = self._daily_realized_loss_usdt(day_start)
        daily_trade_count = self.store.count_opens_since(day_start.isoformat())
        positions = self.client.non_zero_positions()
        open_symbols = tuple(sorted(str(row.get("symbol", "")).upper() for row in positions))
        open_position_count = len(open_symbols)
        target_has_position = plan.symbol.upper() in open_symbols

        symbol_seconds_since_last_open: int | None = None
        last_open = self.store.last_open_at(plan.symbol.upper())
        if last_open is not None:
            symbol_seconds_since_last_open = max(0, int((now - last_open).total_seconds()))

        try:
            algo_rows = self.client.open_algo_orders()
            algo_list = algo_rows if isinstance(algo_rows, list) else [algo_rows]
        except Exception as exc:
            logger.warning("Failed to read open algo orders for account risk: %s", exc)
            algo_list = []
        sl_by_symbol = self._stop_loss_triggers_by_symbol(algo_list)
        current_risk, no_sl_penalty_used, no_sl_symbols = self._estimate_open_positions_risk(positions, sl_by_symbol)
        planned_risk = self._planned_new_risk_usdt(plan)

        stats = AccountRiskStats(
            daily_realized_loss_usdt=daily_loss,
            daily_trade_count=daily_trade_count,
            open_position_count=open_position_count,
            open_position_symbols=open_symbols,
            symbol_seconds_since_last_open=symbol_seconds_since_last_open,
            current_total_estimated_risk_usdt=current_risk,
            planned_new_risk_usdt=planned_risk,
            daily_loss_unavailable=daily_loss_unavailable,
            no_sl_penalty_used=no_sl_penalty_used,
            no_sl_penalty_symbols=no_sl_symbols,
        )

        max_loss = Decimal(str(self.settings.daily_max_loss_usdt))
        if max_loss > 0 and not daily_loss_unavailable and daily_loss >= max_loss:
            return AccountRiskCheckResult(True, False, SKIP_DAILY_MAX_LOSS, stats, limits)

        max_trades = int(self.settings.daily_max_trades)
        if max_trades > 0 and daily_trade_count >= max_trades:
            return AccountRiskCheckResult(True, False, SKIP_DAILY_MAX_TRADES, stats, limits)

        max_open = int(self.settings.max_open_positions)
        if max_open > 0 and not target_has_position and open_position_count >= max_open:
            return AccountRiskCheckResult(True, False, SKIP_MAX_OPEN_POSITIONS, stats, limits)

        cooldown = int(self.settings.symbol_cooldown_sec)
        if cooldown > 0 and symbol_seconds_since_last_open is not None and symbol_seconds_since_last_open < cooldown:
            return AccountRiskCheckResult(True, False, SKIP_SYMBOL_COOLDOWN, stats, limits)

        max_total = Decimal(str(self.settings.max_total_risk_usdt))
        if max_total > 0 and (current_risk + planned_risk) > max_total:
            return AccountRiskCheckResult(True, False, SKIP_MAX_TOTAL_RISK, stats, limits)

        return AccountRiskCheckResult(True, True, None, stats, limits)

    def record_successful_open(self, symbol: str, plan: TradePlan, signal_key: str) -> None:
        risk = self._planned_new_risk_usdt(plan)
        self.store.record_successful_open(
            signal_key=signal_key,
            symbol=symbol.upper(),
            planned_risk_usdt=format(risk, "f"),
        )

    @staticmethod
    def summary_dict(result: AccountRiskCheckResult) -> dict:
        stats = result.stats
        limits = result.limits
        return {
            "enabled": result.enabled,
            "allowed": result.allowed,
            "skip_reason": result.skip_reason,
            "daily_realized_loss_usdt": format(stats.daily_realized_loss_usdt, "f"),
            "daily_max_loss_usdt": limits.get("daily_max_loss_usdt"),
            "daily_loss_unavailable": stats.daily_loss_unavailable,
            "daily_trade_count": stats.daily_trade_count,
            "daily_max_trades": limits.get("daily_max_trades"),
            "open_position_count": stats.open_position_count,
            "max_open_positions": limits.get("max_open_positions"),
            "open_position_symbols": list(stats.open_position_symbols),
            "symbol_cooldown_sec": limits.get("symbol_cooldown_sec"),
            "symbol_seconds_since_last_open": stats.symbol_seconds_since_last_open,
            "current_total_estimated_risk_usdt": format(stats.current_total_estimated_risk_usdt, "f"),
            "planned_new_risk_usdt": format(stats.planned_new_risk_usdt, "f"),
            "max_total_risk_usdt": limits.get("max_total_risk_usdt"),
            "account_risk_no_sl_penalty_pct": limits.get("account_risk_no_sl_penalty_pct"),
            "no_sl_penalty_used": stats.no_sl_penalty_used,
            "no_sl_penalty_symbols": list(stats.no_sl_penalty_symbols),
        }

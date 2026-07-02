from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from .tv_sandbox import binance_env_label, is_tv_signal

if TYPE_CHECKING:
    from .config import Settings
    from .risk import TradePlan
    from .runtime_control import RuntimeControl
    from .schemas import TradingViewSignal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveGuardRejection:
    skip_reason: str
    message: str


def is_live_binance_env(settings: Settings) -> bool:
    return binance_env_label(settings.binance_base_url) == "live"


def live_guard_applies(settings: Settings) -> bool:
    return is_live_binance_env(settings)


def _decimal_value(raw: Any) -> Decimal | None:
    if raw in {None, ""}:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def confirm_phrase_valid(settings: Settings) -> bool:
    expected = settings.live_expected_confirm_phrase.strip()
    if not expected:
        return True
    configured = settings.live_confirm_phrase.strip()
    return configured == expected


def is_one_shot_active(runtime_control: RuntimeControl | None) -> bool:
    if runtime_control is None or not runtime_control.settings.runtime_control_enabled:
        return False
    one_shot = runtime_control.status_payload().get("one_shot") or {}
    if not one_shot.get("enabled"):
        return False
    if int(one_shot.get("remaining") or 0) <= 0:
        return False
    return not one_shot.get("consumed_at")


def build_live_guard_status(
    settings: Settings,
    runtime_control: RuntimeControl | None = None,
) -> dict[str, Any]:
    env = binance_env_label(settings.binance_base_url)
    is_live = env == "live"
    phrase_ok = confirm_phrase_valid(settings)
    return {
        "binance_env": env,
        "binance_base_url": settings.binance_base_url,
        "is_live": is_live,
        "guard_active": live_guard_applies(settings),
        "live_canary_mode": bool(settings.live_canary_mode),
        "live_trading_enabled": bool(settings.live_trading_enabled),
        "live_confirm_phrase_configured": bool(settings.live_confirm_phrase.strip()),
        "live_confirm_phrase_valid": phrase_ok,
        "live_allowed_symbols": sorted(settings.live_allowed_symbol_set),
        "live_max_risk_usdt": settings.live_max_risk_usdt,
        "live_max_margin_usdt": settings.live_max_margin_usdt,
        "live_max_position_notional_usdt": settings.live_max_position_notional_usdt,
        "live_require_one_shot": bool(settings.live_require_one_shot),
        "live_reject_tradingview_by_default": bool(settings.live_reject_tradingview_by_default),
        "live_force_runtime_locked_on_startup": bool(settings.live_force_runtime_locked_on_startup),
        "one_shot_active": is_one_shot_active(runtime_control),
        "would_allow_execution": _would_allow_execution_summary(settings, runtime_control),
    }


def _would_allow_execution_summary(
    settings: Settings,
    runtime_control: RuntimeControl | None,
) -> dict[str, Any]:
    if not live_guard_applies(settings):
        return {"applies": False, "allowed": True, "blocking_reasons": []}
    reasons: list[str] = []
    if not settings.live_trading_enabled:
        reasons.append("live_guard_trading_disabled")
    if not confirm_phrase_valid(settings):
        reasons.append("live_guard_confirm_phrase_invalid")
    if settings.live_require_one_shot and not is_one_shot_active(runtime_control):
        reasons.append("live_guard_one_shot_required")
    return {
        "applies": True,
        "allowed": len(reasons) == 0,
        "blocking_reasons": reasons,
    }


def validate_live_guard_before_plan(
    settings: Settings,
    signal: TradingViewSignal,
    raw_payload: dict[str, Any],
    *,
    runtime_control: RuntimeControl | None = None,
) -> LiveGuardRejection | None:
    if not live_guard_applies(settings):
        return None

    if not settings.live_trading_enabled:
        return LiveGuardRejection(
            skip_reason="live_guard_trading_disabled",
            message=(
                "Live guard 拒绝：LIVE_TRADING_ENABLED=false，"
                "Binance 实盘 endpoint 不允许执行交易"
            ),
        )

    if not confirm_phrase_valid(settings):
        if not settings.live_confirm_phrase.strip():
            return LiveGuardRejection(
                skip_reason="live_guard_confirm_phrase_invalid",
                message=(
                    "Live guard 拒绝：未配置 LIVE_CONFIRM_PHRASE，"
                    f"必须等于 {settings.live_expected_confirm_phrase!r}"
                ),
            )
        return LiveGuardRejection(
            skip_reason="live_guard_confirm_phrase_invalid",
            message=(
                "Live guard 拒绝：LIVE_CONFIRM_PHRASE 与 "
                "LIVE_EXPECTED_CONFIRM_PHRASE 不匹配"
            ),
        )

    symbol = str(signal.symbol or raw_payload.get("symbol") or "").strip().upper()
    if symbol and symbol not in settings.live_allowed_symbol_set:
        return LiveGuardRejection(
            skip_reason="live_guard_symbol_not_allowed",
            message=(
                f"Live guard 拒绝：symbol={symbol} 不在 LIVE_ALLOWED_SYMBOLS="
                f"{sorted(settings.live_allowed_symbol_set)}"
            ),
        )

    risk_usdt = _decimal_value(
        signal.risk_usdt if signal.risk_usdt is not None else raw_payload.get("risk_usdt")
    )
    max_risk = Decimal(str(settings.live_max_risk_usdt))
    if risk_usdt is not None and risk_usdt > max_risk:
        return LiveGuardRejection(
            skip_reason="live_guard_risk_too_large",
            message=(
                f"Live guard 拒绝：risk_usdt={risk_usdt} 超过上限 "
                f"LIVE_MAX_RISK_USDT={max_risk}"
            ),
        )

    margin_usdt = _decimal_value(
        signal.margin_usdt if signal.margin_usdt is not None else raw_payload.get("margin_usdt")
    )
    max_margin = Decimal(str(settings.live_max_margin_usdt))
    if margin_usdt is not None and margin_usdt > max_margin:
        return LiveGuardRejection(
            skip_reason="live_guard_margin_too_large",
            message=(
                f"Live guard 拒绝：margin_usdt={margin_usdt} 超过上限 "
                f"LIVE_MAX_MARGIN_USDT={max_margin}"
            ),
        )

    if settings.live_reject_tradingview_by_default:
        source = str(raw_payload.get("source") or signal.source or "").strip().lower()
        if source == "tradingview" or is_tv_signal(raw_payload, settings):
            return LiveGuardRejection(
                skip_reason="live_guard_tradingview_rejected",
                message=(
                    "Live guard 拒绝：LIVE_REJECT_TRADINGVIEW_BY_DEFAULT=true，"
                    "实盘默认拒绝 TradingView 来源信号"
                ),
            )

    if settings.live_require_one_shot and not is_one_shot_active(runtime_control):
        return LiveGuardRejection(
            skip_reason="live_guard_one_shot_required",
            message=(
                "Live guard 拒绝：LIVE_REQUIRE_ONE_SHOT=true，"
                "实盘执行前必须启用 Runtime one-shot 放行"
            ),
        )

    return None


def validate_live_guard_after_plan(
    settings: Settings,
    signal: TradingViewSignal,
    plan: TradePlan,
) -> LiveGuardRejection | None:
    if not live_guard_applies(settings):
        return None

    max_notional = Decimal(str(settings.live_max_position_notional_usdt))
    if plan.notional_usdt > max_notional:
        return LiveGuardRejection(
            skip_reason="live_guard_notional_too_large",
            message=(
                f"Live guard 拒绝：预估名义价值 notional={plan.notional_usdt} 超过上限 "
                f"LIVE_MAX_POSITION_NOTIONAL_USDT={max_notional}"
            ),
        )

    max_margin = Decimal(str(settings.live_max_margin_usdt))
    margin_usdt = plan.margin_usdt
    if margin_usdt is not None and margin_usdt > max_margin:
        return LiveGuardRejection(
            skip_reason="live_guard_margin_too_large",
            message=(
                f"Live guard 拒绝：计划 margin_usdt={margin_usdt} 超过上限 "
                f"LIVE_MAX_MARGIN_USDT={max_margin}"
            ),
        )

    if signal.margin_usdt is None and signal.risk_mode in {"fixed_usdt", "fixed_pct"}:
        risk_usdt = _decimal_value(plan.target_risk_usdt)
        max_risk = Decimal(str(settings.live_max_risk_usdt))
        if risk_usdt is not None and risk_usdt > max_risk:
            return LiveGuardRejection(
                skip_reason="live_guard_risk_too_large",
                message=(
                    f"Live guard 拒绝：计划 target_risk_usdt={risk_usdt} 超过上限 "
                    f"LIVE_MAX_RISK_USDT={max_risk}"
                ),
            )

    return None


def build_live_guard_skip_result(rejection: LiveGuardRejection) -> dict[str, Any]:
    return {
        "orders": {},
        "skipped": True,
        "skip_reason": rejection.skip_reason,
        "live_guard": {
            "rejected": True,
            "message": rejection.message,
        },
    }


def ensure_live_startup_runtime_lock(
    settings: Settings,
    runtime_control: RuntimeControl | None,
) -> None:
    if not is_live_binance_env(settings):
        return
    if not settings.live_force_runtime_locked_on_startup:
        return
    if runtime_control is None:
        logger.warning(
            "Live startup lock skipped: runtime_control unavailable "
            "(LIVE_FORCE_RUNTIME_LOCKED_ON_STARTUP=true)"
        )
        return
    if not settings.runtime_control_enabled:
        logger.warning(
            "Live startup lock skipped: RUNTIME_CONTROL_ENABLED=false "
            "(LIVE_FORCE_RUNTIME_LOCKED_ON_STARTUP=true)"
        )
        return
    runtime_control.lock(
        reason="live canary startup safety lock",
        locked_until=None,
        operator="system",
    )
    logger.info(
        "Live canary startup lock applied: binance_env=live force_locked=true"
    )

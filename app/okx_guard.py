from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .exchanges.okx_symbols import symbol_to_inst_id
from .live_guard import is_one_shot_active

if TYPE_CHECKING:
    from .config import Settings
    from .runtime_control import RuntimeControl
    from .schemas import TradingViewSignal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OkxGuardRejection:
    skip_reason: str
    message: str


def okx_guard_applies(settings: Settings) -> bool:
    return settings.exchange.strip().lower() == "okx"


def okx_confirm_phrase_valid(settings: Settings) -> bool:
    expected = settings.okx_expected_confirm_phrase.strip()
    if not expected:
        return True
    configured = settings.okx_confirm_phrase.strip()
    return configured == expected


def resolve_okx_inst_id(
    settings: Settings,
    signal: TradingViewSignal,
    raw_payload: dict[str, Any],
) -> str | None:
    symbol = str(signal.symbol or raw_payload.get("symbol") or "").strip()
    if not symbol:
        return None
    try:
        inst_id = symbol_to_inst_id(symbol)
    except ValueError:
        return None
    return inst_id.upper()


def build_okx_guard_status(
    settings: Settings,
    runtime_control: RuntimeControl | None = None,
) -> dict[str, Any]:
    phrase_ok = okx_confirm_phrase_valid(settings)
    return {
        "exchange": "okx",
        "guard_active": okx_guard_applies(settings),
        "readonly_mode": bool(settings.okx_readonly_mode),
        "okx_live_trading_enabled": bool(settings.okx_live_trading_enabled),
        "okx_confirm_phrase_configured": bool(settings.okx_confirm_phrase.strip()),
        "okx_confirm_phrase_valid": phrase_ok,
        "okx_allowed_inst_ids": sorted(settings.okx_allowed_inst_id_set),
        "okx_require_one_shot": bool(settings.okx_require_one_shot),
        "okx_canary_mode": bool(settings.okx_canary_mode),
        "okx_simulated_trading": bool(settings.okx_simulated_trading),
        "one_shot_active": is_one_shot_active(runtime_control),
        "would_allow_execution": _would_allow_execution_summary(settings, runtime_control),
    }


def _would_allow_execution_summary(
    settings: Settings,
    runtime_control: RuntimeControl | None,
) -> dict[str, Any]:
    if not okx_guard_applies(settings):
        return {"applies": False, "allowed": True, "blocking_reasons": []}
    reasons = evaluate_okx_guard_blocking_reasons(settings, runtime_control)
    return {
        "applies": True,
        "allowed": len(reasons) == 0,
        "blocking_reasons": reasons,
    }


def evaluate_okx_guard_blocking_reasons(
    settings: Settings,
    runtime_control: RuntimeControl | None,
    *,
    signal: TradingViewSignal | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> list[str]:
    if not okx_guard_applies(settings):
        return []
    reasons: list[str] = []
    if settings.okx_readonly_mode:
        reasons.append("okx_readonly_mode")
    if not settings.okx_live_trading_enabled:
        reasons.append("okx_live_trading_disabled")
    if not okx_confirm_phrase_valid(settings):
        reasons.append("okx_confirm_phrase_invalid")
    if settings.okx_require_one_shot and not is_one_shot_active(runtime_control):
        reasons.append("okx_one_shot_required")
    if signal is not None:
        inst_id = resolve_okx_inst_id(settings, signal, raw_payload or {})
        if inst_id is None or inst_id not in settings.okx_allowed_inst_id_set:
            reasons.append("okx_symbol_not_allowed")
    return reasons


def validate_okx_guard_before_plan(
    settings: Settings,
    signal: TradingViewSignal,
    raw_payload: dict[str, Any],
    *,
    runtime_control: RuntimeControl | None = None,
) -> OkxGuardRejection | None:
    if not okx_guard_applies(settings):
        return None

    if settings.okx_readonly_mode:
        return OkxGuardRejection(
            skip_reason="okx_readonly_mode",
            message=(
                "OKX guard 拒绝：OKX_READONLY_MODE=true，"
                "v6.5.2 仅支持 read-only preflight，不允许执行交易"
            ),
        )

    if not settings.okx_live_trading_enabled:
        return OkxGuardRejection(
            skip_reason="okx_live_trading_disabled",
            message=(
                "OKX guard 拒绝：OKX_LIVE_TRADING_ENABLED=false，"
                "不允许执行 OKX 交易"
            ),
        )

    if not okx_confirm_phrase_valid(settings):
        if not settings.okx_confirm_phrase.strip():
            return OkxGuardRejection(
                skip_reason="okx_confirm_phrase_invalid",
                message=(
                    "OKX guard 拒绝：未配置 OKX_CONFIRM_PHRASE，"
                    f"必须等于 {settings.okx_expected_confirm_phrase!r}"
                ),
            )
        return OkxGuardRejection(
            skip_reason="okx_confirm_phrase_invalid",
            message=(
                "OKX guard 拒绝：OKX_CONFIRM_PHRASE 与 "
                "OKX_EXPECTED_CONFIRM_PHRASE 不匹配"
            ),
        )

    if settings.okx_require_one_shot and not is_one_shot_active(runtime_control):
        return OkxGuardRejection(
            skip_reason="okx_one_shot_required",
            message=(
                "OKX guard 拒绝：OKX_REQUIRE_ONE_SHOT=true，"
                "执行前必须启用 Runtime one-shot 放行"
            ),
        )

    inst_id = resolve_okx_inst_id(settings, signal, raw_payload)
    if inst_id is None or inst_id not in settings.okx_allowed_inst_id_set:
        return OkxGuardRejection(
            skip_reason="okx_symbol_not_allowed",
            message=(
                f"OKX guard 拒绝：symbol={signal.symbol} instId={inst_id!r} "
                f"不在 OKX_ALLOWED_INST_IDS={sorted(settings.okx_allowed_inst_id_set)}"
            ),
        )

    return OkxGuardRejection(
        skip_reason="okx_execution_not_implemented",
        message=(
            "OKX guard 通过配置检查，但 v6.5.2 仍未实现 OKX 下单执行路径"
        ),
    )


def build_okx_guard_skip_result(rejection: OkxGuardRejection) -> dict[str, Any]:
    return {
        "orders": {},
        "skipped": True,
        "skip_reason": rejection.skip_reason,
        "exchange": "okx",
        "okx_guard": {
            "rejected": True,
            "message": rejection.message,
        },
    }

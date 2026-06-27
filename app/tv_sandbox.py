from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings
    from .schemas import TradingViewSignal


@dataclass(frozen=True)
class TvSandboxRejection:
    skip_reason: str
    message: str
    invalid_fields: list[str] | None = None


def binance_env_label(base_url: str) -> str:
    url = base_url.lower()
    if "demo-fapi" in url or "testnet" in url:
        return "demo"
    if "fapi.binance.com" in url and "demo" not in url and "testnet" not in url:
        return "live"
    return "unknown"


def is_tv_signal(raw_payload: dict[str, Any], settings: Settings) -> bool:
    source = str(raw_payload.get("source") or "").strip().lower()
    if source and source in settings.tv_signal_allowed_source_set:
        return True
    prefix = settings.tv_signal_id_prefix.strip()
    signal_id = str(raw_payload.get("signal_id") or "").strip()
    if prefix and signal_id.startswith(prefix):
        return True
    return False


def _missing_tv_fields(raw_payload: dict[str, Any], settings: Settings) -> list[str]:
    missing: list[str] = []
    checks = [
        ("secret", lambda: raw_payload.get("secret") not in {None, ""}),
        ("signal_id", lambda: str(raw_payload.get("signal_id") or "").strip() != ""),
        ("symbol", lambda: str(raw_payload.get("symbol") or "").strip() != ""),
        ("side", lambda: str(raw_payload.get("side") or "").strip() != ""),
        ("entry_type", lambda: str(raw_payload.get("entry_type") or "").strip() != ""),
        ("risk_mode", lambda: str(raw_payload.get("risk_mode") or "").strip() != ""),
        ("sl", lambda: raw_payload.get("sl") not in {None, ""}),
    ]
    if settings.tv_signal_require_source:
        checks.insert(2, ("source", lambda: str(raw_payload.get("source") or "").strip() != ""))

    for name, ok in checks:
        if not ok():
            missing.append(name)

    if raw_payload.get("risk_usdt") in {None, ""} and raw_payload.get("margin_usdt") in {None, ""}:
        missing.append("risk_usdt|margin_usdt")

    tps = raw_payload.get("tps")
    if not isinstance(tps, list) or len(tps) == 0:
        missing.append("tps")

    return missing


def validate_tv_payload(raw_payload: dict[str, Any], settings: Settings) -> TvSandboxRejection | None:
    if not settings.tv_signal_sandbox_enabled:
        return None
    if not is_tv_signal(raw_payload, settings):
        return None

    missing = _missing_tv_fields(raw_payload, settings)
    if missing:
        return TvSandboxRejection(
            skip_reason="tv_payload_invalid",
            message=f"TradingView 信号缺少必填字段: {', '.join(missing)}",
            invalid_fields=missing,
        )
    return None


def _decimal_value(raw: Any) -> Decimal | None:
    if raw in {None, ""}:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def validate_tv_policy(
    raw_payload: dict[str, Any],
    signal: TradingViewSignal,
    settings: Settings,
) -> TvSandboxRejection | None:
    if not settings.tv_signal_sandbox_enabled:
        return None
    if not is_tv_signal(raw_payload, settings):
        return None

    payload_rejection = validate_tv_payload(raw_payload, settings)
    if payload_rejection:
        return payload_rejection

    source = str(raw_payload.get("source") or "").strip().lower()
    if settings.tv_signal_require_source and not source:
        return TvSandboxRejection(
            skip_reason="tv_source_missing",
            message="TradingView 信号缺少 source 字段",
            invalid_fields=["source"],
        )
    if source and source not in settings.tv_signal_allowed_source_set:
        return TvSandboxRejection(
            skip_reason="tv_source_not_allowed",
            message=f"TradingView 信号 source 不在允许列表: {source}",
        )

    prefix = settings.tv_signal_id_prefix.strip()
    signal_id = str(signal.signal_id or raw_payload.get("signal_id") or "").strip()
    if prefix and not signal_id.startswith(prefix):
        return TvSandboxRejection(
            skip_reason="tv_signal_id_prefix_invalid",
            message=f"TradingView 信号 signal_id 必须以 {prefix} 开头",
        )

    if settings.tv_signal_reject_live_binance:
        env = binance_env_label(settings.binance_base_url)
        if env not in {"demo"}:
            return TvSandboxRejection(
                skip_reason="tv_live_binance_rejected",
                message=(
                    f"TV Sandbox 拒绝非 demo/testnet 环境 (binance_env={env}, "
                    f"url={settings.binance_base_url})"
                ),
            )

    risk_usdt = _decimal_value(signal.risk_usdt if signal.risk_usdt is not None else raw_payload.get("risk_usdt"))
    if risk_usdt is not None and risk_usdt > Decimal(str(settings.tv_signal_max_risk_usdt)):
        return TvSandboxRejection(
            skip_reason="tv_risk_too_large",
            message=(
                f"TradingView 信号 risk_usdt={risk_usdt} 超过上限 "
                f"{settings.tv_signal_max_risk_usdt}"
            ),
        )

    margin_usdt = _decimal_value(
        signal.margin_usdt if signal.margin_usdt is not None else raw_payload.get("margin_usdt")
    )
    if margin_usdt is not None and margin_usdt > Decimal(str(settings.tv_signal_max_margin_usdt)):
        return TvSandboxRejection(
            skip_reason="tv_margin_too_large",
            message=(
                f"TradingView 信号 margin_usdt={margin_usdt} 超过上限 "
                f"{settings.tv_signal_max_margin_usdt}"
            ),
        )

    entry_type = str(signal.entry_type or raw_payload.get("entry_type") or "").strip().lower()
    if entry_type and entry_type not in settings.tv_signal_allowed_entry_type_set:
        return TvSandboxRejection(
            skip_reason="tv_entry_type_not_allowed",
            message=f"TradingView 信号 entry_type={entry_type} 不在允许列表",
        )

    return None


def build_tv_skip_result(rejection: TvSandboxRejection) -> dict[str, Any]:
    return {
        "orders": {},
        "skipped": True,
        "skip_reason": rejection.skip_reason,
        "tv_sandbox": {
            "rejected": True,
            "message": rejection.message,
            "invalid_fields": rejection.invalid_fields,
        },
    }


def is_tv_execution_row(row: dict[str, Any], settings: Settings) -> bool:
    skip_reason = str(row.get("skip_reason") or "")
    status = str(row.get("status") or "")
    if status == "tv_sandbox_rejected" and skip_reason.startswith("tv_"):
        return True
    if skip_reason.startswith("tv_"):
        return True
    prefix = settings.tv_signal_id_prefix.strip()
    signal_id = str(row.get("signal_id") or "")
    if prefix and signal_id.startswith(prefix):
        return True
    raw_json = row.get("raw_signal_json")
    if raw_json:
        try:
            import json

            raw = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            if isinstance(raw, dict) and is_tv_signal(raw, settings):
                return True
        except Exception:
            pass
    return False

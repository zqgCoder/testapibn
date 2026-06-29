from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from .schemas import TakeProfitLevel, TradingViewSignal, normalize_side
from .tv_sandbox import TvSandboxRejection

if TYPE_CHECKING:
    from .binance_client import BinanceClient
    from .config import Settings

ALLOWED_POSITION_STRATEGIES = frozenset({"replace", "reject", "add"})

POSITION_STRATEGY_INVALID_MESSAGE = (
    "TradingView 信号 position_strategy 非法，只允许 replace / reject / add"
)


def position_strategy_invalid_rejection() -> TvSandboxRejection:
    return TvSandboxRejection(
        skip_reason="tv_payload_invalid",
        message=POSITION_STRATEGY_INVALID_MESSAGE,
        invalid_fields=["position_strategy"],
    )


def map_position_strategy_to_policy(strategy: str) -> str:
    value = strategy.strip().lower()
    if value in ALLOWED_POSITION_STRATEGIES:
        return value
    raise ValueError(POSITION_STRATEGY_INVALID_MESSAGE)


def apply_position_strategy(raw_payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Copy position_strategy into position_policy for downstream trader logic."""
    strategy_raw = raw_payload.get("position_strategy")
    if strategy_raw in {None, ""}:
        strategy_raw = raw_payload.get("positionStrategy")
    if strategy_raw in {None, ""}:
        return raw_payload
    value = str(strategy_raw).strip().lower()
    if value not in ALLOWED_POSITION_STRATEGIES:
        return raw_payload
    updated = dict(raw_payload)
    updated["position_policy"] = value
    return updated


def _decimal_value(raw: Any) -> Decimal | None:
    if raw in {None, ""}:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _parse_signal_timestamp(raw_payload: dict[str, Any]) -> datetime | None:
    for key in ("sent_at", "timestamp", "time"):
        raw = raw_payload.get(key)
        if raw in {None, ""}:
            continue
        if isinstance(raw, (int, float)):
            value = float(raw)
            if value > 1_000_000_000_000:
                value /= 1000.0
            return datetime.fromtimestamp(value, tz=timezone.utc)
        text = str(raw).strip()
        try:
            if text.isdigit():
                value = float(text)
                if value > 1_000_000_000_000:
                    value /= 1000.0
                return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _reference_price_from_payload(raw_payload: dict[str, Any], signal: TradingViewSignal) -> Decimal | None:
    for key in ("entry_price", "close", "price"):
        value = _decimal_value(raw_payload.get(key))
        if value is not None and value > 0:
            return value
    if signal.signal_price is not None and signal.signal_price > 0:
        return signal.signal_price
    if signal.limit_price is not None and signal.limit_price > 0:
        return signal.limit_price
    return None


def validate_position_strategy(raw_payload: dict[str, Any], settings: Settings) -> TvSandboxRejection | None:
    if not settings.tv_signal_require_position_strategy:
        return None
    strategy_raw = raw_payload.get("position_strategy")
    if strategy_raw in {None, ""}:
        strategy_raw = raw_payload.get("positionStrategy")
    if strategy_raw in {None, ""}:
        return TvSandboxRejection(
            skip_reason="tv_payload_invalid",
            message="TradingView 信号缺少必填字段 position_strategy",
            invalid_fields=["position_strategy"],
        )
    value = str(strategy_raw).strip().lower()
    if value not in ALLOWED_POSITION_STRATEGIES:
        return position_strategy_invalid_rejection()
    return None


def validate_signal_timestamp(raw_payload: dict[str, Any], settings: Settings) -> TvSandboxRejection | None:
    sent_at = _parse_signal_timestamp(raw_payload)
    if sent_at is None:
        if settings.tv_signal_require_timestamp:
            return TvSandboxRejection(
                skip_reason="tv_payload_invalid",
                message="TradingView 信号缺少有效 timestamp/sent_at/time 字段",
                invalid_fields=["sent_at|timestamp|time"],
            )
        return None

    if not settings.tv_signal_reject_expired:
        return None

    age_seconds = (datetime.now(timezone.utc) - sent_at).total_seconds()
    max_age = int(settings.tv_signal_max_age_seconds)
    if age_seconds > max_age:
        return TvSandboxRejection(
            skip_reason="signal_expired",
            message=(
                f"TradingView 信号已过期: age={int(age_seconds)}s > max_age={max_age}s, "
                f"sent_at={sent_at.isoformat()}"
            ),
            invalid_fields=["sent_at|timestamp|time"],
        )
    if age_seconds < -60:
        return TvSandboxRejection(
            skip_reason="tv_payload_invalid",
            message=f"TradingView 信号时间戳在未来: sent_at={sent_at.isoformat()}",
            invalid_fields=["sent_at|timestamp|time"],
        )
    return None


def validate_tps_qty_pct(raw_payload: dict[str, Any], settings: Settings) -> TvSandboxRejection | None:
    tps = raw_payload.get("tps")
    if not isinstance(tps, list) or len(tps) == 0:
        return TvSandboxRejection(
            skip_reason="tv_payload_invalid",
            message="TradingView 信号 tps 必须是非空数组",
            invalid_fields=["tps"],
        )

    max_tp = int(settings.tv_signal_max_tp_count)
    if len(tps) > max_tp:
        return TvSandboxRejection(
            skip_reason="tv_payload_invalid",
            message=f"TradingView 信号 tps 数量超过上限 MAX_TP_COUNT={max_tp}",
            invalid_fields=["tps"],
        )

    total = Decimal("0")
    invalid_fields: list[str] = []
    for idx, item in enumerate(tps):
        if not isinstance(item, dict):
            invalid_fields.append(f"tps[{idx}]")
            continue
        price = _decimal_value(item.get("price"))
        qty_pct = _decimal_value(item.get("qty_pct"))
        if price is None or price <= 0:
            invalid_fields.append(f"tps[{idx}].price")
        if qty_pct is None or qty_pct <= 0 or qty_pct > 1:
            invalid_fields.append(f"tps[{idx}].qty_pct")
        if qty_pct is not None:
            total += qty_pct

    if invalid_fields:
        return TvSandboxRejection(
            skip_reason="tv_payload_invalid",
            message="TradingView 信号 tps 字段无效: price 必须 > 0，qty_pct 必须在 (0, 1]",
            invalid_fields=invalid_fields,
        )

    min_sum = Decimal(str(settings.tv_signal_tp_qty_sum_min))
    max_sum = Decimal(str(settings.tv_signal_tp_qty_sum_max))
    if total < min_sum or total > max_sum:
        return TvSandboxRejection(
            skip_reason="tv_payload_invalid",
            message=(
                f"TradingView 信号 tps.qty_pct 合计不合理: sum={total}, "
                f"允许范围 [{min_sum}, {max_sum}]"
            ),
            invalid_fields=["tps.qty_pct"],
        )
    return None


def validate_sl_tp_levels(
    raw_payload: dict[str, Any],
    signal: TradingViewSignal,
    settings: Settings,
    client: BinanceClient | None,
) -> TvSandboxRejection | None:
    sl = signal.sl
    if sl is None:
        sl = _decimal_value(raw_payload.get("sl"))
    if sl is None or sl <= 0:
        return TvSandboxRejection(
            skip_reason="tv_payload_invalid",
            message="TradingView 信号缺少有效 sl",
            invalid_fields=["sl"],
        )

    ref_price = _reference_price_from_payload(raw_payload, signal)
    if ref_price is None:
        if client is None:
            return TvSandboxRejection(
                skip_reason="tv_payload_invalid",
                message="无法确定参考价格，且 Binance 客户端不可用",
                invalid_fields=["entry_price|close|price|signal_price"],
            )
        try:
            ref_price = client.ticker_price(signal.symbol)
        except Exception as exc:
            return TvSandboxRejection(
                skip_reason="tv_payload_invalid",
                message=f"无法获取 {signal.symbol} 参考价格: {str(exc)[:200]}",
                invalid_fields=["entry_price|close|price|signal_price"],
            )

    try:
        side = normalize_side(signal.side)
    except ValueError as exc:
        return TvSandboxRejection(
            skip_reason="tv_payload_invalid",
            message=str(exc),
            invalid_fields=["side"],
        )

    invalid_fields: list[str] = []
    if side == "BUY":
        if sl >= ref_price:
            invalid_fields.append("sl")
        for idx, tp in enumerate(signal.tps):
            if tp.price <= ref_price:
                invalid_fields.append(f"tps[{idx}].price")
    else:
        if sl <= ref_price:
            invalid_fields.append("sl")
        for idx, tp in enumerate(signal.tps):
            if tp.price >= ref_price:
                invalid_fields.append(f"tps[{idx}].price")

    if invalid_fields:
        return TvSandboxRejection(
            skip_reason="tv_payload_invalid",
            message=(
                f"SL/TP 方向与 side 不匹配: side={side}, ref_price={ref_price}, "
                f"sl={sl}, 请检查 sl/tps 相对参考价方向"
            ),
            invalid_fields=invalid_fields,
        )

    min_stop_pct = Decimal(str(settings.tv_signal_min_stop_pct)) / Decimal("100")
    min_tp_pct = Decimal(str(settings.tv_signal_min_tp_pct)) / Decimal("100")
    sl_distance = abs(ref_price - sl) / ref_price
    if sl_distance < min_stop_pct:
        return TvSandboxRejection(
            skip_reason="tv_payload_invalid",
            message=(
                f"止损距离过近: distance={sl_distance:.6f}, "
                f"min_stop_pct={settings.tv_signal_min_stop_pct}%"
            ),
            invalid_fields=["sl"],
        )

    for idx, tp in enumerate(signal.tps):
        tp_distance = abs(ref_price - tp.price) / ref_price
        if tp_distance < min_tp_pct:
            return TvSandboxRejection(
                skip_reason="tv_payload_invalid",
                message=(
                    f"止盈距离过近: tps[{idx}] distance={tp_distance:.6f}, "
                    f"min_tp_pct={settings.tv_signal_min_tp_pct}%"
                ),
                invalid_fields=[f"tps[{idx}].price"],
            )

    return None


def validate_tv_production(
    raw_payload: dict[str, Any],
    signal: TradingViewSignal | None,
    settings: Settings,
    client: BinanceClient | None = None,
) -> TvSandboxRejection | None:
    for validator in (
        lambda: validate_position_strategy(raw_payload, settings),
        lambda: validate_signal_timestamp(raw_payload, settings),
        lambda: validate_tps_qty_pct(raw_payload, settings),
    ):
        rejection = validator()
        if rejection is not None:
            return rejection
    if signal is not None:
        return validate_sl_tp_levels(raw_payload, signal, settings, client)
    return None


def tv_rejection_from_pydantic(exc: Exception) -> TvSandboxRejection:
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        invalid_fields: list[str] = []
        messages: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", []) if str(x) != "body")
            if loc:
                invalid_fields.append(loc)
            msg = str(err.get("msg") or "")
            if msg:
                messages.append(f"{loc or 'payload'}: {msg}")
        return TvSandboxRejection(
            skip_reason="tv_payload_invalid",
            message="TradingView 信号字段校验失败: " + ("; ".join(messages) if messages else str(exc)),
            invalid_fields=invalid_fields or None,
        )
    return TvSandboxRejection(
        skip_reason="tv_payload_invalid",
        message=f"TradingView 信号校验失败: {str(exc)[:500]}",
    )

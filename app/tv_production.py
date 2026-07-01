from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from .schemas import TakeProfitLevel, TradingViewSignal, coerce_decimal, coerce_take_profit_levels, get_tp_price, normalize_side
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


def _close_reference_price(raw_payload: dict[str, Any]) -> Decimal | None:
    value = _decimal_value(raw_payload.get("close"))
    if value is not None and value > 0:
        return value
    return None


def _distance_pct(ref_price: Decimal, price: Decimal) -> Decimal:
    return abs(ref_price - price) / ref_price


def _pct_display(distance_ratio: Decimal) -> float:
    return round(float(distance_ratio * Decimal("100")), 6)


def _price_display(value: Decimal) -> float:
    return float(format(value, "f"))


def _needs_distance_normalize(distance: Decimal, min_pct: Decimal, epsilon: Decimal) -> bool:
    return distance <= min_pct + epsilon


def normalize_tv_guard_prices(
    raw_payload: dict[str, Any],
    settings: Settings,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Auto-push SL/TP to min distance + buffer when direction is correct but too close."""
    meta: dict[str, Any] = {
        "enabled": bool(settings.tv_signal_auto_normalize_guard_prices),
        "adjusted": False,
    }
    if not settings.tv_signal_auto_normalize_guard_prices:
        return raw_payload, meta

    ref_price = _close_reference_price(raw_payload)
    if ref_price is None:
        return raw_payload, meta

    side_raw = raw_payload.get("side")
    if side_raw in {None, ""}:
        return raw_payload, meta
    try:
        side = normalize_side(str(side_raw))
    except ValueError:
        return raw_payload, meta

    min_stop_pct = Decimal(str(settings.tv_signal_min_stop_pct)) / Decimal("100")
    min_tp_pct = Decimal(str(settings.tv_signal_min_tp_pct)) / Decimal("100")
    buffer_pct = Decimal(str(settings.tv_signal_guard_distance_buffer_pct)) / Decimal("100")
    epsilon = Decimal(str(settings.tv_signal_distance_epsilon))
    target_stop_pct = min_stop_pct + buffer_pct
    target_tp_pct = min_tp_pct + buffer_pct

    meta.update(
        {
            "ref_price": _price_display(ref_price),
            "min_stop_pct": float(settings.tv_signal_min_stop_pct),
            "min_tp_pct": float(settings.tv_signal_min_tp_pct),
            "buffer_pct": float(settings.tv_signal_guard_distance_buffer_pct),
        }
    )

    updated = dict(raw_payload)
    adjustments: list[dict[str, Any]] = []

    sl = _decimal_value(raw_payload.get("sl"))
    if sl is not None and sl > 0:
        if side == "BUY":
            if sl < ref_price:
                old_distance = (ref_price - sl) / ref_price
                if _needs_distance_normalize(old_distance, min_stop_pct, epsilon):
                    new_sl = ref_price * (Decimal("1") - target_stop_pct)
                    updated["sl"] = _price_display(new_sl)
                    adjustments.append(
                        {
                            "field": "sl",
                            "old": _price_display(sl),
                            "new": _price_display(new_sl),
                            "old_distance_pct": _pct_display(old_distance),
                            "new_distance_pct": _pct_display(target_stop_pct),
                        }
                    )
        elif sl > ref_price:
            old_distance = (sl - ref_price) / ref_price
            if _needs_distance_normalize(old_distance, min_stop_pct, epsilon):
                new_sl = ref_price * (Decimal("1") + target_stop_pct)
                updated["sl"] = _price_display(new_sl)
                adjustments.append(
                    {
                        "field": "sl",
                        "old": _price_display(sl),
                        "new": _price_display(new_sl),
                        "old_distance_pct": _pct_display(old_distance),
                        "new_distance_pct": _pct_display(target_stop_pct),
                    }
                )

    tps_raw = raw_payload.get("tps")
    if isinstance(tps_raw, list):
        new_tps: list[Any] = []
        for idx, item in enumerate(tps_raw):
            if not isinstance(item, dict):
                new_tps.append(item)
                continue
            item_copy = dict(item)
            tp_price = _decimal_value(item.get("price"))
            if tp_price is not None and tp_price > 0:
                if side == "BUY" and tp_price > ref_price:
                    old_distance = (tp_price - ref_price) / ref_price
                    if _needs_distance_normalize(old_distance, min_tp_pct, epsilon):
                        new_tp = ref_price * (Decimal("1") + target_tp_pct)
                        item_copy["price"] = _price_display(new_tp)
                        adjustments.append(
                            {
                                "field": f"tps[{idx}].price",
                                "old": _price_display(tp_price),
                                "new": _price_display(new_tp),
                                "old_distance_pct": _pct_display(old_distance),
                                "new_distance_pct": _pct_display(target_tp_pct),
                            }
                        )
                elif side == "SELL" and tp_price < ref_price:
                    old_distance = (ref_price - tp_price) / ref_price
                    if _needs_distance_normalize(old_distance, min_tp_pct, epsilon):
                        new_tp = ref_price * (Decimal("1") - target_tp_pct)
                        item_copy["price"] = _price_display(new_tp)
                        adjustments.append(
                            {
                                "field": f"tps[{idx}].price",
                                "old": _price_display(tp_price),
                                "new": _price_display(new_tp),
                                "old_distance_pct": _pct_display(old_distance),
                                "new_distance_pct": _pct_display(target_tp_pct),
                            }
                        )
            new_tps.append(item_copy)
        updated["tps"] = new_tps

    if adjustments:
        meta["adjusted"] = True
        meta["reason"] = "guard_price_distance_too_close"
        meta["adjustments"] = adjustments

    return updated, meta


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


def apply_tv_guard_price_normalization(raw_payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Normalize SL/TP in-place on raw_payload; return normalization metadata."""
    normalized, meta = normalize_tv_guard_prices(raw_payload, settings)
    if normalized.get("sl") is not None:
        raw_payload["sl"] = normalized["sl"]
    if "tps" in normalized:
        raw_payload["tps"] = normalized["tps"]
    if meta.get("enabled"):
        raw_payload["tv_price_normalization"] = meta
    return meta


def sync_signal_guard_prices(signal: TradingViewSignal, raw_payload: dict[str, Any]) -> TradingViewSignal:
    """Apply normalized sl/tps from raw_payload onto the TradingViewSignal model."""
    updates: dict[str, Any] = {}
    if raw_payload.get("sl") is not None:
        updates["sl"] = coerce_decimal(raw_payload["sl"], field="sl")
    if raw_payload.get("tps") is not None:
        updates["tps"] = coerce_take_profit_levels(raw_payload["tps"])
    if not updates:
        return signal
    return signal.model_copy(update=updates)


def _tp_prices_from_payload(raw_payload: dict[str, Any]) -> list[tuple[int, Decimal]]:
    rows: list[tuple[int, Decimal]] = []
    tps = raw_payload.get("tps")
    if not isinstance(tps, list):
        return rows
    for idx, item in enumerate(tps):
        if not isinstance(item, dict):
            continue
        price = _decimal_value(item.get("price"))
        if price is not None and price > 0:
            rows.append((idx, price))
    return rows


def validate_sl_tp_levels(
    raw_payload: dict[str, Any],
    signal: TradingViewSignal,
    settings: Settings,
    client: BinanceClient | None,
) -> TvSandboxRejection | None:
    sl = _decimal_value(raw_payload.get("sl"))
    if sl is None and signal.sl is not None:
        sl = signal.sl
    if sl is None or sl <= 0:
        return TvSandboxRejection(
            skip_reason="tv_payload_invalid",
            message="TradingView 信号缺少有效 sl",
            invalid_fields=["sl"],
        )

    ref_price = _close_reference_price(raw_payload)
    if ref_price is None:
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
    tp_levels = _tp_prices_from_payload(raw_payload)
    if not tp_levels and signal.tps:
        for idx, tp in enumerate(signal.tps):
            try:
                tp_levels.append((idx, get_tp_price(tp, index=idx)))
            except ValueError:
                invalid_fields.append(f"tps[{idx}].price")

    if side == "BUY":
        if sl >= ref_price:
            invalid_fields.append("sl")
        for idx, tp_price in tp_levels:
            if tp_price <= ref_price:
                invalid_fields.append(f"tps[{idx}].price")
    else:
        if sl <= ref_price:
            invalid_fields.append("sl")
        for idx, tp_price in tp_levels:
            if tp_price >= ref_price:
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

    apply_tv_guard_price_normalization(raw_payload, settings)

    sl = _decimal_value(raw_payload.get("sl")) or sl
    tp_levels = _tp_prices_from_payload(raw_payload)
    if not tp_levels and signal.tps:
        for idx, tp in enumerate(signal.tps):
            try:
                tp_levels.append((idx, get_tp_price(tp, index=idx)))
            except ValueError:
                invalid_fields.append(f"tps[{idx}].price")

    min_stop_pct = Decimal(str(settings.tv_signal_min_stop_pct)) / Decimal("100")
    min_tp_pct = Decimal(str(settings.tv_signal_min_tp_pct)) / Decimal("100")
    epsilon = Decimal(str(settings.tv_signal_distance_epsilon))
    sl_distance = abs(ref_price - sl) / ref_price
    if sl_distance <= min_stop_pct + epsilon:
        return TvSandboxRejection(
            skip_reason="tv_payload_invalid",
            message=(
                f"止损距离过近: distance={sl_distance:.6f}, "
                f"min_stop_pct={settings.tv_signal_min_stop_pct}%"
            ),
            invalid_fields=["sl"],
        )

    for idx, tp_price in tp_levels:
        tp_distance = abs(ref_price - tp_price) / ref_price
        if tp_distance <= min_tp_pct + epsilon:
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

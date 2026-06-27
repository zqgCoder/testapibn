from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

from .schemas import TradingViewSignal

logger = logging.getLogger(__name__)


class ExecutionStatus:
    BLOCKED_BY_ACCOUNT_RISK = "blocked_by_account_risk"
    SKIPPED_BY_POSITION_POLICY = "skipped_by_position_policy"
    ENTRY_NOT_FILLED = "entry_not_filled"
    PROTECTED = "protected"
    PROTECTION_FAILED = "protection_failed"
    FAILED = "failed"


ACCOUNT_RISK_SKIP_REASONS = {
    "daily_max_loss_exceeded",
    "daily_max_trades_exceeded",
    "max_open_positions_exceeded",
    "symbol_cooldown_active",
    "max_total_risk_exceeded",
}

STATUS_LABELS_ZH = {
    ExecutionStatus.BLOCKED_BY_ACCOUNT_RISK: "账户风控拒绝",
    ExecutionStatus.SKIPPED_BY_POSITION_POLICY: "持仓策略跳过",
    ExecutionStatus.ENTRY_NOT_FILLED: "未成交",
    ExecutionStatus.PROTECTED: "已开仓并挂保护单",
    ExecutionStatus.PROTECTION_FAILED: "保护单失败或不完整",
    ExecutionStatus.FAILED: "执行异常",
}


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str, ensure_ascii=False)


def _filled_qty_from_result(result: dict) -> Decimal:
    entry_summary = result.get("entry_summary") or {}
    raw = entry_summary.get("filled_qty")
    if raw in {None, ""}:
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal("0")


def resolve_execution_status(result: dict) -> str:
    skip_reason = result.get("skip_reason")
    if skip_reason in ACCOUNT_RISK_SKIP_REASONS:
        return ExecutionStatus.BLOCKED_BY_ACCOUNT_RISK
    if skip_reason == "same_side_position_exists":
        return ExecutionStatus.SKIPPED_BY_POSITION_POLICY

    filled_qty = _filled_qty_from_result(result)
    if filled_qty <= 0:
        return ExecutionStatus.ENTRY_NOT_FILLED

    protection = result.get("protection_summary") or {}
    plan = result.get("effective_plan") or result.get("plan") or {}
    stop_loss_price = plan.get("stop_loss_price")
    take_profits = plan.get("take_profits") or []

    sl_required = stop_loss_price is not None
    tp_required = len(take_profits) > 0
    sl_ok = not sl_required or bool(protection.get("stop_loss_submitted"))
    tp_ok = not tp_required or int(protection.get("take_profit_submitted_count") or 0) >= len(take_profits)
    position_ok = bool(protection.get("position_confirmed"))

    if protection.get("protection_skipped_reason") or result.get("protection_skipped_reason"):
        return ExecutionStatus.PROTECTION_FAILED
    if not position_ok:
        return ExecutionStatus.PROTECTION_FAILED
    if sl_ok and tp_ok:
        return ExecutionStatus.PROTECTED
    return ExecutionStatus.PROTECTION_FAILED


def _decimal_str(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    try:
        return format(Decimal(str(value)), "f")
    except Exception:
        return str(value)


def _order_row_from_response(
    *,
    execution_id: int,
    signal_key: str,
    symbol: str,
    role: str,
    payload: Any,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("skipped") or payload.get("error"):
        return {
            "execution_id": execution_id,
            "signal_key": signal_key,
            "symbol": symbol,
            "role": role,
            "order_id": None,
            "algo_id": None,
            "client_order_id": None,
            "side": None,
            "order_type": None,
            "status": str(payload.get("status") or payload.get("reason") or "skipped"),
            "price": None,
            "avg_price": None,
            "quantity": None,
            "executed_qty": None,
            "trigger_price": None,
            "reduce_only": None,
            "close_position": None,
            "raw_order_json": _json_dumps(payload),
        }

    order_id = payload.get("orderId") or payload.get("order_id")
    algo_id = payload.get("algoId") or payload.get("algo_id")
    reduce_only_raw = payload.get("reduceOnly", payload.get("reduce_only"))
    close_position_raw = payload.get("closePosition", payload.get("close_position"))
    reduce_only = None
    close_position = None
    if reduce_only_raw is not None:
        reduce_only = 1 if str(reduce_only_raw).lower() in {"true", "1"} else 0
    if close_position_raw is not None:
        close_position = 1 if str(close_position_raw).lower() in {"true", "1"} else 0

    return {
        "execution_id": execution_id,
        "signal_key": signal_key,
        "symbol": symbol,
        "role": role,
        "order_id": str(order_id) if order_id is not None else None,
        "algo_id": str(algo_id) if algo_id is not None else None,
        "client_order_id": payload.get("clientOrderId") or payload.get("newClientOrderId"),
        "side": payload.get("side"),
        "order_type": payload.get("type") or payload.get("orderType") or payload.get("origType"),
        "status": payload.get("status") or payload.get("algoStatus"),
        "price": _decimal_str(payload.get("price")),
        "avg_price": _decimal_str(payload.get("avgPrice")),
        "quantity": _decimal_str(payload.get("origQty") or payload.get("quantity")),
        "executed_qty": _decimal_str(payload.get("executedQty") or payload.get("cumQty")),
        "trigger_price": _decimal_str(payload.get("triggerPrice") or payload.get("stopPrice")),
        "reduce_only": reduce_only,
        "close_position": close_position,
        "raw_order_json": _json_dumps(payload),
    }


def extract_orders_from_result(execution_id: int, signal_key: str, symbol: str, result: dict) -> list[dict[str, Any]]:
    orders_block = result.get("orders") or {}
    rows: list[dict[str, Any]] = []

    single_role_map = {
        "close_existing_position": "close_existing",
        "open": "entry_market",
        "open_limit": "entry_limit",
        "open_limit_final": "entry_limit_final",
        "open_limit_cancel": "entry_limit_cancel",
        "open_limit_after_cancel_check": "entry_limit_after_cancel_check",
        "open_market_fallback": "entry_fallback",
        "open_market_fallback_remaining": "entry_fallback_remaining",
        "leverage": "set_leverage",
        "fallback_leverage": "set_leverage_fallback",
        "stop_loss": "stop_loss",
        "emergency_close": "emergency_close",
    }
    for key, role in single_role_map.items():
        payload = orders_block.get(key)
        if payload is None:
            continue
        row = _order_row_from_response(
            execution_id=execution_id,
            signal_key=signal_key,
            symbol=symbol,
            role=role,
            payload=payload,
        )
        if row:
            rows.append(row)

    take_profits = orders_block.get("take_profits")
    if isinstance(take_profits, list):
        for idx, payload in enumerate(take_profits, start=1):
            row = _order_row_from_response(
                execution_id=execution_id,
                signal_key=signal_key,
                symbol=symbol,
                role=f"take_profit_{idx}",
                payload=payload,
            )
            if row:
                rows.append(row)

    return rows


class TradeJournal:
    def __init__(self, store) -> None:
        self.store = store

    def persist_execution(self, signal: TradingViewSignal, signal_key: str, raw_payload: dict, result: dict) -> int | None:
        if result.get("dry_run"):
            return None
        try:
            plan = result.get("effective_plan") or result.get("plan") or {}
            entry_summary = result.get("entry_summary") or {}
            protection_summary = result.get("protection_summary") or {}
            account_risk_summary = result.get("account_risk_summary") or {}

            status = resolve_execution_status(result)
            execution_id = self.store.insert_execution(
                {
                    "signal_key": signal_key,
                    "signal_id": signal.signal_id,
                    "symbol": plan.get("symbol") or signal.symbol,
                    "side": plan.get("side") or signal.side,
                    "entry_type": plan.get("entry_type") or signal.entry_type,
                    "risk_mode": plan.get("risk_mode") or signal.risk_mode,
                    "position_policy": result.get("position_policy") or signal.position_policy,
                    "status": status,
                    "skip_reason": result.get("skip_reason"),
                    "error_message": None,
                    "planned_qty": _decimal_str(plan.get("quantity")),
                    "filled_qty": entry_summary.get("filled_qty") or _decimal_str(_filled_qty_from_result(result)),
                    "entry_price": entry_summary.get("latest_price_used") or _decimal_str(plan.get("entry_ref_price")),
                    "stop_loss_price": _decimal_str(plan.get("stop_loss_price")),
                    "target_risk_usdt": _decimal_str(plan.get("target_risk_usdt")),
                    "estimated_total_loss_at_sl": _decimal_str(plan.get("estimated_total_loss_at_sl")),
                    "leverage": plan.get("leverage"),
                    "account_risk_allowed": 1 if account_risk_summary.get("allowed", True) else 0,
                    "account_risk_skip_reason": account_risk_summary.get("skip_reason"),
                    "raw_signal_json": _json_dumps(raw_payload),
                    "plan_json": _json_dumps(plan),
                    "account_risk_json": _json_dumps(account_risk_summary) if account_risk_summary else None,
                    "entry_summary_json": _json_dumps(entry_summary) if entry_summary else None,
                    "protection_summary_json": _json_dumps(protection_summary) if protection_summary else None,
                    "result_json": _json_dumps(result),
                }
            )
            symbol = str(plan.get("symbol") or signal.symbol).upper()
            for order_row in extract_orders_from_result(execution_id, signal_key, symbol, result):
                self.store.insert_order(order_row)
            return execution_id
        except Exception as exc:
            logger.warning("Failed to persist trade journal execution: signal_key=%s error=%s", signal_key, exc)
            return None

    def persist_failure(
        self,
        signal: TradingViewSignal,
        signal_key: str,
        raw_payload: dict,
        exc: Exception,
        result: dict | None = None,
    ) -> int | None:
        try:
            plan = (result or {}).get("plan") or {}
            execution_id = self.store.insert_execution(
                {
                    "signal_key": signal_key,
                    "signal_id": signal.signal_id,
                    "symbol": plan.get("symbol") or signal.symbol,
                    "side": plan.get("side") or signal.side,
                    "entry_type": plan.get("entry_type") or signal.entry_type,
                    "risk_mode": plan.get("risk_mode") or signal.risk_mode,
                    "position_policy": (result or {}).get("position_policy") or signal.position_policy,
                    "status": ExecutionStatus.FAILED,
                    "skip_reason": (result or {}).get("skip_reason"),
                    "error_message": str(exc)[:2000],
                    "planned_qty": _decimal_str(plan.get("quantity")),
                    "filled_qty": None,
                    "entry_price": _decimal_str(plan.get("entry_ref_price")),
                    "stop_loss_price": _decimal_str(plan.get("stop_loss_price")),
                    "target_risk_usdt": _decimal_str(plan.get("target_risk_usdt")),
                    "estimated_total_loss_at_sl": _decimal_str(plan.get("estimated_total_loss_at_sl")),
                    "leverage": plan.get("leverage"),
                    "account_risk_allowed": None,
                    "account_risk_skip_reason": None,
                    "raw_signal_json": _json_dumps(raw_payload),
                    "plan_json": _json_dumps(plan) if plan else None,
                    "account_risk_json": _json_dumps((result or {}).get("account_risk_summary")),
                    "entry_summary_json": _json_dumps((result or {}).get("entry_summary")),
                    "protection_summary_json": _json_dumps((result or {}).get("protection_summary")),
                    "result_json": _json_dumps(result) if result else None,
                }
            )
            if result:
                symbol = str(plan.get("symbol") or signal.symbol).upper()
                for order_row in extract_orders_from_result(execution_id, signal_key, symbol, result):
                    self.store.insert_order(order_row)
            return execution_id
        except Exception as persist_exc:
            logger.warning("Failed to persist failed trade journal: signal_key=%s error=%s", signal_key, persist_exc)
            return None

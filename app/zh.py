from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from .risk import TradePlan


def to_jsonable(value: Any) -> Any:
    """递归转换成 JSONResponse 可以直接返回的类型。

    Python 的 json.dumps 不能直接处理 Decimal。
    TradePlan 里除了顶层字段，还有 take_profits 这种嵌套 list/tuple，
    所以必须递归转换。
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return value


def _s(value: Any) -> Any:
    return to_jsonable(value)


def _direction(side: str) -> str:
    side = str(side).upper()
    if side == "BUY":
        return "开多 / 买入"
    if side == "SELL":
        return "开空 / 卖出"
    return side


def _close_direction(side: str) -> str:
    side = str(side).upper()
    if side == "BUY":
        return "买入平仓"
    if side == "SELL":
        return "卖出平仓"
    return side


def _risk_mode(mode: str) -> str:
    mapping = {
        "manual": "手动仓位模式",
        "fixed_pct": "账户百分比风险模式",
        "fixed_usdt": "固定金额风险模式",
    }
    return mapping.get(str(mode), str(mode))


def _working_type(value: str) -> str:
    mapping = {
        "MARK_PRICE": "标记价格",
        "CONTRACT_PRICE": "合约最新价",
    }
    return mapping.get(str(value), str(value))


def trade_plan_to_chinese(plan: TradePlan) -> dict[str, Any]:
    """把内部 TradePlan 转成中文字段，方便人工阅读。"""
    return {
        "交易对": plan.symbol,
        "开仓方向": _direction(plan.side),
        "平仓方向": _close_direction(plan.close_side),
        "进场方式": "限价" if getattr(plan, "entry_type", "market") == "limit" else "市价",
        "限价进场价": _s(getattr(plan, "limit_price", None)),
        "参考开仓价": _s(plan.entry_ref_price),
        "名义仓位_USDT": _s(plan.notional_usdt),
        "保证金预算_USDT": _s(plan.margin_usdt),
        "下单数量": _s(plan.quantity),
        "杠杆倍数": f"{plan.leverage}x",
        "止损价": _s(plan.stop_loss_price),
        "分批止盈": [
            {
                "止盈触发价": _s(price),
                "平仓数量": _s(qty),
            }
            for price, qty in plan.take_profits
        ],
        "触发价格类型": _working_type(plan.working_type),
        "是否模拟运行": "是，不会真实下单" if plan.dry_run else "否，会真实下单",
        "风控模式": _risk_mode(plan.risk_mode),
        "账户资产": plan.account_asset,
        "账户总余额": _s(plan.account_balance),
        "账户可用余额": _s(plan.account_available_balance),
        "用于风控的余额": _s(plan.selected_balance),
        "目标最大亏损_USDT": _s(plan.target_risk_usdt),
        "预估止损价差亏损_USDT": _s(plan.estimated_price_loss_usdt),
        "预估开平仓手续费_USDT": _s(plan.estimated_fees_usdt),
        "预估触发止损总亏损_USDT": _s(plan.estimated_total_loss_at_sl),
        "使用的手续费率": _s(plan.fee_rate_used),
        "允许最大杠杆": f"{plan.max_leverage_allowed}x" if plan.max_leverage_allowed is not None else None,
    }


def trade_plan_raw(plan: TradePlan) -> dict[str, Any]:
    """保留英文原始字段，方便程序调试。"""
    return to_jsonable(asdict(plan))


def _safe_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def position_to_chinese(row: dict[str, Any]) -> dict[str, Any]:
    amt = _safe_decimal(row.get("positionAmt", "0"))
    if amt > 0:
        direction = "多单 / LONG"
    elif amt < 0:
        direction = "空单 / SHORT"
    else:
        direction = "无持仓"
    return {
        "交易对": row.get("symbol"),
        "方向": direction,
        "持仓数量": _s(row.get("positionAmt")),
        "开仓均价": _s(row.get("entryPrice")),
        "标记价格": _s(row.get("markPrice")),
        "未实现盈亏": _s(row.get("unRealizedProfit", row.get("unrealizedProfit"))),
        "杠杆倍数": f"{row.get('leverage')}x" if row.get("leverage") is not None else None,
        "保证金类型": row.get("marginType"),
        "强平价格": _s(row.get("liquidationPrice")),
        "原始字段": to_jsonable(row),
    }


def order_to_chinese(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "交易对": row.get("symbol"),
        "方向": _direction(str(row.get("side", ""))),
        "订单类型": row.get("type"),
        "订单状态": row.get("status"),
        "委托价格": _s(row.get("price")),
        "原始数量": _s(row.get("origQty")),
        "已成交数量": _s(row.get("executedQty")),
        "只减仓": row.get("reduceOnly"),
        "客户端订单ID": row.get("clientOrderId"),
        "原始字段": to_jsonable(row),
    }


def algo_order_to_chinese(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "交易对": row.get("symbol"),
        "方向": _close_direction(str(row.get("side", ""))),
        "条件单类型": row.get("orderType", row.get("type")),
        "条件单状态": row.get("algoStatus", row.get("status")),
        "触发价": _s(row.get("triggerPrice", row.get("stopPrice"))),
        "委托价格": _s(row.get("price")),
        "数量": _s(row.get("quantity", row.get("origQty"))),
        "全部平仓": row.get("closePosition"),
        "只减仓": row.get("reduceOnly"),
        "触发价格类型": _working_type(str(row.get("workingType", ""))),
        "条件单ID": row.get("algoId"),
        "客户端条件单ID": row.get("clientAlgoId"),
        "原始字段": to_jsonable(row),
    }

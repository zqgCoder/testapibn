from __future__ import annotations

import json
from datetime import datetime, timezone

from .journal import STATUS_LABELS_ZH
from .okx_error_observability import extract_okx_error_from_result
from .redaction import redact_json_text, redact_sensitive
from .storage import TradeJournalStore


class TradeStatsService:
    def __init__(self, store: TradeJournalStore):
        self.store = store

    @staticmethod
    def _today_start_iso() -> str:
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.isoformat()

    def summary(self) -> dict:
        totals = self.store.count_by_status()
        total_executions = sum(totals.values())
        protected = totals.get("protected", 0)
        today_totals = self.store.count_by_status_since(self._today_start_iso())
        today_total = sum(today_totals.values())
        success_rate = format(protected / total_executions, "f") if total_executions else "0"
        return {
            "total_executions": total_executions,
            "protected_count": protected,
            "entry_not_filled_count": totals.get("entry_not_filled", 0),
            "blocked_by_account_risk_count": totals.get("blocked_by_account_risk", 0),
            "blocked_by_runtime_lock_count": totals.get("blocked_by_runtime_lock", 0),
            "skipped_by_position_policy_count": totals.get("skipped_by_position_policy", 0),
            "protection_failed_count": totals.get("protection_failed", 0),
            "failed_count": totals.get("failed", 0),
            "success_rate": success_rate,
            "today_executions": today_total,
            "today_protected": today_totals.get("protected", 0),
        }

    def by_symbol(self) -> list[dict]:
        return self.store.stats_by_symbol()

    def rejections(self, limit: int = 20) -> list[dict]:
        return self.store.stats_rejections(limit=limit)

    @staticmethod
    def _okx_error_brief_from_row(row: dict) -> dict:
        raw = row.get("result_json")
        if not raw:
            return {}
        try:
            result = json.loads(raw)
        except Exception:
            return {}
        return extract_okx_error_from_result(result)

    @staticmethod
    def execution_brief(row: dict) -> dict:
        status = row.get("status")
        brief = {
            "编号": row.get("id"),
            "信号编号": row.get("signal_key"),
            "信号ID": row.get("signal_id"),
            "交易对": row.get("symbol"),
            "方向": row.get("side"),
            "状态": status,
            "状态说明": STATUS_LABELS_ZH.get(status, status),
            "跳过原因": row.get("skip_reason"),
            "计划数量": row.get("planned_qty"),
            "成交数量": row.get("filled_qty"),
            "进场价": row.get("entry_price"),
            "杠杆": row.get("leverage"),
            "创建时间": row.get("created_at"),
            "更新时间": row.get("updated_at"),
            "错误信息": row.get("error_message"),
        }
        brief.update(TradeStatsService._okx_error_brief_from_row(row))
        return brief

    @staticmethod
    def execution_detail(row: dict) -> dict:
        detail = TradeStatsService.execution_brief(row)
        detail.update(
            {
                "进场方式": row.get("entry_type"),
                "风险模式": row.get("risk_mode"),
                "持仓策略": row.get("position_policy"),
                "错误信息": row.get("error_message"),
                "止损价": row.get("stop_loss_price"),
                "目标风险USDT": row.get("target_risk_usdt"),
                "估算止损总亏损": row.get("estimated_total_loss_at_sl"),
                "账户风控通过": row.get("account_risk_allowed"),
                "账户风控拒绝原因": row.get("account_risk_skip_reason"),
                "原始信号": redact_json_text(row.get("raw_signal_json")),
                "交易计划": redact_json_text(row.get("plan_json")),
                "账户风控": redact_json_text(row.get("account_risk_json")),
                "进场摘要": redact_json_text(row.get("entry_summary_json")),
                "保护单摘要": redact_json_text(row.get("protection_summary_json")),
                "执行结果": redact_json_text(row.get("result_json")),
            }
        )
        return redact_sensitive(detail)

    @staticmethod
    def order_brief(row: dict) -> dict:
        brief = {
            "编号": row.get("id"),
            "执行编号": row.get("execution_id"),
            "信号编号": row.get("signal_key"),
            "交易对": row.get("symbol"),
            "角色": row.get("role"),
            "订单ID": row.get("order_id"),
            "条件单ID": row.get("algo_id"),
            "客户端订单ID": row.get("client_order_id"),
            "方向": row.get("side"),
            "订单类型": row.get("order_type"),
            "状态": row.get("status"),
            "价格": row.get("price"),
            "成交均价": row.get("avg_price"),
            "数量": row.get("quantity"),
            "成交数量": row.get("executed_qty"),
            "触发价": row.get("trigger_price"),
            "只减仓": row.get("reduce_only"),
            "全平": row.get("close_position"),
            "原始订单": redact_json_text(row.get("raw_order_json")),
            "创建时间": row.get("created_at"),
        }
        return redact_sensitive(brief)

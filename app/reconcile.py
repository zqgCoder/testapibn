from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .binance_client import BinanceClient
from .config import Settings
from .runtime_control import RuntimeControl
from .tv_sandbox import binance_env_label

logger = logging.getLogger(__name__)

_LEVEL_RANK = {"OK": 0, "WARN": 1, "ERROR": 2, "INFO": -1}


@dataclass
class SafetyAuditCheck:
    name: str
    level: str
    message: str
    symbol: str | None = None


@dataclass
class SafetyAuditReport:
    success: bool
    level: str
    generated_at: str
    binance_env: str
    runtime: dict[str, Any]
    summary: dict[str, Any]
    checks: list[SafetyAuditCheck]
    symbols: list[dict[str, Any]]
    trigger: str = "manual"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "level": self.level,
            "generated_at": self.generated_at,
            "binance_env": self.binance_env,
            "runtime": self.runtime,
            "summary": self.summary,
            "checks": [asdict(item) for item in self.checks],
            "symbols": self.symbols,
            "trigger": self.trigger,
            "error": self.error,
        }


def _aggregate_level(levels: list[str]) -> str:
    max_rank = 0
    result = "OK"
    for level in levels:
        rank = _LEVEL_RANK.get(level, 0)
        if rank > max_rank:
            max_rank = rank
            result = level
    return result


def _safe_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _is_truthy(value: Any) -> bool:
    return str(value).lower() in {"true", "1"}


def _order_type(row: dict[str, Any]) -> str:
    return str(row.get("orderType") or row.get("type") or "").upper()


def _is_stop_loss_algo_order(row: dict[str, Any]) -> bool:
    order_type = _order_type(row)
    if "TAKE_PROFIT" in order_type:
        return False
    if order_type in {"STOP_MARKET", "STOP", "TRAILING_STOP_MARKET"}:
        return True
    if "STOP" in order_type and "TAKE_PROFIT" not in order_type:
        return True
    if _is_truthy(row.get("closePosition")) and "TAKE_PROFIT" not in order_type:
        return True
    return False


def _is_take_profit_algo_order(row: dict[str, Any]) -> bool:
    return "TAKE_PROFIT" in _order_type(row)


def _order_quantity(row: dict[str, Any]) -> Decimal:
    for key in ("quantity", "origQty", "qty"):
        value = row.get(key)
        if value not in {None, "", "0", "0.0"}:
            return _safe_decimal(value)
    return Decimal("0")


def _expected_close_side(position_amt: Decimal) -> str | None:
    if position_amt > 0:
        return "SELL"
    if position_amt < 0:
        return "BUY"
    return None


def _position_side_label(position_amt: Decimal) -> str | None:
    if position_amt > 0:
        return "LONG"
    if position_amt < 0:
        return "SHORT"
    return None


def _qty_within_tolerance(actual: Decimal, expected: Decimal, ratio: Decimal = Decimal("0.02")) -> bool:
    if expected <= 0:
        return actual <= 0
    if actual >= expected:
        return True
    gap = expected - actual
    return gap / expected <= ratio


class SafetyReconcileService:
    """Read-only startup / manual safety audit against Binance account state."""

    def __init__(
        self,
        settings: Settings,
        client: BinanceClient,
        runtime_control: RuntimeControl,
        *,
        persist_path: str = "data/reconcile_latest.json",
    ) -> None:
        self.settings = settings
        self.client = client
        self.runtime_control = runtime_control
        self.persist_path = persist_path
        self._latest_report: dict[str, Any] | None = None
        self._load_persisted()

    def get_latest_report(self) -> dict[str, Any] | None:
        return self._latest_report

    def run_audit(self, *, trigger: str = "manual") -> dict[str, Any]:
        try:
            report = self._build_report(trigger=trigger)
        except Exception as exc:
            logger.exception("Safety audit failed: trigger=%s", trigger)
            report = self._error_report(str(exc)[:500], trigger=trigger)
        self._store_report(report)
        return report

    def _load_persisted(self) -> None:
        path = Path(self.persist_path)
        if not path.is_file():
            return
        try:
            with path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                self._latest_report = payload
        except Exception as exc:
            logger.warning("Failed to load persisted reconcile report: %s", exc)

    def _store_report(self, report: dict[str, Any]) -> None:
        self._latest_report = report
        if not self.persist_path:
            return
        path = Path(self.persist_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, default=str)

    def _audit_symbols(self) -> list[str]:
        symbols = set(self.settings.allowed_symbol_set)
        symbols.update(self.settings.tv_alert_expected_symbol_set)
        return sorted(symbols)

    def _error_report(self, message: str, *, trigger: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        env = binance_env_label(self.settings.binance_base_url)
        runtime = self.runtime_control.status_payload()
        check = SafetyAuditCheck(
            name="audit_execution",
            level="ERROR",
            message=f"安全审计执行失败: {message}",
        )
        report = SafetyAuditReport(
            success=False,
            level="ERROR",
            generated_at=now,
            binance_env=env,
            runtime=runtime,
            summary={
                "symbols_checked": 0,
                "open_position_count": 0,
                "unprotected_position_count": 0,
                "residual_order_symbol_count": 0,
                "error_count": 1,
                "warn_count": 0,
            },
            checks=[check],
            symbols=[],
            trigger=trigger,
            error=message,
        )
        return report.to_dict()

    def _build_report(self, *, trigger: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        env = binance_env_label(self.settings.binance_base_url)
        runtime = self.runtime_control.status_payload()
        global_checks: list[SafetyAuditCheck] = []
        symbol_reports: list[dict[str, Any]] = []

        if self.settings.runtime_control_enabled:
            global_checks.append(
                SafetyAuditCheck(
                    name="runtime_control_enabled",
                    level="OK",
                    message="Runtime Control 已启用",
                )
            )
        else:
            global_checks.append(
                SafetyAuditCheck(
                    name="runtime_control_enabled",
                    level="ERROR",
                    message="Runtime Control 未启用",
                )
            )

        effective_locked = bool(runtime.get("effective_locked", runtime.get("locked")))
        one_shot = runtime.get("one_shot") or {}
        one_shot_active = bool(one_shot.get("enabled")) and int(one_shot.get("remaining") or 0) > 0

        if effective_locked:
            global_checks.append(
                SafetyAuditCheck(
                    name="runtime_locked",
                    level="OK",
                    message="Runtime 当前已锁定",
                )
            )
        else:
            global_checks.append(
                SafetyAuditCheck(
                    name="runtime_locked",
                    level="ERROR",
                    message="Runtime 当前未锁定，存在自动执行风险",
                )
            )

        if one_shot_active:
            global_checks.append(
                SafetyAuditCheck(
                    name="runtime_one_shot",
                    level="WARN",
                    message=(
                        f"当前处于 Runtime One-Shot 解锁窗口，剩余 {one_shot.get('remaining', 0)} 次，"
                        f"expires={one_shot.get('expires_at') or '-'}"
                    ),
                )
            )

        if env == "demo":
            global_checks.append(
                SafetyAuditCheck(
                    name="binance_demo_environment",
                    level="OK",
                    message=f"Binance 环境为 demo/testnet ({self.settings.binance_base_url})",
                )
            )
        else:
            global_checks.append(
                SafetyAuditCheck(
                    name="binance_demo_environment",
                    level="ERROR",
                    message=f"Binance 环境不是 demo/testnet (binance_env={env})",
                )
            )

        if self.settings.enable_trading:
            global_checks.append(
                SafetyAuditCheck(
                    name="trading_enabled",
                    level="WARN",
                    message="ENABLE_TRADING=true，demo 环境也会真实下 demo 单，请确认风控",
                )
            )
        else:
            global_checks.append(
                SafetyAuditCheck(
                    name="trading_enabled",
                    level="OK",
                    message="ENABLE_TRADING=false，不会自动真实下单",
                )
            )

        if self.settings.tv_signal_reject_live_binance:
            global_checks.append(
                SafetyAuditCheck(
                    name="tv_sandbox_guard",
                    level="OK",
                    message="TV_SIGNAL_REJECT_LIVE_BINANCE=true，已拒绝实盘 endpoint 的 TV 信号",
                )
            )
        else:
            global_checks.append(
                SafetyAuditCheck(
                    name="tv_sandbox_guard",
                    level="WARN",
                    message="TV_SIGNAL_REJECT_LIVE_BINANCE=false，TV 信号可能在非 demo 环境被执行",
                )
            )

        symbols = self._audit_symbols()
        open_position_count = 0
        unprotected_position_count = 0
        residual_order_symbol_count = 0

        for symbol in symbols:
            symbol_checks: list[SafetyAuditCheck] = []
            position_row: dict[str, Any] | None = None
            position_amt = Decimal("0")
            try:
                data = self.client.position_risk(symbol)
                rows = data if isinstance(data, list) else [data]
                for row in rows:
                    if str(row.get("symbol", "")).upper() == symbol:
                        position_row = row if isinstance(row, dict) else None
                        position_amt = _safe_decimal(row.get("positionAmt", "0"))
                        break
            except Exception as exc:
                symbol_checks.append(
                    SafetyAuditCheck(
                        name="symbol_data",
                        level="ERROR",
                        message=f"查询 {symbol} 持仓失败: {str(exc)[:200]}",
                        symbol=symbol,
                    )
                )

            open_orders: list[dict[str, Any]] = []
            algo_orders: list[dict[str, Any]] = []
            try:
                rows = self.client.open_orders(symbol)
                open_orders = rows if isinstance(rows, list) else []
            except Exception as exc:
                symbol_checks.append(
                    SafetyAuditCheck(
                        name="open_orders_fetch",
                        level="WARN",
                        message=f"查询 {symbol} 普通委托失败: {str(exc)[:200]}",
                        symbol=symbol,
                    )
                )
            try:
                rows = self.client.open_algo_orders(symbol)
                algo_orders = rows if isinstance(rows, list) else []
            except Exception as exc:
                symbol_checks.append(
                    SafetyAuditCheck(
                        name="algo_orders_fetch",
                        level="WARN",
                        message=f"查询 {symbol} 条件单失败: {str(exc)[:200]}",
                        symbol=symbol,
                    )
                )

            has_position = position_amt != 0
            if has_position:
                open_position_count += 1

            sl_orders = [row for row in algo_orders if isinstance(row, dict) and _is_stop_loss_algo_order(row)]
            tp_orders = [row for row in algo_orders if isinstance(row, dict) and _is_take_profit_algo_order(row)]
            has_stop_loss = bool(sl_orders)
            has_take_profit = bool(tp_orders)

            if not has_position and not open_orders and not algo_orders:
                symbol_checks.append(
                    SafetyAuditCheck(
                        name="flat_and_clean",
                        level="OK",
                        message=f"{symbol} 无持仓且无残留委托",
                        symbol=symbol,
                    )
                )
            elif not has_position:
                if open_orders or algo_orders:
                    residual_order_symbol_count += 1
                if open_orders:
                    symbol_checks.append(
                        SafetyAuditCheck(
                            name="residual_regular_orders",
                            level="WARN",
                            message="无持仓但存在普通委托，可能是残留订单",
                            symbol=symbol,
                        )
                    )
                if algo_orders:
                    symbol_checks.append(
                        SafetyAuditCheck(
                            name="residual_algo_orders",
                            level="WARN",
                            message="无持仓但存在残留条件单，建议调用 /positions/{symbol}/cleanup",
                            symbol=symbol,
                        )
                    )

            if has_position:
                expected_close = _expected_close_side(position_amt)
                position_abs = abs(position_amt)

                if not sl_orders:
                    unprotected_position_count += 1
                    symbol_checks.append(
                        SafetyAuditCheck(
                            name="missing_stop_loss",
                            level="ERROR",
                            message="存在未受止损保护的持仓",
                            symbol=symbol,
                        )
                    )

                if not tp_orders:
                    symbol_checks.append(
                        SafetyAuditCheck(
                            name="missing_take_profit",
                            level="WARN",
                            message="存在持仓但没有止盈保护",
                            symbol=symbol,
                        )
                    )

                if not algo_orders:
                    symbol_checks.append(
                        SafetyAuditCheck(
                            name="missing_algo_orders",
                            level="ERROR",
                            message="有持仓但条件单数量为 0",
                            symbol=symbol,
                        )
                    )

                for order in sl_orders + tp_orders:
                    side = str(order.get("side") or "").upper()
                    if expected_close and side and side != expected_close:
                        symbol_checks.append(
                            SafetyAuditCheck(
                                name="protection_side_mismatch",
                                level="ERROR",
                                message=(
                                    f"保护单方向不匹配：持仓={_position_side_label(position_amt)}，"
                                    f"期望平仓方向={expected_close}，实际={side}"
                                ),
                                symbol=symbol,
                            )
                        )

                for order in sl_orders + tp_orders:
                    if not _is_truthy(order.get("reduceOnly")):
                        symbol_checks.append(
                            SafetyAuditCheck(
                                name="protection_reduce_only",
                                level="ERROR",
                                message=(
                                    f"{_order_type(order)} 未设置 reduceOnly=true，存在反向开仓风险"
                                ),
                                symbol=symbol,
                            )
                        )

                for order in sl_orders:
                    if _order_type(order) == "STOP_MARKET":
                        if _is_truthy(order.get("closePosition")):
                            symbol_checks.append(
                                SafetyAuditCheck(
                                    name="stop_loss_close_position",
                                    level="OK",
                                    message="STOP_MARKET 使用 closePosition=true",
                                    symbol=symbol,
                                )
                            )
                        else:
                            qty = _order_quantity(order)
                            if not _qty_within_tolerance(qty, position_abs):
                                symbol_checks.append(
                                    SafetyAuditCheck(
                                        name="stop_loss_quantity",
                                        level="ERROR",
                                        message=(
                                            f"STOP_MARKET 未 closePosition 且数量不足覆盖仓位: "
                                            f"qty={qty}, position={position_abs}"
                                        ),
                                        symbol=symbol,
                                    )
                                )

                if len(tp_orders) > 1:
                    tp_total = sum(_order_quantity(order) for order in tp_orders)
                    if not _qty_within_tolerance(tp_total, position_abs):
                        symbol_checks.append(
                            SafetyAuditCheck(
                                name="take_profit_quantity_low",
                                level="WARN",
                                message=(
                                    f"分批止盈数量合计明显小于仓位: total={tp_total}, position={position_abs}"
                                ),
                                symbol=symbol,
                            )
                        )
                    elif tp_total > position_abs * Decimal("1.05"):
                        over_level = "ERROR" if tp_total > position_abs * Decimal("1.20") else "WARN"
                        symbol_checks.append(
                            SafetyAuditCheck(
                                name="take_profit_quantity_high",
                                level=over_level,
                                message=(
                                    f"分批止盈数量合计明显大于仓位: total={tp_total}, position={position_abs}"
                                ),
                                symbol=symbol,
                            )
                        )

                if has_stop_loss and has_take_profit and sl_orders:
                    symbol_checks.append(
                        SafetyAuditCheck(
                            name="position_protected",
                            level="OK",
                            message=f"{symbol} 持仓已检测到止损与止盈保护",
                            symbol=symbol,
                        )
                    )
                elif has_stop_loss:
                    symbol_checks.append(
                        SafetyAuditCheck(
                            name="position_stop_loss_only",
                            level="OK",
                            message=f"{symbol} 持仓已检测到止损保护",
                            symbol=symbol,
                        )
                    )

            symbol_level = _aggregate_level([check.level for check in symbol_checks] or ["OK"])
            symbol_reports.append(
                {
                    "symbol": symbol,
                    "position_amt": format(position_amt, "f"),
                    "side": _position_side_label(position_amt),
                    "open_orders_count": len(open_orders),
                    "algo_orders_count": len(algo_orders),
                    "has_stop_loss": has_stop_loss,
                    "has_take_profit": has_take_profit,
                    "protection_level": symbol_level,
                    "checks": [asdict(item) for item in symbol_checks],
                }
            )

        all_checks = global_checks + [
            SafetyAuditCheck(
                name=item["name"],
                level=item["level"],
                message=item["message"],
                symbol=item.get("symbol"),
            )
            for report in symbol_reports
            for item in report.get("checks", [])
        ]
        error_count = sum(1 for check in all_checks if check.level == "ERROR")
        warn_count = sum(1 for check in all_checks if check.level == "WARN")
        overall_level = _aggregate_level([check.level for check in all_checks])

        report = SafetyAuditReport(
            success=overall_level != "ERROR",
            level=overall_level,
            generated_at=now,
            binance_env=env,
            runtime=runtime,
            summary={
                "symbols_checked": len(symbols),
                "open_position_count": open_position_count,
                "unprotected_position_count": unprotected_position_count,
                "residual_order_symbol_count": residual_order_symbol_count,
                "error_count": error_count,
                "warn_count": warn_count,
            },
            checks=global_checks,
            symbols=symbol_reports,
            trigger=trigger,
        )
        return report.to_dict()


def merge_reconcile_into_health_overview(
    overview: dict[str, Any],
    report: dict[str, Any] | None,
) -> dict[str, Any]:
    summary = overview.setdefault("summary", {})
    checks: list[dict[str, str]] = list(overview.get("checks") or [])

    if not report:
        checks.append(
            {
                "name": "safety_audit",
                "level": "WARN",
                "message": "尚未生成安全审计报告",
            }
        )
        overview["checks"] = checks
        overview["level"] = _aggregate_level([check.get("level", "OK") for check in checks])
        return overview

    audit_summary = report.get("summary") or {}
    summary["reconcile_level"] = report.get("level")
    summary["reconcile_generated_at"] = report.get("generated_at")
    summary["reconcile_open_position_count"] = audit_summary.get("open_position_count")
    summary["reconcile_unprotected_position_count"] = audit_summary.get("unprotected_position_count")
    summary["reconcile_residual_order_symbol_count"] = audit_summary.get("residual_order_symbol_count")

    level = str(report.get("level") or "OK")
    checks.append(
        {
            "name": "safety_audit",
            "level": level if level in {"OK", "WARN", "ERROR"} else "WARN",
            "message": (
                f"最近安全审计 level={level}, 持仓={audit_summary.get('open_position_count', 0)}, "
                f"未保护={audit_summary.get('unprotected_position_count', 0)}, "
                f"残留委托={audit_summary.get('residual_order_symbol_count', 0)}"
            ),
        }
    )
    overview["checks"] = checks
    overview["level"] = _aggregate_level([check.get("level", "OK") for check in checks])
    return overview


def alerts_from_reconcile_report(
    report: dict[str, Any] | None,
    *,
    created_at: str | None,
) -> list[dict[str, Any]]:
    if not report:
        return [
            {
                "id": "reconcile-missing",
                "source": "reconcile",
                "level": "WARN",
                "type": "safety_audit_missing",
                "title": "安全审计尚未生成",
                "message": "服务启动后尚未完成安全审计，或审计报告不可用",
                "symbol": None,
                "status": None,
                "reason": None,
                "created_at": created_at,
            }
        ]

    alerts: list[dict[str, Any]] = []
    overall_level = str(report.get("level") or "OK")
    audit_summary = report.get("summary") or {}
    if overall_level in {"WARN", "ERROR"}:
        alerts.append(
            {
                "id": "reconcile-overall",
                "source": "reconcile",
                "level": overall_level,
                "type": "safety_audit",
                "title": "安全审计摘要",
                "message": (
                    f"level={overall_level}, 持仓={audit_summary.get('open_position_count', 0)}, "
                    f"未保护={audit_summary.get('unprotected_position_count', 0)}, "
                    f"残留委托={audit_summary.get('residual_order_symbol_count', 0)}"
                ),
                "symbol": None,
                "status": None,
                "reason": None,
                "created_at": report.get("generated_at") or created_at,
            }
        )

    def _append_alert(check: dict[str, Any], *, symbol: str | None) -> None:
        level = check.get("level")
        if level not in {"WARN", "ERROR"}:
            return
        name = str(check.get("name") or "safety_audit")
        sym_suffix = f"-{symbol}" if symbol else ""
        alerts.append(
            {
                "id": f"reconcile-{name}{sym_suffix}",
                "source": "reconcile",
                "level": level,
                "type": name,
                "title": name,
                "message": str(check.get("message") or ""),
                "symbol": symbol,
                "status": None,
                "reason": name,
                "created_at": report.get("generated_at") or created_at,
            }
        )

    for check in report.get("checks") or []:
        if isinstance(check, dict):
            _append_alert(check, symbol=check.get("symbol"))

    for symbol_report in report.get("symbols") or []:
        if not isinstance(symbol_report, dict):
            continue
        symbol = symbol_report.get("symbol")
        for check in symbol_report.get("checks") or []:
            if isinstance(check, dict):
                _append_alert(check, symbol=symbol)

    return alerts

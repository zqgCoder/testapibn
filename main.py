from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.binance_client import BinanceClient
from app.config import get_settings
from app.exchange_rules import ExchangeRules
from app.schemas import TradingViewSignal, normalize_side
from app.storage import AccountRiskStore, RuntimeControlStore, SignalStore, TradeJournalStore
from app.account_risk import AccountRiskGuard
from app.journal import TradeJournal, resolve_execution_status
from app.stats import TradeStatsService
from app.dashboard import create_dashboard_router, require_api_token_if_protected
from app.runtime_control import (
    LockRequest,
    PositionCleanupRequest,
    PositionCloseRequest,
    RuntimeControl,
    UnlockOnceRequest,
    UnlockRequest,
    assert_demo_maintenance_allowed,
    verify_runtime_control_write_token,
    verify_runtime_read_token,
)
from app.reconcile import SafetyReconcileService
from app.trader import Trader
from app.tv_sandbox import is_tv_signal, validate_tv_payload, validate_tv_policy
from app.tv_production import apply_position_strategy, position_strategy_invalid_rejection
from app.zh import algo_order_to_chinese, order_to_chinese, position_to_chinese, to_jsonable, trade_plan_raw, trade_plan_to_chinese

settings = get_settings()

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler("logs/bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
    ],
)
logger = logging.getLogger("tv_binance_bot")

client = BinanceClient(settings)
rules = ExchangeRules(client)
account_risk_store = AccountRiskStore(settings.sqlite_path)
account_risk = AccountRiskGuard(settings, client, account_risk_store)
journal_store = TradeJournalStore(settings.sqlite_path)
trade_journal = TradeJournal(journal_store)
trade_stats = TradeStatsService(journal_store)
runtime_store = RuntimeControlStore(settings.sqlite_path)
runtime_control = RuntimeControl(settings, runtime_store)
trader = Trader(settings, client, rules, account_risk=account_risk, runtime_control=runtime_control)
reconcile_service = SafetyReconcileService(settings, client, runtime_control)
store = SignalStore(settings.sqlite_path)

APP_VERSION = "1.13.0"

app = FastAPI(title="TradingView to Binance Futures Bot", version=APP_VERSION)

app.include_router(
    create_dashboard_router(
        settings, journal_store, trade_stats, client, APP_VERSION, runtime_control, reconcile_service
    )
)


@app.on_event("startup")
async def startup_safety_audit() -> None:
    try:
        reconcile_service.run_audit(trigger="startup")
        logger.info(
            "Startup safety audit completed: level=%s",
            (reconcile_service.get_latest_report() or {}).get("level"),
        )
    except Exception:
        logger.exception("Startup safety audit failed unexpectedly")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    details = []
    for err in exc.errors():
        loc = " -> ".join(str(x) for x in err.get("loc", []))
        msg = err.get("msg", "参数错误")
        details.append({"位置": loc, "说明": msg, "输入值": err.get("input")})
    return JSONResponse(
        status_code=422,
        content={
            "成功": False,
            "错误": "请求参数校验失败，请检查 JSON 字段和值。",
            "详情": details,
        },
    )


def make_signal_key_from_raw(raw_payload: dict) -> str:
    signal_id = str(raw_payload.get("signal_id") or "").strip()
    if signal_id:
        return signal_id
    normalized = json.dumps(raw_payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def make_signal_key(signal: TradingViewSignal, raw_payload: dict) -> str:
    """
    Prefer signal_id when supplied by TradingView/Pine.
    Otherwise hash the JSON payload to reduce accidental duplicate processing.
    """
    if signal.signal_id:
        return signal.signal_id.strip()
    normalized = json.dumps(raw_payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def execute_background(signal: TradingViewSignal, signal_key: str, raw_payload: dict) -> None:
    result: dict | None = None
    final_status = "failed"
    is_tv = is_tv_signal(raw_payload, settings)
    try:
        result = trader.execute(signal, signal_key, raw_payload)
        trade_journal.persist_execution(signal, signal_key, raw_payload, result)
        store.mark_done(signal_key)
        final_status = resolve_execution_status(result)
    except Exception as exc:
        logger.exception("Signal execution failed: key=%s error=%s", signal_key, exc)
        trade_journal.persist_failure(signal, signal_key, raw_payload, exc, result)
        store.mark_failed(signal_key, str(exc))
        final_status = "failed"
    finally:
        if is_tv:
            signal_id = (signal.signal_id or signal_key or "unknown").strip() or "unknown"
            runtime_control.maybe_consume_one_shot_for_tv_signal(signal_id, final_status)


@app.get("/health")
async def health():
    return {
        "成功": True,
        "是否允许真实下单": settings.enable_trading,
        "币安接口地址": settings.binance_base_url,
        "允许交易的币对": sorted(settings.allowed_symbol_set),
    }


def clean_path_symbol(symbol: str) -> str:
    s = symbol.strip().upper().replace("BINANCE:", "").replace(".P", "")
    if not s:
        raise HTTPException(status_code=422, detail="交易对不能为空")
    if settings.allowed_symbol_set and s not in settings.allowed_symbol_set:
        raise HTTPException(status_code=422, detail=f"交易对 {s} 不在 ALLOWED_SYMBOLS 白名单中")
    return s


@app.get("/account")
async def account():
    """查看合约账户余额。"""
    balances = client.futures_balance()
    return JSONResponse(
        content={
            "成功": True,
            "账户余额": to_jsonable(balances),
        }
    )


@app.get("/positions")
async def positions(include_zero: bool = False):
    """查看当前合约持仓。默认只显示非 0 持仓。"""
    rows = client.position_risk()
    rows = rows if isinstance(rows, list) else [rows]
    if not include_zero:
        rows = [r for r in rows if str(r.get("positionAmt", "0")) not in {"0", "0.0", "0.00", "0.000", "0.0000"}]
    return JSONResponse(
        content={
            "成功": True,
            "持仓数量": len(rows),
            "持仓": [position_to_chinese(r) for r in rows],
        }
    )


@app.get("/positions/{symbol}")
async def position_by_symbol(symbol: str):
    """查看某个交易对的持仓。"""
    symbol = clean_path_symbol(symbol)
    rows = client.position_risk(symbol)
    rows = rows if isinstance(rows, list) else [rows]
    return JSONResponse(
        content={
            "成功": True,
            "交易对": symbol,
            "持仓": [position_to_chinese(r) for r in rows],
        }
    )


@app.get("/open-orders/{symbol}")
async def open_orders(symbol: str):
    """查看某个交易对的普通未成交委托。"""
    symbol = clean_path_symbol(symbol)
    rows = client.open_orders(symbol)
    return JSONResponse(
        content={
            "成功": True,
            "交易对": symbol,
            "普通委托数量": len(rows),
            "普通委托": [order_to_chinese(r) for r in rows],
        }
    )


@app.get("/algo-orders/{symbol}")
async def algo_orders(symbol: str):
    """查看某个交易对的止损/止盈等条件单。"""
    symbol = clean_path_symbol(symbol)
    rows = client.open_algo_orders(symbol)
    return JSONResponse(
        content={
            "成功": True,
            "交易对": symbol,
            "条件单数量": len(rows),
            "条件单": [algo_order_to_chinese(r) for r in rows],
        }
    )


@app.delete("/orders/{symbol}")
async def cancel_orders(symbol: str):
    """手动取消某个交易对的普通委托和条件单。"""
    symbol = clean_path_symbol(symbol)
    result = trader.cancel_symbol_open_orders(symbol)
    return JSONResponse(
        content={
            "成功": True,
            "交易对": symbol,
            "提示": "已尝试取消普通委托和条件单。",
            "结果": to_jsonable(result),
        }
    )


def _maintenance_result_payload(result: dict) -> dict:
    """Map Trader maintenance result fields to Chinese JSON response keys."""
    payload = {
        "symbol": result.get("symbol"),
        "position_before": result.get("position_before"),
        "close_side": result.get("close_side"),
        "close_quantity": result.get("close_quantity"),
        "close_order": result.get("close_order"),
        "position_after": result.get("position_after"),
        "cancel_regular_before": result.get("cancel_regular_before"),
        "cancel_algo_before": result.get("cancel_algo_before"),
        "cancel_regular_after": result.get("cancel_regular_after"),
        "cancel_algo_after": result.get("cancel_algo_after"),
        "success": result.get("success"),
        "status": result.get("status"),
    }
    if result.get("close_error"):
        payload["close_error"] = result["close_error"]
    return to_jsonable(payload)


@app.post("/positions/{symbol}/close")
async def close_position_maintenance(
    symbol: str,
    body: PositionCloseRequest,
    control_token: str | None = Query(None),
    x_runtime_control_token: str | None = Header(None, alias="X-Runtime-Control-Token"),
):
    """关闭指定交易对持仓并清理保护单（维护接口，Runtime 锁定期间可用）。"""
    verify_runtime_control_write_token(
        settings,
        control_token=control_token,
        header_token=x_runtime_control_token,
    )
    assert_demo_maintenance_allowed(settings)
    symbol = clean_path_symbol(symbol)
    try:
        result = trader.close_position_maintenance(
            symbol,
            reason=body.reason.strip(),
            operator=body.operator.strip() or "local-admin",
            cancel_before_close=body.cancel_before_close,
            cancel_after_close=body.cancel_after_close,
            wait_seconds=body.wait_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    structured = _maintenance_result_payload(result)
    if not result.get("success"):
        status_code = 502 if result.get("status") == "close_order_failed" else 409
        return JSONResponse(
            status_code=status_code,
            content={
                "成功": False,
                "交易对": symbol,
                "状态": result.get("status"),
                "提示": "平仓维护未完成，请查看结果详情。",
                "结果": structured,
            },
        )

    return JSONResponse(
        content={
            "成功": True,
            "交易对": symbol,
            "状态": result.get("status"),
            "提示": "平仓维护已完成。",
            "结果": structured,
        }
    )


@app.post("/positions/{symbol}/cleanup")
async def cleanup_position_orders(
    symbol: str,
    body: PositionCleanupRequest,
    control_token: str | None = Query(None),
    x_runtime_control_token: str | None = Header(None, alias="X-Runtime-Control-Token"),
):
    """取消指定交易对残留普通委托与条件单，不平仓（维护接口，Runtime 锁定期间可用）。"""
    verify_runtime_control_write_token(
        settings,
        control_token=control_token,
        header_token=x_runtime_control_token,
    )
    assert_demo_maintenance_allowed(settings)
    symbol = clean_path_symbol(symbol)
    try:
        result = trader.cleanup_symbol_orders(
            symbol,
            reason=body.reason.strip(),
            operator=body.operator.strip() or "local-admin",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    open_orders = result.get("open_orders") or []
    algo_orders = result.get("algo_orders") or []
    position_row = result.get("position")
    return JSONResponse(
        content={
            "成功": True,
            "交易对": symbol,
            "提示": "已尝试清理残留委托，当前持仓未主动平仓。",
            "结果": to_jsonable(
                {
                    "symbol": result.get("symbol"),
                    "cancel_regular": result.get("cancel_regular"),
                    "cancel_algo": result.get("cancel_algo"),
                    "cancel_regular_error": result.get("cancel_regular_error"),
                    "cancel_algo_error": result.get("cancel_algo_error"),
                    "position": position_row,
                    "open_orders": open_orders,
                    "algo_orders": algo_orders,
                }
            ),
            "持仓": position_to_chinese(position_row) if position_row else None,
            "普通委托数量": len(open_orders),
            "普通委托": [order_to_chinese(r) for r in open_orders],
            "条件单数量": len(algo_orders),
            "条件单": [algo_order_to_chinese(r) for r in algo_orders],
        }
    )


def _tv_rejection_response(signal_key: str, rejection) -> JSONResponse:
    content = {
        "成功": False,
        "跳过": True,
        "跳过原因": rejection.skip_reason,
        "提示": rejection.message,
        "信号编号": signal_key,
    }
    if rejection.invalid_fields:
        content["错误字段"] = rejection.invalid_fields
    return JSONResponse(status_code=200, content=content)


def _persist_tv_rejection(signal_key: str, raw_payload: dict, rejection) -> None:
    if store.exists(signal_key):
        return
    side = str(raw_payload.get("side") or "")
    store.mark_received(
        signal_key,
        str(raw_payload.get("symbol") or ""),
        side,
        json.dumps(raw_payload, default=str, ensure_ascii=False),
    )
    trade_journal.persist_tv_sandbox_rejection(signal_key, raw_payload, rejection)
    store.mark_failed(signal_key, rejection.skip_reason)


@app.post("/tradingview")
async def tradingview_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        raw_payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是合法 JSON")

    if not isinstance(raw_payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

    tv_signal = is_tv_signal(raw_payload, settings)

    if settings.tv_signal_sandbox_enabled and tv_signal:
        payload_rejection = validate_tv_payload(raw_payload, settings)
        if payload_rejection is not None:
            signal_key = make_signal_key_from_raw(raw_payload)
            _persist_tv_rejection(signal_key, raw_payload, payload_rejection)
            return _tv_rejection_response(signal_key, payload_rejection)

    if tv_signal:
        try:
            raw_payload = apply_position_strategy(raw_payload, settings)
        except ValueError:
            rejection = position_strategy_invalid_rejection()
            signal_key = make_signal_key_from_raw(raw_payload)
            _persist_tv_rejection(signal_key, raw_payload, rejection)
            return _tv_rejection_response(signal_key, rejection)

    try:
        signal = TradingViewSignal.model_validate(raw_payload)
    except Exception as exc:
        if settings.tv_signal_sandbox_enabled and tv_signal:
            payload_rejection = validate_tv_payload(raw_payload, settings)
            if payload_rejection is not None:
                signal_key = make_signal_key_from_raw(raw_payload)
                _persist_tv_rejection(signal_key, raw_payload, payload_rejection)
                return _tv_rejection_response(signal_key, payload_rejection)
            from app.tv_production import tv_rejection_from_pydantic

            pydantic_rejection = tv_rejection_from_pydantic(exc)
            signal_key = make_signal_key_from_raw(raw_payload)
            _persist_tv_rejection(signal_key, raw_payload, pydantic_rejection)
            return _tv_rejection_response(signal_key, pydantic_rejection)
        raise HTTPException(status_code=422, detail=str(exc))

    if signal.secret != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="Webhook 密钥错误")

    try:
        side = normalize_side(signal.side)
        signal_key = make_signal_key(signal, raw_payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if store.exists(signal_key):
        if tv_signal:
            trade_journal.persist_duplicate_signal(signal_key, raw_payload)
        return JSONResponse(
            status_code=200,
            content={
                "成功": True,
                "是否重复信号": True,
                "跳过": True,
                "跳过原因": "duplicate_signal",
                "信号编号": signal_key,
                "提示": "该信号已经接收过，已被防重复机制拦截",
            },
        )

    if settings.tv_signal_sandbox_enabled and tv_signal:
        policy_rejection = validate_tv_policy(raw_payload, signal, settings, client=client)
        if policy_rejection is not None:
            _persist_tv_rejection(signal_key, raw_payload, policy_rejection)
            return _tv_rejection_response(signal_key, policy_rejection)

    store.mark_received(signal_key, signal.symbol, side, json.dumps(raw_payload, default=str, ensure_ascii=False))
    background_tasks.add_task(execute_background, signal, signal_key, raw_payload)

    return {
        "成功": True,
        "已接收": True,
        "信号编号": signal_key,
        "是否允许真实下单": settings.enable_trading,
        "提示": "信号已接收，请查看 logs/bot.log 获取执行详情。",
    }


@app.post("/plan")
async def plan_only(signal: TradingViewSignal):
    """Local debugging endpoint: validate a signal and return the calculated trade plan without placing orders."""
    if signal.secret != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="Webhook 密钥错误")
    signal.dry_run = True
    plan = trader.prepare_plan(signal)
    return JSONResponse(
        content={
            "成功": True,
            "交易计划": trade_plan_to_chinese(plan),
            "原始字段": trade_plan_raw(plan),
        }
    )


@app.get("/journal/executions")
async def journal_executions(
    limit: int = 50,
    symbol: str | None = None,
    status: str | None = None,
    token: str | None = Query(None),
    x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
):
    require_api_token_if_protected(
        settings,
        protect=settings.protect_journal_api,
        query_token=token,
        header_token=x_dashboard_token,
    )
    rows = journal_store.list_executions(limit=limit, symbol=symbol, status=status)
    return JSONResponse(
        content={
            "成功": True,
            "数量": len(rows),
            "记录": [TradeStatsService.execution_brief(row) for row in rows],
        }
    )


@app.get("/journal/executions/{execution_id}")
async def journal_execution_detail(
    execution_id: int,
    token: str | None = Query(None),
    x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
):
    require_api_token_if_protected(
        settings,
        protect=settings.protect_journal_api,
        query_token=token,
        header_token=x_dashboard_token,
    )
    row = journal_store.get_execution(execution_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"执行记录不存在: {execution_id}")
    return JSONResponse(
        content={
            "成功": True,
            "记录": TradeStatsService.execution_detail(row),
        }
    )


@app.get("/journal/orders/{execution_id}")
async def journal_orders(
    execution_id: int,
    token: str | None = Query(None),
    x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
):
    require_api_token_if_protected(
        settings,
        protect=settings.protect_journal_api,
        query_token=token,
        header_token=x_dashboard_token,
    )
    if journal_store.get_execution(execution_id) is None:
        raise HTTPException(status_code=404, detail=f"执行记录不存在: {execution_id}")
    rows = journal_store.list_orders(execution_id)
    return JSONResponse(
        content={
            "成功": True,
            "执行编号": execution_id,
            "订单数量": len(rows),
            "订单": [TradeStatsService.order_brief(row) for row in rows],
        }
    )


@app.get("/stats/summary")
async def stats_summary(
    token: str | None = Query(None),
    x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
):
    require_api_token_if_protected(
        settings,
        protect=settings.protect_stats_api,
        query_token=token,
        header_token=x_dashboard_token,
    )
    return JSONResponse(
        content={
            "成功": True,
            "统计": trade_stats.summary(),
        }
    )


@app.get("/stats/by-symbol")
async def stats_by_symbol(
    token: str | None = Query(None),
    x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
):
    require_api_token_if_protected(
        settings,
        protect=settings.protect_stats_api,
        query_token=token,
        header_token=x_dashboard_token,
    )
    rows = trade_stats.by_symbol()
    return JSONResponse(
        content={
            "成功": True,
            "数量": len(rows),
            "按交易对": rows,
        }
    )


@app.get("/stats/rejections")
async def stats_rejections(
    limit: int = 20,
    token: str | None = Query(None),
    x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
):
    require_api_token_if_protected(
        settings,
        protect=settings.protect_stats_api,
        query_token=token,
        header_token=x_dashboard_token,
    )
    rows = trade_stats.rejections(limit=limit)
    return JSONResponse(
        content={
            "成功": True,
            "数量": len(rows),
            "拒绝统计": rows,
        }
    )


@app.get("/reconcile/status")
async def reconcile_status(
    control_token: str | None = Query(None),
    token: str | None = Query(None),
    x_runtime_control_token: str | None = Header(None, alias="X-Runtime-Control-Token"),
    x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
):
    """返回最近一次只读安全审计报告。"""
    verify_runtime_read_token(
        settings,
        control_token=control_token,
        control_header=x_runtime_control_token,
        dashboard_token=token,
        dashboard_header=x_dashboard_token,
    )
    report = reconcile_service.get_latest_report()
    if report is None:
        return JSONResponse(
            content={
                "成功": False,
                "level": "WARN",
                "提示": "尚未生成安全审计报告，请等待服务启动审计完成或手动触发 POST /reconcile/run。",
                "报告": None,
            }
        )
    return JSONResponse(content={"成功": True, "报告": to_jsonable(report)})


@app.post("/reconcile/run")
async def reconcile_run(
    control_token: str | None = Query(None),
    x_runtime_control_token: str | None = Header(None, alias="X-Runtime-Control-Token"),
):
    """手动触发一次只读安全审计，不会下单、撤单、平仓或修改 Runtime。"""
    verify_runtime_control_write_token(
        settings,
        control_token=control_token,
        header_token=x_runtime_control_token,
    )
    report = reconcile_service.run_audit(trigger="manual")
    return JSONResponse(content={"成功": True, "报告": to_jsonable(report)})


@app.get("/runtime/status")
async def runtime_status(
    control_token: str | None = Query(None),
    token: str | None = Query(None),
    x_runtime_control_token: str | None = Header(None, alias="X-Runtime-Control-Token"),
    x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
):
    verify_runtime_read_token(
        settings,
        control_token=control_token,
        control_header=x_runtime_control_token,
        dashboard_token=token,
        dashboard_header=x_dashboard_token,
    )
    return JSONResponse(content={"成功": True, "运行状态": runtime_control.status_payload()})


@app.get("/runtime/events")
async def runtime_events(
    limit: int = 50,
    control_token: str | None = Query(None),
    token: str | None = Query(None),
    x_runtime_control_token: str | None = Header(None, alias="X-Runtime-Control-Token"),
    x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
):
    verify_runtime_read_token(
        settings,
        control_token=control_token,
        control_header=x_runtime_control_token,
        dashboard_token=token,
        dashboard_header=x_dashboard_token,
    )
    rows = runtime_control.list_events(limit=limit)
    return JSONResponse(content={"成功": True, "数量": len(rows), "事件": rows})


@app.post("/runtime/lock")
async def runtime_lock(
    body: LockRequest,
    control_token: str | None = Query(None),
    x_runtime_control_token: str | None = Header(None, alias="X-Runtime-Control-Token"),
):
    verify_runtime_control_write_token(
        settings,
        control_token=control_token,
        header_token=x_runtime_control_token,
    )
    try:
        summary = runtime_control.lock(
            reason=body.reason.strip() or "manual lock",
            locked_until=body.locked_until,
            operator=body.operator,
            actor=body.actor,
            locked_by=body.locked_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return JSONResponse(content={"成功": True, "已锁定": True, "运行状态": summary})


@app.post("/runtime/unlock")
async def runtime_unlock(
    body: UnlockRequest = UnlockRequest(),
    control_token: str | None = Query(None),
    x_runtime_control_token: str | None = Header(None, alias="X-Runtime-Control-Token"),
):
    verify_runtime_control_write_token(
        settings,
        control_token=control_token,
        header_token=x_runtime_control_token,
    )
    summary = runtime_control.unlock(
        operator=body.operator,
        actor=body.actor,
        locked_by=body.locked_by,
    )
    return JSONResponse(content={"成功": True, "已锁定": False, "运行状态": summary})


@app.post("/runtime/unlock-once")
async def runtime_unlock_once(
    body: UnlockOnceRequest,
    control_token: str | None = Query(None),
    x_runtime_control_token: str | None = Header(None, alias="X-Runtime-Control-Token"),
):
    verify_runtime_control_write_token(
        settings,
        control_token=control_token,
        header_token=x_runtime_control_token,
    )
    summary = runtime_control.unlock_once(
        reason=body.reason.strip() or "one-shot unlock",
        operator=body.operator.strip() or "local-admin",
        ttl_seconds=body.ttl_seconds,
    )
    return JSONResponse(
        content={
            "成功": True,
            "one_shot": True,
            "运行状态": runtime_control.status_payload(),
        }
    )

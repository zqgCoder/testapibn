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
from app.journal import TradeJournal
from app.stats import TradeStatsService
from app.dashboard import create_dashboard_router, require_api_token_if_protected
from app.runtime_control import (
    LockRequest,
    RuntimeControl,
    UnlockRequest,
    verify_runtime_control_write_token,
    verify_runtime_read_token,
)
from app.trader import Trader
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
store = SignalStore(settings.sqlite_path)

APP_VERSION = "1.7.0"

app = FastAPI(title="TradingView to Binance Futures Bot", version=APP_VERSION)

app.include_router(create_dashboard_router(settings, journal_store, trade_stats, client, APP_VERSION))


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
    try:
        result = trader.execute(signal, signal_key)
        trade_journal.persist_execution(signal, signal_key, raw_payload, result)
        store.mark_done(signal_key)
    except Exception as exc:
        logger.exception("Signal execution failed: key=%s error=%s", signal_key, exc)
        trade_journal.persist_failure(signal, signal_key, raw_payload, exc, result)
        store.mark_failed(signal_key, str(exc))


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
    result = {"regular": None, "algo": None}
    try:
        result["regular"] = client.cancel_all_open_orders(symbol)
    except Exception as exc:
        result["regular_error"] = str(exc)
    try:
        result["algo"] = client.cancel_all_algo_open_orders(symbol)
    except Exception as exc:
        result["algo_error"] = str(exc)
    return JSONResponse(
        content={
            "成功": True,
            "交易对": symbol,
            "提示": "已尝试取消普通委托和条件单。",
            "结果": to_jsonable(result),
        }
    )


@app.post("/tradingview")
async def tradingview_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        raw_payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是合法 JSON")

    try:
        signal = TradingViewSignal.model_validate(raw_payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if signal.secret != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="Webhook 密钥错误")

    try:
        side = normalize_side(signal.side)
        signal_key = make_signal_key(signal, raw_payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if store.exists(signal_key):
        return JSONResponse(
            status_code=200,
            content={"成功": True, "是否重复信号": True, "信号编号": signal_key, "提示": "该信号已经接收过，已被防重复机制拦截"},
        )

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

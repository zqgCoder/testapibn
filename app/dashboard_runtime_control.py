from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .dashboard import (
    _check_dashboard_access,
    _error_html,
    _guard_runtime_control_dashboard_read,
)
from .runtime_control import (
    LockRequest,
    PositionCleanupRequest,
    PositionCloseRequest,
    UnlockOnceRequest,
    assert_demo_maintenance_allowed,
)

if TYPE_CHECKING:
    from .binance_client import BinanceClient
    from .config import Settings
    from .reconcile import SafetyReconcileService
    from .runtime_control import RuntimeControl
    from .storage import TradeJournalStore
    from .trader import Trader

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE_PATH = _PROJECT_ROOT / "templates" / "dashboard_runtime_control.html"

DEFAULT_SYMBOL = "BTCUSDT"
REFRESH_RUNTIME_SEC = 5
REFRESH_DATA_SEC = 10


def _guard_dashboard_runtime_write(
    settings: Settings,
    *,
    query_token: str | None,
    header_token: str | None,
) -> None:
    _check_dashboard_access(
        settings,
        query_token=query_token,
        header_token=header_token,
    )
    if not settings.runtime_control_enabled:
        raise HTTPException(status_code=404, detail="Runtime Control 未启用")
    if settings.runtime_control_require_token and not settings.runtime_control_token:
        raise HTTPException(
            status_code=403,
            detail="服务端未配置 RUNTIME_CONTROL_TOKEN，Dashboard 无法执行运行控制写操作",
        )


def _clean_symbol(settings: Settings, symbol: str) -> str:
    value = symbol.strip().upper().replace("BINANCE:", "").replace(".P", "")
    if not value:
        raise HTTPException(status_code=422, detail="交易对不能为空")
    if settings.allowed_symbol_set and value not in settings.allowed_symbol_set:
        raise HTTPException(status_code=422, detail=f"交易对 {value} 不在 ALLOWED_SYMBOLS 白名单中")
    return value


def _symbol_positions(client: BinanceClient, symbol: str) -> list[dict[str, Any]]:
    rows = client.position_risk(symbol)
    rows = rows if isinstance(rows, list) else [rows]
    return [row for row in rows if isinstance(row, dict)]


def _symbol_algo_orders(client: BinanceClient, symbol: str) -> list[dict[str, Any]]:
    rows = client.open_algo_orders(symbol)
    rows = rows if isinstance(rows, list) else [rows]
    return [row for row in rows if isinstance(row, dict)]


def _symbol_open_orders(client: BinanceClient, symbol: str) -> list[dict[str, Any]]:
    rows = client.open_orders(symbol)
    rows = rows if isinstance(rows, list) else [rows]
    return [row for row in rows if isinstance(row, dict)]


def render_runtime_control_page_html() -> str:
    if not _TEMPLATE_PATH.is_file():
        raise FileNotFoundError(f"模板不存在: {_TEMPLATE_PATH}")
    html = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        html.replace("__REFRESH_RUNTIME_SEC__", str(REFRESH_RUNTIME_SEC))
        .replace("__REFRESH_DATA_SEC__", str(REFRESH_DATA_SEC))
        .replace("__DEFAULT_SYMBOL__", DEFAULT_SYMBOL)
    )


def register_runtime_control_dashboard_routes(
    router: APIRouter,
    settings: Settings,
    journal_store: TradeJournalStore,
    client: BinanceClient,
    trader: Trader,
    runtime_control: RuntimeControl,
    reconcile_service: SafetyReconcileService | None,
) -> None:
    def guard_write(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ) -> None:
        _guard_dashboard_runtime_write(
            settings,
            query_token=token,
            header_token=x_dashboard_token or request.headers.get("X-Dashboard-Token"),
        )

    @router.get("/runtime-control", include_in_schema=False)
    async def runtime_control_page(
        request: Request,
        token: str | None = Query(None),
    ) -> HTMLResponse:
        if not settings.dashboard_enabled:
            return HTMLResponse(
                content=_error_html("Dashboard 未启用", "请在 .env 中设置 DASHBOARD_ENABLED=true"),
                status_code=404,
            )
        try:
            content = render_runtime_control_page_html()
        except FileNotFoundError as exc:
            return HTMLResponse(content=_error_html("页面模板缺失", str(exc)), status_code=500)
        return HTMLResponse(content=content)

    @router.post("/api/runtime-control/lock")
    async def api_runtime_control_lock(
        body: LockRequest,
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard_write(request, token, x_dashboard_token)
        summary = runtime_control.lock(
            reason=body.reason.strip() or "manual browser lock from dashboard",
            locked_until=body.locked_until,
            operator=body.resolved_operator() or "browser-admin",
        )
        return JSONResponse(
            content={
                "成功": True,
                "已锁定": True,
                "运行状态": runtime_control.status_payload(),
                "摘要": summary,
            }
        )

    @router.post("/api/runtime-control/unlock-once")
    async def api_runtime_control_unlock_once(
        body: UnlockOnceRequest,
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard_write(request, token, x_dashboard_token)
        runtime_control.unlock_once(
            reason=body.reason.strip() or "browser one-shot unlock",
            operator=body.operator.strip() or "browser-admin",
            ttl_seconds=body.ttl_seconds,
        )
        return JSONResponse(
            content={
                "成功": True,
                "one_shot": True,
                "运行状态": runtime_control.status_payload(),
            }
        )

    @router.get("/api/runtime-control/symbol/{symbol}/positions")
    async def api_runtime_control_symbol_positions(
        symbol: str,
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        _guard_runtime_control_dashboard_read(
            settings,
            query_token=token,
            header_token=x_dashboard_token or request.headers.get("X-Dashboard-Token"),
        )
        sym = _clean_symbol(settings, symbol)
        try:
            rows = _symbol_positions(client, sym)
        except Exception as exc:
            return JSONResponse(
                content={"成功": False, "交易对": sym, "错误": str(exc)[:500], "持仓": []},
                status_code=200,
            )
        return JSONResponse(content={"成功": True, "交易对": sym, "持仓": rows})

    @router.get("/api/runtime-control/symbol/{symbol}/algo-orders")
    async def api_runtime_control_symbol_algo_orders(
        symbol: str,
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        _guard_runtime_control_dashboard_read(
            settings,
            query_token=token,
            header_token=x_dashboard_token or request.headers.get("X-Dashboard-Token"),
        )
        sym = _clean_symbol(settings, symbol)
        try:
            rows = _symbol_algo_orders(client, sym)
        except Exception as exc:
            return JSONResponse(
                content={"成功": False, "交易对": sym, "错误": str(exc)[:500], "条件单": []},
                status_code=200,
            )
        return JSONResponse(
            content={"成功": True, "交易对": sym, "条件单数量": len(rows), "条件单": rows}
        )

    @router.get("/api/runtime-control/symbol/{symbol}/open-orders")
    async def api_runtime_control_symbol_open_orders(
        symbol: str,
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        _guard_runtime_control_dashboard_read(
            settings,
            query_token=token,
            header_token=x_dashboard_token or request.headers.get("X-Dashboard-Token"),
        )
        sym = _clean_symbol(settings, symbol)
        try:
            rows = _symbol_open_orders(client, sym)
        except Exception as exc:
            return JSONResponse(
                content={"成功": False, "交易对": sym, "错误": str(exc)[:500], "普通委托": []},
                status_code=200,
            )
        return JSONResponse(
            content={"成功": True, "交易对": sym, "普通委托数量": len(rows), "普通委托": rows}
        )

    @router.post("/api/runtime-control/symbol/{symbol}/close")
    async def api_runtime_control_symbol_close(
        symbol: str,
        body: PositionCloseRequest,
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard_write(request, token, x_dashboard_token)
        assert_demo_maintenance_allowed(settings)
        sym = _clean_symbol(settings, symbol)
        try:
            result = trader.close_position_maintenance(
                sym,
                reason=body.reason.strip() or "browser dashboard close",
                operator=body.operator.strip() or "browser-admin",
                cancel_before_close=body.cancel_before_close,
                cancel_after_close=body.cancel_after_close,
                wait_seconds=body.wait_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        success = bool(result.get("success"))
        status_code = 200 if success else (502 if result.get("status") == "close_order_failed" else 409)
        return JSONResponse(
            status_code=status_code,
            content={
                "成功": success,
                "交易对": sym,
                "状态": result.get("status"),
                "结果": result,
            },
        )

    @router.post("/api/runtime-control/symbol/{symbol}/cleanup")
    async def api_runtime_control_symbol_cleanup(
        symbol: str,
        body: PositionCleanupRequest,
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard_write(request, token, x_dashboard_token)
        assert_demo_maintenance_allowed(settings)
        sym = _clean_symbol(settings, symbol)
        try:
            result = trader.cleanup_symbol_orders(
                sym,
                reason=body.reason.strip() or "browser dashboard cleanup",
                operator=body.operator.strip() or "browser-admin",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        success = bool(result.get("success"))
        return JSONResponse(
            status_code=200 if success else 409,
            content={
                "成功": success,
                "交易对": sym,
                "状态": result.get("status"),
                "结果": result,
            },
        )

    @router.post("/api/runtime-control/reconcile/run")
    async def api_runtime_control_reconcile_run(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard_write(request, token, x_dashboard_token)
        if reconcile_service is None:
            raise HTTPException(status_code=503, detail="安全审计服务未启用")
        report = reconcile_service.run_audit(trigger="dashboard_manual")
        summary = (report or {}).get("summary") or {}
        return JSONResponse(
            content={
                "成功": True,
                "报告": report,
                "summary": {
                    "open_position_count": summary.get("open_position_count"),
                    "unprotected_position_count": summary.get("unprotected_position_count"),
                    "residual_order_symbol_count": summary.get("residual_order_symbol_count"),
                    "error_count": summary.get("error_count"),
                    "warn_count": summary.get("warn_count"),
                },
            }
        )

    @router.get("/api/live-canary/preflight")
    async def api_live_canary_preflight(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        _guard_runtime_control_dashboard_read(
            settings,
            query_token=token,
            header_token=x_dashboard_token or request.headers.get("X-Dashboard-Token"),
        )
        from .live_canary_preflight import build_live_canary_preflight, fetch_canary_market_snapshot

        try:
            market = fetch_canary_market_snapshot(client, DEFAULT_SYMBOL)
        except Exception as exc:
            from .live_canary_preflight import CanaryMarketSnapshot

            market = CanaryMarketSnapshot(symbol=DEFAULT_SYMBOL)
            market_error = str(exc)[:200]
        else:
            market_error = None

        report = reconcile_service.get_latest_report() if reconcile_service is not None else None
        payload = build_live_canary_preflight(
            settings,
            runtime_control,
            market=market,
            reconcile_report=report,
        )
        if market_error:
            payload["btcusdt"]["fetch_error"] = market_error
        return JSONResponse(content={"成功": True, "Preflight": payload})

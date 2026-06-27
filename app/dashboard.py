from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .stats import TradeStatsService

if TYPE_CHECKING:
    from .binance_client import BinanceClient
    from .config import Settings
    from .storage import TradeJournalStore


def verify_dashboard_token(
    settings: Settings,
    *,
    query_token: str | None,
    header_token: str | None,
) -> None:
    if not settings.dashboard_token:
        raise HTTPException(status_code=403, detail="Dashboard Token 未配置")
    provided = header_token or query_token
    if not provided or provided != settings.dashboard_token:
        raise HTTPException(status_code=401, detail="Dashboard Token 无效")


def require_api_token_if_protected(
    settings: Settings,
    *,
    protect: bool,
    query_token: str | None,
    header_token: str | None,
) -> None:
    if not protect:
        return
    verify_dashboard_token(settings, query_token=query_token, header_token=header_token)


def _position_side_from_amt(amt_raw: Any) -> str:
    try:
        amt = Decimal(str(amt_raw))
    except Exception:
        return "FLAT"
    if amt > 0:
        return "LONG"
    if amt < 0:
        return "SHORT"
    return "FLAT"


def build_runtime_config(settings: Settings, app_version: str) -> dict[str, Any]:
    return {
        "app_version": app_version,
        "enable_trading": settings.enable_trading,
        "binance_base_url": settings.binance_base_url,
        "allowed_symbols": sorted(settings.allowed_symbol_set),
        "position_mode": settings.position_mode,
        "account_risk_enabled": settings.account_risk_enabled,
        "dashboard_enabled": settings.dashboard_enabled,
        "dashboard_require_token": settings.dashboard_require_token,
        "default_position_policy": settings.default_position_policy,
        "allow_market_entry": settings.allow_market_entry,
        "allow_limit_entry": settings.allow_limit_entry,
        "default_entry_type": settings.default_entry_type,
        "default_limit_fallback_to_market": settings.default_limit_fallback_to_market,
        "max_auto_leverage": settings.max_auto_leverage,
        "emergency_close_on_protection_fail": settings.emergency_close_on_protection_fail,
    }


def build_dashboard_positions(client: BinanceClient) -> list[dict[str, Any]]:
    rows = client.non_zero_positions()
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "symbol": row.get("symbol"),
                "side": _position_side_from_amt(row.get("positionAmt", "0")),
                "positionAmt": str(row.get("positionAmt", "0")),
                "entryPrice": str(row.get("entryPrice", "")),
                "markPrice": str(row.get("markPrice", "")),
                "unRealizedProfit": str(row.get("unRealizedProfit", row.get("unrealizedProfit", ""))),
                "notional": str(row.get("notional", "")),
                "initialMargin": str(row.get("initialMargin", "")),
                "liquidationPrice": str(row.get("liquidationPrice", "")),
            }
        )
    return result


def build_dashboard_algo_orders(settings: Settings, client: BinanceClient) -> list[dict[str, Any]]:
    symbols = sorted(settings.allowed_symbol_set)
    if not symbols:
        return []
    result: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            rows = client.open_algo_orders(symbol)
        except Exception:
            continue
        rows = rows if isinstance(rows, list) else [rows]
        for row in rows:
            if not isinstance(row, dict):
                continue
            result.append(
                {
                    "symbol": row.get("symbol") or symbol,
                    "orderType": row.get("orderType") or row.get("type"),
                    "side": row.get("side"),
                    "triggerPrice": str(row.get("triggerPrice") or row.get("stopPrice") or ""),
                    "quantity": str(row.get("quantity") or row.get("origQty") or ""),
                    "reduceOnly": row.get("reduceOnly"),
                    "closePosition": row.get("closePosition"),
                    "algoStatus": row.get("algoStatus") or row.get("status"),
                    "workingType": row.get("workingType"),
                    "createTime": row.get("createTime") or row.get("bookTime"),
                }
            )
    return result


def build_dashboard_health(settings: Settings, client: BinanceClient) -> dict[str, Any]:
    from .zh import to_jsonable

    payload: dict[str, Any] = {
        "ok": True,
        "enable_trading": settings.enable_trading,
        "binance_base_url": settings.binance_base_url,
        "allowed_symbols": sorted(settings.allowed_symbol_set),
    }
    try:
        payload["account"] = to_jsonable(client.futures_balance())
    except Exception as exc:
        payload["account_error"] = str(exc)[:500]
    return payload


def _check_dashboard_access(
    settings: Settings,
    *,
    query_token: str | None,
    header_token: str | None,
) -> None:
    if not settings.dashboard_enabled:
        raise HTTPException(status_code=404, detail="Dashboard 未启用")
    if not settings.dashboard_require_token:
        return
    verify_dashboard_token(settings, query_token=query_token, header_token=header_token)


def _error_html(title: str, message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    body {{
      margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
      font-family: system-ui, -apple-system, Segoe UI, sans-serif;
      background: #0f1419; color: #e7ecf3;
    }}
    .box {{
      max-width: 420px; padding: 2rem; border: 1px solid #2a3441; border-radius: 12px;
      background: #151b23; text-align: center;
    }}
    h1 {{ margin: 0 0 0.75rem; font-size: 1.25rem; }}
    p {{ margin: 0; color: #9aa7b8; line-height: 1.5; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>{title}</h1>
    <p>{message}</p>
  </div>
</body>
</html>"""


def render_dashboard_html(auto_refresh_sec: int) -> str:
    refresh = max(0, int(auto_refresh_sec))
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Trade Journal Dashboard</title>
  <style>
    :root {{
      --bg: #0f1419;
      --panel: #151b23;
      --border: #2a3441;
      --text: #e7ecf3;
      --muted: #9aa7b8;
      --accent: #3b82f6;
      --ok: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.45;
    }}
    header {{
      padding: 1rem 1.25rem; border-bottom: 1px solid var(--border);
      display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; justify-content: space-between;
      background: var(--panel); position: sticky; top: 0; z-index: 10;
    }}
    header h1 {{ margin: 0; font-size: 1.1rem; font-weight: 600; }}
    .badge {{ font-size: 0.75rem; color: var(--muted); border: 1px solid var(--border); padding: 0.15rem 0.5rem; border-radius: 999px; }}
    main {{ padding: 1rem 1.25rem 2rem; max-width: 1400px; margin: 0 auto; }}
    .cards {{
      display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 0.75rem; margin-bottom: 1.25rem;
    }}
    .card {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 0.85rem 1rem;
    }}
    .card .label {{ font-size: 0.75rem; color: var(--muted); margin-bottom: 0.25rem; }}
    .card .value {{ font-size: 1.35rem; font-weight: 700; }}
    section {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
      padding: 1rem; margin-bottom: 1rem;
    }}
    section h2 {{ margin: 0 0 0.75rem; font-size: 1rem; }}
    .toolbar {{
      display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: end; margin-bottom: 0.75rem;
    }}
    label {{ display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.75rem; color: var(--muted); }}
    input, select, button {{
      background: #0b1016; color: var(--text); border: 1px solid var(--border);
      border-radius: 8px; padding: 0.45rem 0.6rem; font: inherit;
    }}
    button {{
      cursor: pointer; background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600;
    }}
    button.secondary {{ background: transparent; color: var(--text); border-color: var(--border); }}
    button:disabled {{ opacity: 0.55; cursor: not-allowed; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
    th, td {{ border-bottom: 1px solid var(--border); padding: 0.45rem 0.35rem; text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; white-space: nowrap; }}
    .status {{ display: inline-block; padding: 0.1rem 0.45rem; border-radius: 999px; font-size: 0.72rem; border: 1px solid var(--border); }}
    .status.protected {{ color: var(--ok); border-color: #14532d; }}
    .status.failed, .status.protection_failed {{ color: var(--bad); border-color: #7f1d1d; }}
    .status.blocked_by_account_risk, .status.skipped_by_position_policy {{ color: var(--warn); border-color: #78350f; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.78rem; word-break: break-all; }}
    .error-banner {{
      background: #2a1215; border: 1px solid #7f1d1d; color: #fecaca; padding: 0.65rem 0.85rem;
      border-radius: 8px; margin-bottom: 1rem; display: none;
    }}
    .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }}
    @media (max-width: 900px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
    .detail-backdrop {{
      position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: none; align-items: center; justify-content: center;
      padding: 1rem; z-index: 20;
    }}
    .detail-panel {{
      width: min(960px, 100%); max-height: 90vh; overflow: auto; background: var(--panel);
      border: 1px solid var(--border); border-radius: 12px; padding: 1rem;
    }}
    .detail-panel h3 {{ margin: 0 0 0.75rem; }}
    pre {{
      margin: 0 0 0.75rem; padding: 0.75rem; background: #0b1016; border: 1px solid var(--border);
      border-radius: 8px; overflow: auto; font-size: 0.75rem; white-space: pre-wrap; word-break: break-word;
    }}
    .detail-meta {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 0.5rem; margin-bottom: 0.75rem; }}
    .detail-meta div {{ font-size: 0.82rem; }}
    .detail-meta span {{ color: var(--muted); display: block; font-size: 0.72rem; }}
    .empty {{ color: var(--muted); font-size: 0.85rem; padding: 0.5rem 0; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Trade Journal Dashboard</h1>
      <div class="badge">只读 · 无下单能力</div>
    </div>
    <div class="badge" id="refreshHint">自动刷新: {refresh}s</div>
  </header>
  <main>
    <div id="errorBanner" class="error-banner"></div>

    <div class="cards" id="summaryCards">
      <div class="card"><div class="label">总执行数</div><div class="value" data-k="total_executions">-</div></div>
      <div class="card"><div class="label">保护成功</div><div class="value" data-k="protected_count">-</div></div>
      <div class="card"><div class="label">账户风控拒绝</div><div class="value" data-k="blocked_by_account_risk_count">-</div></div>
      <div class="card"><div class="label">持仓策略跳过</div><div class="value" data-k="skipped_by_position_policy_count">-</div></div>
      <div class="card"><div class="label">保护单失败</div><div class="value" data-k="protection_failed_count">-</div></div>
      <div class="card"><div class="label">执行异常</div><div class="value" data-k="failed_count">-</div></div>
      <div class="card"><div class="label">成功率</div><div class="value" data-k="success_rate">-</div></div>
      <div class="card"><div class="label">今日执行</div><div class="value" data-k="today_executions">-</div></div>
      <div class="card"><div class="label">今日保护成功</div><div class="value" data-k="today_protected">-</div></div>
    </div>

    <section>
      <h2>运行状态</h2>
      <div class="detail-meta" id="runtimeMeta"></div>
      <div class="detail-meta" id="healthMeta" style="margin-top:0.5rem"></div>
    </section>

    <section>
      <h2>当前持仓</h2>
      <div style="overflow-x:auto" id="positionsWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section>
      <h2>当前条件单</h2>
      <div style="overflow-x:auto" id="algoOrdersWrap">
        <div class="empty">加载中...</div>
      </div>
    </section>

    <section>
      <h2>最近执行记录</h2>
      <div class="toolbar">
        <label>交易对<input id="filterSymbol" placeholder="BTCUSDT" /></label>
        <label>状态
          <select id="filterStatus">
            <option value="">全部</option>
            <option value="protected">protected</option>
            <option value="entry_not_filled">entry_not_filled</option>
            <option value="blocked_by_account_risk">blocked_by_account_risk</option>
            <option value="skipped_by_position_policy">skipped_by_position_policy</option>
            <option value="protection_failed">protection_failed</option>
            <option value="failed">failed</option>
          </select>
        </label>
        <label>条数<input id="filterLimit" type="number" min="1" max="500" value="50" /></label>
        <button id="refreshBtn" type="button">刷新</button>
      </div>
      <div style="overflow-x:auto">
        <table>
          <thead>
            <tr>
              <th>编号</th><th>创建时间</th><th>交易对</th><th>方向</th><th>状态</th><th>状态说明</th>
              <th>跳过原因</th><th>计划数量</th><th>成交数量</th><th>进场价</th><th>杠杆</th><th>信号编号</th><th></th>
            </tr>
          </thead>
          <tbody id="executionsBody"><tr><td colspan="13" class="empty">加载中...</td></tr></tbody>
        </table>
      </div>
    </section>

    <div class="grid-2">
      <section>
        <h2>按交易对统计</h2>
        <div style="overflow-x:auto">
          <table>
            <thead>
              <tr><th>交易对</th><th>总数</th><th>保护成功</th><th>未成交</th><th>风控拒绝</th><th>保护失败</th></tr>
            </thead>
            <tbody id="bySymbolBody"><tr><td colspan="6" class="empty">加载中...</td></tr></tbody>
          </table>
        </div>
      </section>
      <section>
        <h2>拒绝原因统计</h2>
        <div style="overflow-x:auto">
          <table>
            <thead><tr><th>原因</th><th>状态</th><th>次数</th></tr></thead>
            <tbody id="rejectionsBody"><tr><td colspan="3" class="empty">加载中...</td></tr></tbody>
          </table>
        </div>
      </section>
    </div>
  </main>

  <div id="detailBackdrop" class="detail-backdrop">
    <div class="detail-panel">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:0.5rem;">
        <h3 id="detailTitle">执行详情</h3>
        <button class="secondary" id="closeDetailBtn" type="button">关闭</button>
      </div>
      <div class="detail-meta" id="detailMeta"></div>
      <h4 style="margin:0.75rem 0 0.35rem;font-size:0.85rem;color:var(--muted)">原始信号</h4>
      <pre id="detailRawSignal"></pre>
      <h4 style="margin:0.75rem 0 0.35rem;font-size:0.85rem;color:var(--muted)">交易计划</h4>
      <pre id="detailPlan"></pre>
      <h4 style="margin:0.75rem 0 0.35rem;font-size:0.85rem;color:var(--muted)">账户风控</h4>
      <pre id="detailAccountRisk"></pre>
      <h4 style="margin:0.75rem 0 0.35rem;font-size:0.85rem;color:var(--muted)">进场摘要</h4>
      <pre id="detailEntrySummary"></pre>
      <h4 style="margin:0.75rem 0 0.35rem;font-size:0.85rem;color:var(--muted)">保护单摘要</h4>
      <pre id="detailProtectionSummary"></pre>
      <h4 style="margin:0.75rem 0 0.35rem;font-size:0.85rem;color:var(--muted)">执行结果</h4>
      <pre id="detailResult"></pre>
      <h4 style="margin:0.75rem 0 0.35rem;font-size:0.85rem;color:var(--muted)">关联订单</h4>
      <pre id="detailOrders"></pre>
    </div>
  </div>

  <script>
    const AUTO_REFRESH_SEC = {refresh};
    let dashboardToken = null;
    let refreshTimer = null;

    function readTokenFromUrl() {{
      const params = new URLSearchParams(window.location.search);
      return params.get("token");
    }}

    function showError(message) {{
      const el = document.getElementById("errorBanner");
      el.textContent = message;
      el.style.display = "block";
    }}

    function clearError() {{
      const el = document.getElementById("errorBanner");
      el.textContent = "";
      el.style.display = "none";
    }}

    async function apiFetch(path) {{
      const headers = {{ Accept: "application/json" }};
      if (dashboardToken) headers["X-Dashboard-Token"] = dashboardToken;
      const resp = await fetch(path, {{ headers }});
      let data = null;
      try {{ data = await resp.json(); }} catch (_) {{ data = null; }}
      if (!resp.ok) {{
        const detail = (data && (data.detail || data.错误)) || resp.statusText || "请求失败";
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }}
      return data;
    }}

    function esc(text) {{
      if (text === null || text === undefined) return "";
      return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}

    function fmtJson(value) {{
      if (value === null || value === undefined) return "null";
      if (typeof value === "string") {{
        try {{ return JSON.stringify(JSON.parse(value), null, 2); }} catch (_) {{ return value; }}
      }}
      return JSON.stringify(value, null, 2);
    }}

    function renderSummary(stats) {{
      document.querySelectorAll("#summaryCards .value").forEach((el) => {{
        const key = el.getAttribute("data-k");
        let val = stats[key];
        if (key === "success_rate" && val !== undefined) val = (Number(val) * 100).toFixed(2) + "%";
        el.textContent = val ?? "-";
      }});
    }}

    const RUNTIME_LABELS = {{
      app_version: "应用版本",
      enable_trading: "允许真实下单",
      binance_base_url: "币安接口",
      allowed_symbols: "允许交易对",
      position_mode: "持仓模式",
      account_risk_enabled: "账户风控",
      dashboard_enabled: "Dashboard 启用",
      dashboard_require_token: "Dashboard 需 Token",
      default_position_policy: "默认持仓策略",
      allow_market_entry: "允许市价进场",
      allow_limit_entry: "允许限价进场",
      default_entry_type: "默认进场方式",
      default_limit_fallback_to_market: "限价超时改市价",
      max_auto_leverage: "最大自动杠杆",
      emergency_close_on_protection_fail: "保护失败紧急平仓",
    }};

    const HEALTH_LABELS = {{
      ok: "服务正常",
      enable_trading: "允许真实下单",
      binance_base_url: "币安接口",
      allowed_symbols: "允许交易对",
      account_error: "账户查询错误",
    }};

    function renderKeyValueGrid(containerId, data, labels) {{
      const el = document.getElementById(containerId);
      if (!data) {{
        el.innerHTML = '<div class="empty">暂无数据</div>';
        return;
      }}
      el.innerHTML = Object.entries(data).map(([key, val]) => {{
        if (key === "account") return "";
        const label = labels[key] || key;
        let display = val;
        if (Array.isArray(val)) display = val.join(", ");
        if (typeof val === "boolean") display = val ? "是" : "否";
        if (val === null || val === undefined) display = "-";
        return `<div><span>${{esc(label)}}</span>${{esc(display)}}</div>`;
      }}).join("");
    }}

    function renderPositions(rows) {{
      const wrap = document.getElementById("positionsWrap");
      if (!rows || rows.length === 0) {{
        wrap.innerHTML = '<div class="empty">当前无持仓</div>';
        return;
      }}
      wrap.innerHTML = `<table>
        <thead><tr>
          <th>交易对</th><th>方向</th><th>数量</th><th>开仓价</th><th>标记价</th>
          <th>未实现盈亏</th><th>名义价值</th><th>初始保证金</th><th>强平价</th>
        </tr></thead>
        <tbody>${{rows.map((row) => `<tr>
          <td>${{esc(row.symbol)}}</td>
          <td>${{esc(row.side)}}</td>
          <td>${{esc(row.positionAmt)}}</td>
          <td>${{esc(row.entryPrice)}}</td>
          <td>${{esc(row.markPrice)}}</td>
          <td>${{esc(row.unRealizedProfit)}}</td>
          <td>${{esc(row.notional)}}</td>
          <td>${{esc(row.initialMargin)}}</td>
          <td>${{esc(row.liquidationPrice)}}</td>
        </tr>`).join("")}}</tbody>
      </table>`;
    }}

    function renderAlgoOrders(rows) {{
      const wrap = document.getElementById("algoOrdersWrap");
      if (!rows || rows.length === 0) {{
        wrap.innerHTML = '<div class="empty">当前无条件单</div>';
        return;
      }}
      wrap.innerHTML = `<table>
        <thead><tr>
          <th>交易对</th><th>类型</th><th>方向</th><th>触发价</th><th>数量</th>
          <th>只减仓</th><th>全平</th><th>状态</th><th>触发类型</th><th>创建时间</th>
        </tr></thead>
        <tbody>${{rows.map((row) => `<tr>
          <td>${{esc(row.symbol)}}</td>
          <td>${{esc(row.orderType)}}</td>
          <td>${{esc(row.side)}}</td>
          <td>${{esc(row.triggerPrice)}}</td>
          <td>${{esc(row.quantity)}}</td>
          <td>${{esc(row.reduceOnly)}}</td>
          <td>${{esc(row.closePosition)}}</td>
          <td>${{esc(row.algoStatus)}}</td>
          <td>${{esc(row.workingType)}}</td>
          <td class="mono">${{esc(row.createTime)}}</td>
        </tr>`).join("")}}</tbody>
      </table>`;
    }}

    function renderExecutions(rows) {{
      const body = document.getElementById("executionsBody");
      if (!rows || rows.length === 0) {{
        body.innerHTML = '<tr><td colspan="13" class="empty">暂无记录</td></tr>';
        return;
      }}
      body.innerHTML = rows.map((row) => {{
        const status = row["状态"] || "";
        return `<tr>
          <td>${{esc(row["编号"])}}</td>
          <td class="mono">${{esc(row["创建时间"])}}</td>
          <td>${{esc(row["交易对"])}}</td>
          <td>${{esc(row["方向"])}}</td>
          <td><span class="status ${{esc(status)}}">${{esc(status)}}</span></td>
          <td>${{esc(row["状态说明"])}}</td>
          <td class="mono">${{esc(row["跳过原因"])}}</td>
          <td>${{esc(row["计划数量"])}}</td>
          <td>${{esc(row["成交数量"])}}</td>
          <td>${{esc(row["进场价"])}}</td>
          <td>${{esc(row["杠杆"])}}</td>
          <td class="mono">${{esc(row["信号编号"])}}</td>
          <td><button class="secondary" type="button" data-id="${{esc(row["编号"])}}">详情</button></td>
        </tr>`;
      }}).join("");
      body.querySelectorAll("button[data-id]").forEach((btn) => {{
        btn.addEventListener("click", () => openDetail(btn.getAttribute("data-id")));
      }});
    }}

    function renderBySymbol(rows) {{
      const body = document.getElementById("bySymbolBody");
      if (!rows || rows.length === 0) {{
        body.innerHTML = '<tr><td colspan="6" class="empty">暂无数据</td></tr>';
        return;
      }}
      body.innerHTML = rows.map((row) => `<tr>
        <td>${{esc(row.symbol)}}</td>
        <td>${{esc(row.total_executions)}}</td>
        <td>${{esc(row.protected_count)}}</td>
        <td>${{esc(row.entry_not_filled_count)}}</td>
        <td>${{esc(row.blocked_count)}}</td>
        <td>${{esc(row.protection_failed_count)}}</td>
      </tr>`).join("");
    }}

    function renderRejections(rows) {{
      const body = document.getElementById("rejectionsBody");
      if (!rows || rows.length === 0) {{
        body.innerHTML = '<tr><td colspan="3" class="empty">暂无数据</td></tr>';
        return;
      }}
      body.innerHTML = rows.map((row) => `<tr>
        <td class="mono">${{esc(row.reason)}}</td>
        <td>${{esc(row.status)}}</td>
        <td>${{esc(row.count)}}</td>
      </tr>`).join("");
    }}

    async function loadAll() {{
      clearError();
      const symbol = document.getElementById("filterSymbol").value.trim();
      const status = document.getElementById("filterStatus").value;
      const limit = document.getElementById("filterLimit").value || "50";
      const params = new URLSearchParams();
      params.set("limit", limit);
      if (symbol) params.set("symbol", symbol);
      if (status) params.set("status", status);

      try {{
        const [summaryResp, execResp, bySymbolResp, rejectResp, runtimeResp, healthResp, posResp, algoResp] = await Promise.all([
          apiFetch("/dashboard/api/summary"),
          apiFetch("/dashboard/api/executions?" + params.toString()),
          apiFetch("/dashboard/api/by-symbol"),
          apiFetch("/dashboard/api/rejections"),
          apiFetch("/dashboard/api/runtime"),
          apiFetch("/dashboard/api/health"),
          apiFetch("/dashboard/api/positions"),
          apiFetch("/dashboard/api/algo-orders"),
        ]);
        renderSummary(summaryResp["统计"] || {{}});
        renderKeyValueGrid("runtimeMeta", runtimeResp["运行配置"] || {{}}, RUNTIME_LABELS);
        renderKeyValueGrid("healthMeta", healthResp["健康"] || {{}}, HEALTH_LABELS);
        renderPositions(posResp["持仓"] || []);
        renderAlgoOrders(algoResp["条件单"] || []);
        renderExecutions(execResp["记录"] || []);
        renderBySymbol(bySymbolResp["按交易对"] || []);
        renderRejections(rejectResp["拒绝统计"] || []);
      }} catch (err) {{
        showError(err.message || String(err));
      }}
    }}

    async function openDetail(id) {{
      try {{
        const [detailResp, ordersResp] = await Promise.all([
          apiFetch("/dashboard/api/executions/" + id),
          apiFetch("/dashboard/api/orders/" + id),
        ]);
        const record = detailResp["记录"] || {{}};
        document.getElementById("detailTitle").textContent = "执行详情 #" + id;
        document.getElementById("detailMeta").innerHTML = [
          ["交易对", record["交易对"]],
          ["方向", record["方向"]],
          ["状态", record["状态"]],
          ["状态说明", record["状态说明"]],
          ["跳过原因", record["跳过原因"]],
          ["计划数量", record["计划数量"]],
          ["成交数量", record["成交数量"]],
          ["进场价", record["进场价"]],
          ["杠杆", record["杠杆"]],
          ["信号编号", record["信号编号"]],
          ["创建时间", record["创建时间"]],
        ].map(([k, v]) => `<div><span>${{esc(k)}}</span>${{esc(v)}}</div>`).join("");
        document.getElementById("detailRawSignal").textContent = fmtJson(record["原始信号"]);
        document.getElementById("detailPlan").textContent = fmtJson(record["交易计划"]);
        document.getElementById("detailAccountRisk").textContent = fmtJson(record["账户风控"]);
        document.getElementById("detailEntrySummary").textContent = fmtJson(record["进场摘要"]);
        document.getElementById("detailProtectionSummary").textContent = fmtJson(record["保护单摘要"]);
        document.getElementById("detailResult").textContent = fmtJson(record["执行结果"]);
        document.getElementById("detailOrders").textContent = fmtJson(ordersResp["订单"] || []);
        document.getElementById("detailBackdrop").style.display = "flex";
      }} catch (err) {{
        showError(err.message || String(err));
      }}
    }}

    function closeDetail() {{
      document.getElementById("detailBackdrop").style.display = "none";
    }}

    function setupAutoRefresh() {{
      if (refreshTimer) clearInterval(refreshTimer);
      if (AUTO_REFRESH_SEC > 0) {{
        refreshTimer = setInterval(loadAll, AUTO_REFRESH_SEC * 1000);
      }} else {{
        document.getElementById("refreshHint").textContent = "自动刷新: 关闭";
      }}
    }}

    document.getElementById("refreshBtn").addEventListener("click", loadAll);
    document.getElementById("closeDetailBtn").addEventListener("click", closeDetail);
    document.getElementById("detailBackdrop").addEventListener("click", (ev) => {{
      if (ev.target.id === "detailBackdrop") closeDetail();
    }});

    dashboardToken = readTokenFromUrl();
    loadAll();
    setupAutoRefresh();
  </script>
</body>
</html>"""


def create_dashboard_router(
    settings: Settings,
    journal_store: TradeJournalStore,
    trade_stats: TradeStatsService,
    client: BinanceClient,
    app_version: str,
) -> APIRouter:
    router = APIRouter(prefix="/dashboard", tags=["dashboard"])

    def guard(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ) -> None:
        _check_dashboard_access(
            settings,
            query_token=token,
            header_token=x_dashboard_token,
        )

    @router.get("", include_in_schema=False)
    @router.get("/", include_in_schema=False)
    async def dashboard_page(
        request: Request,
        token: str | None = Query(None),
    ) -> HTMLResponse:
        if not settings.dashboard_enabled:
            return HTMLResponse(
                content=_error_html("Dashboard 未启用", "请在 .env 中设置 DASHBOARD_ENABLED=true"),
                status_code=404,
            )
        try:
            _check_dashboard_access(
                settings,
                query_token=token,
                header_token=request.headers.get("X-Dashboard-Token"),
            )
        except HTTPException as exc:
            if exc.status_code == 401:
                return HTMLResponse(
                    content=_error_html(
                        "访问被拒绝",
                        "请通过 /dashboard?token=你的密钥 访问，Token 需与 DASHBOARD_TOKEN 一致。",
                    ),
                    status_code=401,
                )
            if exc.status_code == 403:
                return HTMLResponse(
                    content=_error_html("Dashboard 未配置", "服务端尚未设置 DASHBOARD_TOKEN。"),
                    status_code=403,
                )
            raise
        return HTMLResponse(content=render_dashboard_html(settings.dashboard_auto_refresh_sec))

    @router.get("/api/summary")
    async def api_summary(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        return JSONResponse(content={"成功": True, "统计": trade_stats.summary()})

    @router.get("/api/by-symbol")
    async def api_by_symbol(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        rows = trade_stats.by_symbol()
        return JSONResponse(content={"成功": True, "数量": len(rows), "按交易对": rows})

    @router.get("/api/rejections")
    async def api_rejections(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
        limit: int = 20,
    ):
        guard(request, token, x_dashboard_token)
        rows = trade_stats.rejections(limit=limit)
        return JSONResponse(content={"成功": True, "数量": len(rows), "拒绝统计": rows})

    @router.get("/api/executions")
    async def api_executions(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
        limit: int = 50,
        symbol: str | None = None,
        status: str | None = None,
    ):
        guard(request, token, x_dashboard_token)
        rows = journal_store.list_executions(limit=limit, symbol=symbol, status=status)
        return JSONResponse(
            content={
                "成功": True,
                "数量": len(rows),
                "记录": [TradeStatsService.execution_brief(row) for row in rows],
            }
        )

    @router.get("/api/executions/{execution_id}")
    async def api_execution_detail(
        execution_id: int,
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        row = journal_store.get_execution(execution_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"执行记录不存在: {execution_id}")
        return JSONResponse(
            content={"成功": True, "记录": TradeStatsService.execution_detail(row)}
        )

    @router.get("/api/orders/{execution_id}")
    async def api_orders(
        execution_id: int,
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
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

    @router.get("/api/runtime")
    async def api_runtime(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        return JSONResponse(
            content={"成功": True, "运行配置": build_runtime_config(settings, app_version)}
        )

    @router.get("/api/positions")
    async def api_positions(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        try:
            rows = build_dashboard_positions(client)
        except Exception as exc:
            return JSONResponse(
                content={"成功": False, "错误": str(exc)[:500], "持仓": []},
                status_code=200,
            )
        return JSONResponse(content={"成功": True, "数量": len(rows), "持仓": rows})

    @router.get("/api/algo-orders")
    async def api_algo_orders(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        try:
            rows = build_dashboard_algo_orders(settings, client)
        except Exception as exc:
            return JSONResponse(
                content={"成功": False, "错误": str(exc)[:500], "条件单": []},
                status_code=200,
            )
        return JSONResponse(content={"成功": True, "数量": len(rows), "条件单": rows})

    @router.get("/api/health")
    async def api_health(
        request: Request,
        token: str | None = Query(None),
        x_dashboard_token: str | None = Header(None, alias="X-Dashboard-Token"),
    ):
        guard(request, token, x_dashboard_token)
        return JSONResponse(content={"成功": True, "健康": build_dashboard_health(settings, client)})

    return router

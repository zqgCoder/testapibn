(function () {
  "use strict";

  const TOKEN_MISSING_MESSAGE =
    "请通过 /dashboard/runtime-control?token=你的密钥 访问。";

  const body = document.body;
  const SYMBOL = body.dataset.symbol || "BTCUSDT";
  const REFRESH_RUNTIME_MS = (parseInt(body.dataset.refreshRuntime, 10) || 5) * 1000;
  const REFRESH_DATA_MS = (parseInt(body.dataset.refreshData, 10) || 10) * 1000;

  let dashboardToken = "";
  let activeTab = "positions";
  let symbolCache = { positions: null, algo: null, open: null };
  let runtimeTimer = null;
  let dataTimer = null;

  function readTokenFromUrl() {
    const params = new URLSearchParams(window.location.search);
    return (params.get("token") || "").trim();
  }

  function withTokenQuery(path) {
    if (!dashboardToken) {
      return path;
    }
    const url = new URL(path, window.location.origin);
    url.searchParams.set("token", dashboardToken);
    return url.pathname + url.search;
  }

  function esc(text) {
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showAuthGate() {
    const gate = document.getElementById("authGate");
    const shell = document.getElementById("appShell");
    if (gate) gate.classList.remove("hidden");
    if (shell) shell.classList.add("hidden");
    showError("");
  }

  function hideAuthGate() {
    const gate = document.getElementById("authGate");
    const shell = document.getElementById("appShell");
    if (gate) gate.classList.add("hidden");
    if (shell) shell.classList.remove("hidden");
  }

  function showError(message) {
    const el = document.getElementById("errorBanner");
    if (!el) return;
    if (!message) {
      el.classList.add("hidden");
      el.textContent = "";
      return;
    }
    el.textContent = message;
    el.classList.remove("hidden");
  }

  function showToast(message) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.classList.remove("hidden");
    window.clearTimeout(showToast._timer);
    showToast._timer = window.setTimeout(() => el.classList.add("hidden"), 4000);
  }

  function renderLiveGuardBanner(data) {
    const el = document.getElementById("liveGuardBanner");
    if (!el) return;
    if (!data || !data.guard_active) {
      el.classList.add("hidden");
      el.innerHTML = "";
      return;
    }
    const danger = data.is_live && (!data.live_trading_enabled || !data.live_confirm_phrase_valid);
    el.classList.remove("hidden");
    el.classList.toggle("live-danger", danger);
    const blocking = (data.would_allow_execution && data.would_allow_execution.blocking_reasons) || [];
    el.innerHTML = `
      <h3>⚠ Binance 实盘环境 · Live Canary Guard</h3>
      <p>当前 endpoint 为 <strong>live/mainnet</strong>。本版默认不自动放行实盘交易。</p>
      <div class="live-guard-grid">
        <div>Binance 环境: ${esc(data.binance_env)}</div>
        <div>is_live: ${esc(data.is_live)}</div>
        <div>LIVE_TRADING_ENABLED: ${esc(data.live_trading_enabled)}</div>
        <div>LIVE_CANARY_MODE: ${esc(data.live_canary_mode)}</div>
        <div>confirm phrase valid: ${esc(data.live_confirm_phrase_valid)}</div>
        <div>allowed symbols: ${esc((data.live_allowed_symbols || []).join(", "))}</div>
        <div>max risk USDT: ${esc(data.live_max_risk_usdt)}</div>
        <div>max margin USDT: ${esc(data.live_max_margin_usdt)}</div>
        <div>max notional USDT: ${esc(data.live_max_position_notional_usdt)}</div>
        <div>reject TradingView: ${esc(data.live_reject_tradingview_by_default)}</div>
        <div>one-shot active: ${esc(data.one_shot_active)}</div>
      </div>
      ${
        blocking.length
          ? `<p style="margin-top:0.65rem">当前阻塞原因: ${esc(blocking.join(", "))}</p>`
          : ""
      }
    `;
  }

  async function loadLiveGuardStatus() {
    const data = await apiFetch("/dashboard/api/live-guard/status");
    renderLiveGuardBanner((data && data["Live Guard"]) || null);
  }

  async function apiFetch(path, options) {
    if (!dashboardToken) {
      throw new Error(TOKEN_MISSING_MESSAGE);
    }
    const opts = options || {};
    const requestUrl = withTokenQuery(path);
    const headers = Object.assign(
      { Accept: "application/json", "X-Dashboard-Token": dashboardToken },
      opts.headers || {}
    );
    if (opts.body && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    const resp = await fetch(requestUrl, Object.assign({}, opts, { headers }));
    let data = null;
    try {
      data = await resp.json();
    } catch (_e) {
      data = null;
    }
    if (!resp.ok) {
      const detail = (data && (data.detail || data.错误 || data.提示)) || resp.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    if (data && data.成功 === false && data.错误) {
      throw new Error(data.错误);
    }
    return data;
  }

  function fmt(value) {
    if (value === null || value === undefined || value === "") return "—";
    return String(value);
  }

  function renderKvGrid(containerId, entries) {
    const el = document.getElementById(containerId);
    if (!el) return;
    el.innerHTML = entries
      .map(
        ([label, value]) =>
          `<div class="kv"><label>${esc(label)}</label><span>${esc(fmt(value))}</span></div>`
      )
      .join("");
  }

  function setLockBadge(state) {
    const badge = document.getElementById("runtimeLockBadge");
    if (!badge) return;
    const locked = !!(state && state.effective_locked);
    badge.textContent = locked ? "LOCKED" : "UNLOCKED";
    badge.className = "badge " + (locked ? "bad" : "ok");
  }

  async function loadRuntimeStatus() {
    const data = await apiFetch("/dashboard/api/runtime-control/status");
    const state = (data && data.运行控制) || {};
    const one = state.one_shot || {};
    setLockBadge(state);
    renderKvGrid("runtimeStatusGrid", buildRuntimeEntries(state));
    return state;
  }

  function renderSymbolView() {
    const view = document.getElementById("symbolDataView");
    if (!view) return;
    let payload = null;
    if (activeTab === "positions") payload = symbolCache.positions;
    if (activeTab === "algo") payload = symbolCache.algo;
    if (activeTab === "open") payload = symbolCache.open;
    view.textContent = payload ? JSON.stringify(payload, null, 2) : "无数据";
  }

  async function loadSymbolData() {
    const [pos, algo, open] = await Promise.all([
      apiFetch(`/dashboard/api/runtime-control/symbol/${SYMBOL}/positions`),
      apiFetch(`/dashboard/api/runtime-control/symbol/${SYMBOL}/algo-orders`),
      apiFetch(`/dashboard/api/runtime-control/symbol/${SYMBOL}/open-orders`),
    ]);
    symbolCache.positions = pos;
    symbolCache.algo = algo;
    symbolCache.open = open;
    renderSymbolView();
  }

  async function loadJournal() {
    const data = await apiFetch("/dashboard/api/executions?limit=5");
    const rows = (data && data.记录) || [];
    const tbody = document.getElementById("journalBody");
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="empty">暂无记录</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .map((row) => {
        const qty = row.成交数量 != null ? row.成交数量 : row.计划数量;
        return `<tr>
          <td>${esc(row.编号)}</td>
          <td>${esc(row.信号ID || row.信号编号)}</td>
          <td>${esc(row.交易对)}</td>
          <td>${esc(row.方向)}</td>
          <td>${esc(row.状态)}</td>
          <td>${esc(row.跳过原因)}</td>
          <td>${esc(qty)}</td>
          <td>${esc(row.进场价)}</td>
          <td>${esc(row.杠杆)}</td>
          <td>${esc(row.创建时间)}</td>
        </tr>`;
      })
      .join("");
  }

  async function loadMarketData() {
    await Promise.all([loadSymbolData(), loadJournal()]);
  }

  async function loadAll() {
    showError("");
    try {
      await Promise.all([loadLiveGuardStatus(), loadRuntimeStatus()]);
      await loadMarketData();
    } catch (err) {
      showError(err.message || String(err));
    }
  }

  function confirmTwice(message, dangerMessage) {
    if (!window.confirm(message)) return false;
    return window.confirm(dangerMessage);
  }

  async function postLock() {
    if (
      !confirmTwice(
        "确认锁定 Runtime？锁定后新的 TradingView 信号将被拦截。",
        "再次确认：立即锁定 Runtime。"
      )
    ) {
      return;
    }
    const data = await apiFetch("/dashboard/api/runtime-control/lock", {
      method: "POST",
      body: JSON.stringify({
        reason: "manual browser lock from dashboard",
        operator: "browser-admin",
      }),
    });
    showToast("Runtime 已锁定");
    if (data && data.运行状态) setLockBadge(data.运行状态);
    await loadRuntimeStatus();
  }

  async function postUnlockOnce() {
    const ttlInput = document.getElementById("oneShotTtl");
    const reasonInput = document.getElementById("oneShotReason");
    let ttl = parseInt((ttlInput && ttlInput.value) || "300", 10);
    if (Number.isNaN(ttl)) ttl = 300;
    ttl = Math.min(3600, Math.max(30, ttl));
    if (ttlInput) ttlInput.value = String(ttl);
    const reason = ((reasonInput && reasonInput.value) || "").trim() || "browser one-shot unlock";
    if (
      !confirmTwice(
        `确认 One-Shot 放行？仅下一条 TV 信号可执行，TTL=${ttl}s。`,
        "再次确认：启用 One-Shot Unlock。"
      )
    ) {
      return;
    }
    const data = await apiFetch("/dashboard/api/runtime-control/unlock-once", {
      method: "POST",
      body: JSON.stringify({
        ttl_seconds: ttl,
        reason: reason,
        operator: "browser-admin",
      }),
    });
    showToast("One-Shot 已启用");
    if (data && data.运行状态) {
      setLockBadge(data.运行状态);
      renderKvGrid("runtimeStatusGrid", buildRuntimeEntries(data.运行状态));
    }
    await loadRuntimeStatus();
  }

  function buildRuntimeEntries(state) {
    const one = state.one_shot || {};
    return [
      ["enabled", state.enabled],
      ["locked", state.locked],
      ["effective_locked", state.effective_locked],
      ["reason", state.reason],
      ["locked_by", state.locked_by],
      ["locked_at", state.locked_at],
      ["one_shot.enabled", one.enabled],
      ["one_shot.remaining", one.remaining],
      ["one_shot.reason", one.reason],
      ["one_shot.operator", one.operator],
      ["one_shot.started_at", one.started_at],
      ["one_shot.expires_at", one.expires_at],
      ["one_shot.consumed_by_signal_id", one.consumed_by_signal_id],
      ["one_shot.consumed_at", one.consumed_at],
      ["updated_at", state.updated_at],
    ];
  }

  async function postClose() {
    if (
      !confirmTwice(
        `⚠ 危险操作：将市价平仓 ${SYMBOL} 并清理保护单！`,
        "最终确认：我了解此操作不可撤销，继续平仓。"
      )
    ) {
      return;
    }
    const data = await apiFetch(`/dashboard/api/runtime-control/symbol/${SYMBOL}/close`, {
      method: "POST",
      body: JSON.stringify({
        reason: "browser dashboard close",
        operator: "browser-admin",
        cancel_before_close: true,
        cancel_after_close: true,
        wait_seconds: 10,
      }),
    });
    showToast(data.成功 ? "平仓请求已完成" : "平仓未完成，请查看结果");
    await loadMarketData();
  }

  async function postCleanup() {
    if (
      !confirmTwice(
        `确认清理 ${SYMBOL} 的普通委托与条件单？`,
        "再次确认：清理所有挂单。"
      )
    ) {
      return;
    }
    const data = await apiFetch(`/dashboard/api/runtime-control/symbol/${SYMBOL}/cleanup`, {
      method: "POST",
      body: JSON.stringify({
        reason: "browser dashboard cleanup",
        operator: "browser-admin",
      }),
    });
    showToast(data.成功 ? "清理完成" : "清理未完成");
    await loadMarketData();
  }

  async function postReconcile() {
    const data = await apiFetch("/dashboard/api/runtime-control/reconcile/run", {
      method: "POST",
      body: "{}",
    });
    const summary = (data && data.summary) || {};
    renderKvGrid("reconcileSummary", [
      ["open_position_count", summary.open_position_count],
      ["unprotected_position_count", summary.unprotected_position_count],
      ["residual_order_symbol_count", summary.residual_order_symbol_count],
      ["error_count", summary.error_count],
      ["warn_count", summary.warn_count],
    ]);
    showToast("Reconcile 已完成");
    await loadMarketData();
  }

  function bindTabs() {
    document.querySelectorAll(".tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        activeTab = btn.dataset.tab || "positions";
        renderSymbolView();
      });
    });
  }

  function startAutoRefresh() {
    if (runtimeTimer) window.clearInterval(runtimeTimer);
    if (dataTimer) window.clearInterval(dataTimer);
    runtimeTimer = window.setInterval(() => {
      loadRuntimeStatus().catch((e) => showError(e.message));
    }, REFRESH_RUNTIME_MS);
    dataTimer = window.setInterval(() => {
      loadLiveGuardStatus().catch((e) => showError(e.message));
      loadMarketData().catch((e) => showError(e.message));
    }, REFRESH_DATA_MS);
  }

  function init() {
    dashboardToken = readTokenFromUrl();

    const mainLink = document.getElementById("linkMainDashboard");
    if (mainLink) {
      mainLink.href = dashboardToken
        ? `/dashboard?token=${encodeURIComponent(dashboardToken)}`
        : "/dashboard";
    }

    if (!dashboardToken) {
      showAuthGate();
      return;
    }

    hideAuthGate();

    const symLabel = document.getElementById("symbolLabel");
    if (symLabel) symLabel.textContent = SYMBOL;

    document.getElementById("labelRefreshRuntime").textContent = String(REFRESH_RUNTIME_MS / 1000);
    document.getElementById("labelRefreshData").textContent = String(REFRESH_DATA_MS / 1000);

    bindTabs();
    document.getElementById("btnRefreshAll").addEventListener("click", () => loadAll());
    document.getElementById("btnLock").addEventListener("click", () => postLock().catch((e) => showError(e.message)));
    document.getElementById("btnUnlockOnce").addEventListener("click", () => postUnlockOnce().catch((e) => showError(e.message)));
    document.getElementById("btnClose").addEventListener("click", () => postClose().catch((e) => showError(e.message)));
    document.getElementById("btnCleanup").addEventListener("click", () => postCleanup().catch((e) => showError(e.message)));
    document.getElementById("btnReconcile").addEventListener("click", () => postReconcile().catch((e) => showError(e.message)));

    loadAll();
    startAutoRefresh();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

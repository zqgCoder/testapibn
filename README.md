# TradingView Webhook -> Binance USD-M Futures Bot

这是一个 Python FastAPI 项目，用来接收 TradingView Webhook 信号，并在 Binance USDⓈ-M Futures 下单。

默认配置是安全的：`ENABLE_TRADING=false`，只会计算交易计划并写日志，不会向币安提交订单。

## 功能

- 接收 TradingView Webhook JSON
- Webhook secret 校验
- 白名单交易对限制
- 防重复信号处理 SQLite
- 手动计算仓位大小：`margin_usdt * leverage` 或直接用 `notional_usdt`
- 自动风控仓位：开仓前读取账户实际余额，按固定账户百分比或固定 USDT 亏损反推下单数量
- 自动杠杆：风险仓位算出名义价值后，根据保证金预算自动计算杠杆
- 手续费估算：读取交易对 taker 手续费，止损亏损估算包含开仓费 + 止损平仓费
- 设置杠杆
- 市价开仓
- 止损：`STOP_MARKET`，`closePosition=true`
- 分批止盈：多档 `TAKE_PROFIT_MARKET`，`reduceOnly=true`
- 开仓前取消旧普通订单与旧条件单
- 日志文件：`logs/bot.log`

## 重要说明

1. 本项目只支持 Binance USD-M Futures 单向持仓模式 One-way Mode。
2. 先用 Futures Demo/Testnet，不要直接上实盘。
3. 不要把 Binance API Secret 放进 TradingView Alert Message。
4. TradingView Webhook URL 必须是公网 HTTPS，通常用 443 端口。
5. 2025-12-09 后，Binance USD-M Futures 条件单迁移到 Algo Service，本项目止盈止损使用 `/fapi/v1/algoOrder`。

## 安装

```bash
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`：

```env
BINANCE_API_KEY=你的测试网API_KEY
BINANCE_API_SECRET=你的测试网API_SECRET
BINANCE_BASE_URL=https://demo-fapi.binance.com
WEBHOOK_SECRET=你自己生成的长随机字符串
ENABLE_TRADING=false
ALLOWED_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT
```

## 运行

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

## 本地测试信号

先不要开交易：`.env` 保持 `ENABLE_TRADING=false`。

```bash
curl -X POST http://127.0.0.1:8000/tradingview \
  -H "Content-Type: application/json" \
  -d @examples/tradingview_alert.json
```

查看日志：

```bash
cat logs/bot.log
```

## 让测试网真实下单

确认你使用的是 Binance Futures Demo/Testnet API Key，且 `.env` 里是：

```env
BINANCE_BASE_URL=https://demo-fapi.binance.com
ENABLE_TRADING=true
```

然后重启服务。

## TradingView Alert Message 示例

```json
{
  "secret": "你的Webhook密钥",
  "signal_id": "BTCUSDT-{{time}}-long",
  "symbol": "BTCUSDT",
  "side": "buy",
  "margin_usdt": 20,
  "leverage": 2,
  "sl": 65000,
  "tps": [
    {"price": 68000, "qty_pct": 0.5},
    {"price": 70000, "qty_pct": 0.3},
    {"price": 72000, "qty_pct": 0.2}
  ],
  "cancel_before_open": true,
  "working_type": "MARK_PRICE"
}
```

做空示例：

```json
{
  "secret": "你的Webhook密钥",
  "signal_id": "BTCUSDT-{{time}}-short",
  "symbol": "BTCUSDT",
  "side": "sell",
  "margin_usdt": 20,
  "leverage": 2,
  "sl": 72000,
  "tps": [
    {"price": 68000, "qty_pct": 0.5},
    {"price": 66000, "qty_pct": 0.3},
    {"price": 64000, "qty_pct": 0.2}
  ],
  "cancel_before_open": true,
  "working_type": "MARK_PRICE"
}
```



## 自动风控仓位：按账户百分比或固定金额止损

新增 3 种模式：

```text
risk_mode="manual"      # 原来的模式：margin_usdt * leverage 或 notional_usdt
risk_mode="fixed_pct"   # 按账户余额百分比亏损，例如 risk_pct=0.01 表示最多亏 1%
risk_mode="fixed_usdt"  # 按固定金额亏损，例如 risk_usdt=10 表示最多亏 10 USDT
```

核心公式：

```text
允许亏损 = 账户余额 * risk_pct
或
允许亏损 = risk_usdt

单个币亏损 = abs(开仓参考价 - 止损价)
单个币手续费 = (开仓参考价 + 止损价) * taker_fee_rate * FEE_SAFETY_MULTIPLIER
下单数量 = 允许亏损 / (单个币亏损 + 单个币手续费)
名义价值 = 下单数量 * 开仓参考价
自动杠杆 = ceil(名义价值 / 保证金预算)
```

说明：杠杆本身不会决定止损亏多少，止损亏损主要由“仓位数量 × 止损距离”决定。自动杠杆只是根据已经算出的名义价值和你愿意占用的保证金预算来反推。

### 按账户 1% 风险开多

```json
{
  "secret": "你的Webhook密钥",
  "signal_id": "BTCUSDT-{{time}}-risk-long",
  "symbol": "BTCUSDT",
  "side": "buy",
  "risk_mode": "fixed_pct",
  "risk_pct": 0.01,
  "margin_usdt": 50,
  "sl": 65000,
  "tps": [
    {"price": 68000, "qty_pct": 0.5},
    {"price": 70000, "qty_pct": 0.3},
    {"price": 72000, "qty_pct": 0.2}
  ],
  "cancel_before_open": true,
  "working_type": "MARK_PRICE"
}
```

这个例子会先读取账户 USDT 可用余额。假设可用余额是 1000 USDT，`risk_pct=0.01`，则目标最大亏损约为 10 USDT。程序会把开仓手续费和止损平仓手续费算进去，然后反推出下单数量；再根据 `margin_usdt=50` 自动计算需要几倍杠杆。

### 按固定 10 USDT 风险开空

```json
{
  "secret": "你的Webhook密钥",
  "signal_id": "BTCUSDT-{{time}}-risk-short",
  "symbol": "BTCUSDT",
  "side": "sell",
  "risk_mode": "fixed_usdt",
  "risk_usdt": 10,
  "margin_usdt": 50,
  "sl": 72000,
  "tps": [
    {"price": 68000, "qty_pct": 0.5},
    {"price": 66000, "qty_pct": 0.3},
    {"price": 64000, "qty_pct": 0.2}
  ],
  "cancel_before_open": true,
  "working_type": "MARK_PRICE"
}
```

### 不传 margin_usdt 时

如果 `risk_mode` 是 `fixed_pct` 或 `fixed_usdt`，但 TradingView 没传 `margin_usdt`，程序会使用：

```text
保证金预算 = 账户可用余额 * AUTO_MARGIN_BALANCE_PCT
```

例如可用余额 1000 USDT，`AUTO_MARGIN_BALANCE_PCT=0.20`，则保证金预算是 200 USDT。

### 本地只看计划，不下单

```bash
curl -X POST http://127.0.0.1:8000/plan \
  -H "Content-Type: application/json" \
  -d @examples/tradingview_alert_risk_pct.json
```

`/plan` 会返回计划，不会下单；`/tradingview` 在 `ENABLE_TRADING=false` 时也只会 dry-run。

## 部署建议

生产环境推荐：

- VPS + Nginx + HTTPS
- 只开放 443
- Binance API Key 绑定 VPS 固定 IP
- `WEBHOOK_SECRET` 使用 32 位以上随机字符串
- 先跑 Demo/Testnet 至少几天

Nginx 反代示例：

```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;

    location /tradingview {
        proxy_pass http://127.0.0.1:8000/tradingview;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

TradingView Webhook URL：

```text
https://your-domain.com/tradingview
```

## v3 新增：查询接口与已有持仓处理

### 查询接口

启动服务后可以直接用浏览器或 curl 查看当前状态：

```bash
curl http://127.0.0.1:8000/positions
curl http://127.0.0.1:8000/positions/BTCUSDT
curl http://127.0.0.1:8000/open-orders/BTCUSDT
curl http://127.0.0.1:8000/algo-orders/BTCUSDT
```

含义：

```text
/positions                 查看当前非 0 持仓
/positions/BTCUSDT         查看 BTCUSDT 持仓
/open-orders/BTCUSDT       查看普通未成交委托
/algo-orders/BTCUSDT       查看止损、止盈等条件单
```

也可以手动取消某个交易对的所有普通委托和条件单：

```bash
curl -X DELETE http://127.0.0.1:8000/orders/BTCUSDT
```

### 已有持仓处理 position_policy

新增字段：

```json
"position_policy": "replace"
```

可选值：

```text
replace           默认推荐。先取消旧委托/条件单，平掉当前持仓，再开新仓。
reverse_only      当前仓位方向相反时才平旧仓再开新仓；同向信号会跳过。
ignore_same_side  同向信号跳过；反向信号平旧仓再开新仓。
add               保留旧仓并继续开新仓。谨慎使用。
```

如果 JSON 不传 `position_policy`，则使用 `.env` 里的：

```env
DEFAULT_POSITION_POLICY=replace
```

推荐保持 `replace`，这样 TradingView 出现反向信号时，机器人会先清理旧 TP/SL，再平掉旧仓，然后开新方向仓位并挂新的止损止盈。

## 只读 Dashboard（v5 Phase 3）

浏览器查看交易日志与统计，**不能下单、平仓或改配置**。

### 配置

在 `.env` 中设置：

```env
DASHBOARD_ENABLED=true
DASHBOARD_REQUIRE_TOKEN=true
DASHBOARD_TOKEN=你自己生成的长随机字符串
DASHBOARD_AUTO_REFRESH_SEC=10
```

- `DASHBOARD_ENABLED=false` 时，Dashboard 页面与 API 返回 404。
- `DASHBOARD_REQUIRE_TOKEN=true` 时，必须携带正确 Token 才能访问。
- `DASHBOARD_AUTO_REFRESH_SEC=0` 关闭页面自动刷新。

### 访问

启动服务后，在浏览器打开：

```text
http://127.0.0.1:8000/dashboard?token=你的DASHBOARD_TOKEN
```

页面会从 URL 读取 token 并存入内存，后续 `fetch` 自动带上请求头 `X-Dashboard-Token`。不使用 localStorage，也不在页面上显示 token 明文。

### Dashboard API

```text
GET /dashboard/api/summary
GET /dashboard/api/by-symbol
GET /dashboard/api/rejections
GET /dashboard/api/executions?limit=50&symbol=BTCUSDT&status=protected
GET /dashboard/api/executions/{execution_id}
GET /dashboard/api/orders/{execution_id}
GET /dashboard/api/runtime
GET /dashboard/api/health
GET /dashboard/api/positions
GET /dashboard/api/algo-orders
GET /dashboard/api/runtime-control/status
GET /dashboard/api/runtime-control/events?limit=10
GET /dashboard/api/health-overview
GET /dashboard/api/alerts?limit=20
```

Token 可通过请求头 `X-Dashboard-Token` 或 query 参数 `?token=` 传递。

原有的 `/journal/*` 与 `/stats/*` 接口默认不受 Dashboard 开关影响；可通过下方配置单独启用 Token 保护。

## Dashboard 安全增强 + 账户状态（v5 Phase 4）

### 新增配置

```env
PROTECT_JOURNAL_API=false
PROTECT_STATS_API=false
```

- `PROTECT_JOURNAL_API=true` 时，`/journal/*` 必须携带与 `DASHBOARD_TOKEN` 相同的 Token（请求头 `X-Dashboard-Token` 或 `?token=`）。
- `PROTECT_STATS_API=true` 时，`/stats/*` 同上。
- 默认 `false`，保持 v5.1 无 Token 行为；**公网部署时建议开启**，避免未保护的 journal/stats 被直接访问。

启用任一保护项时，必须在 `.env` 中设置 `DASHBOARD_TOKEN`。

### 新增 Dashboard API

```text
GET /dashboard/api/runtime      脱敏运行配置（不含任何 secret）
GET /dashboard/api/health       服务健康 + 账户余额（失败时返回 account_error，不 500）
GET /dashboard/api/positions    当前非 0 持仓
GET /dashboard/api/algo-orders  ALLOWED_SYMBOLS 下所有条件单
```

Dashboard 页面新增三个只读区域：**运行状态**、**当前持仓**、**当前条件单**。仍无任何下单/平仓/改配置按钮。

### 安全提示

- 不要在页面或 API 响应中暴露 `BINANCE_API_KEY`、`BINANCE_API_SECRET`、`WEBHOOK_SECRET`、`DASHBOARD_TOKEN`。
- 若 Dashboard 或 journal/stats 需公网访问，务必启用 Token 保护并使用 HTTPS 反向代理。
- Journal 写入与 API 返回时会自动脱敏 `secret`、`token`、`api_key` 等敏感字段（显示为 `***REDACTED***`），历史库中已有明文也会在读取时再次脱敏。

## Runtime Control / 运行锁定（v5.4）

手动锁定后，新的 webhook 信号会在 `prepare_plan` 之前安全跳过，并写入 journal（`status=blocked_by_runtime_lock`）。`/plan` 不受锁定影响；duplicate 信号仍先拦截且不写 journal。

### 配置

```env
RUNTIME_CONTROL_ENABLED=false
RUNTIME_CONTROL_REQUIRE_TOKEN=true
RUNTIME_CONTROL_TOKEN=你自己生成的长随机字符串
RUNTIME_STATUS_ALLOW_DASHBOARD_TOKEN=true
```

`RUNTIME_CONTROL_TOKEN` 与 `DASHBOARD_TOKEN` 分离：Dashboard Token **只能读** status/events，**不能** lock/unlock。

### 接口

```text
GET  /runtime/status
GET  /runtime/events?limit=50
POST /runtime/lock
POST /runtime/unlock
```

**读接口 Token**（启用且 `RUNTIME_CONTROL_REQUIRE_TOKEN=true` 时）：
- `X-Runtime-Control-Token` 或 `?control_token=`
- 若 `RUNTIME_STATUS_ALLOW_DASHBOARD_TOKEN=true`，也可用 `X-Dashboard-Token` 或 `?token=`

**写接口 Token**（lock/unlock）：仅 `X-Runtime-Control-Token` 或 `?control_token=`

锁定示例：

```bash
curl -X POST http://127.0.0.1:8000/runtime/lock \
  -H "X-Runtime-Control-Token: 你的control_token" \
  -H "Content-Type: application/json" \
  -d '{"reason":"manual maintenance","locked_until":null}'
```

解锁：

```bash
curl -X POST http://127.0.0.1:8000/runtime/unlock \
  -H "X-Runtime-Control-Token: 你的control_token" \
  -H "Content-Type: application/json" \
  -d '{}'
```

若设置了 `locked_until` 且已过期，系统会自动解锁并记录 `auto_expire` 事件。

## Dashboard 展示 Runtime Control（v5.5）

Dashboard 页面**只读**展示运行锁定状态与最近事件，**不提供** lock/unlock 按钮。

### Dashboard 只读 API

```text
GET /dashboard/api/runtime-control/status
GET /dashboard/api/runtime-control/events?limit=10
GET /dashboard/api/health-overview
GET /dashboard/api/alerts?limit=20
GET /dashboard/api/risk-config
```

- 仅支持 **Dashboard Token**（`X-Dashboard-Token` 或 `?token=`）
- **不接受** `RUNTIME_CONTROL_TOKEN` 作为 Dashboard API 鉴权
- 需 `RUNTIME_STATUS_ALLOW_DASHBOARD_TOKEN=true`；否则返回 403「Dashboard 无权限读取运行控制状态」
- `RUNTIME_CONTROL_ENABLED=false` 时正常返回 `enabled=false`，不报 500
- 响应中不包含任何 token/secret

### lock/unlock 操作

锁定/解锁只能通过 **Runtime Control Token** 调用：

```text
POST /runtime/lock
POST /runtime/unlock
```

Dashboard Token 只能读取状态，不能执行写操作。

## Health Overview / 系统健康摘要（v5.6）

Dashboard **只读**展示系统运行健康状态与关键风险提示。不会自动下单、平仓、撤单、解锁或修复任何问题。

### Dashboard API

```text
GET /dashboard/api/health-overview
GET /dashboard/api/alerts?limit=20
```

仅支持 **Dashboard Token** 鉴权，不返回任何 secret/token。

### 健康等级

| 等级 | 含义 |
|------|------|
| **OK** | 当前检查项无严重问题 |
| **WARN** | 存在需关注的风险或配置提醒 |
| **ERROR** | 存在需要尽快处理的问题 |

典型 **ERROR** 场景：
- Binance 账户查询失败
- 最近 journal 执行状态为 `failed` / `protection_failed`
- **有持仓但无止损（STOP/STOP_MARKET）条件单** — 标记为「存在未保护持仓」

典型 **WARN** 场景：
- `ENABLE_TRADING=false`（仅监控）
- `ENABLE_TRADING=true`（真实交易已启用）
- Runtime Control 已锁定或未启用
- 当前有持仓
- Journal/Stats API 未启用 Token 保护

### 检查项

`service_health`、`binance_account`、`enable_trading`、`runtime_lock`、`recent_execution`、`open_positions`、`protection_orders`、`api_protection`、`runtime_status_permission`

页面新增 **「系统健康摘要」** 区块：总体等级、关键指标、风险提示列表。接口失败时仅该区块显示错误，不影响整页。

## Alert Center / 告警中心（v5.7）

Dashboard **只读**聚合最近风险事件，来源包括 Health Overview 检查、Journal 执行记录、Runtime Control 事件。不会自动交易、撤单、平仓、解锁或修复。

### Dashboard API

```text
GET /dashboard/api/alerts?limit=20
```

- 仅 **Dashboard Token** 鉴权（`X-Dashboard-Token` / `?token=`）
- `limit` 默认 20，最大 100
- 不返回任何 secret/token
- 动态聚合，不新增数据库表

### 告警来源

| source | 说明 |
|--------|------|
| `health` | Health Overview 中 WARN/ERROR 检查项 |
| `journal` | 最近执行：`failed`、`protection_failed`、`blocked_*`、`entry_not_filled` 等 |
| `runtime` | lock/unlock/auto_expire 事件及当前锁定状态 |

`protected` 等成功状态默认不产生告警，避免噪音。

### 严重级别

| 等级 | 典型场景 |
|------|----------|
| **ERROR** | 执行失败、保护单失败、未保护持仓 |
| **WARN** | Runtime 锁定、风控拒绝、runtime lock 拦截、有持仓 |
| **INFO** | Runtime 解锁、自动过期 |

页面新增 **「告警中心」** 区块：ERROR/WARN/INFO 计数、最新等级、告警表格。

## Risk Config Inspector / 风控配置体检（v5.8）

Dashboard **只读**检查当前 `.env` / Settings 中的交易、安全、风控、Runtime、Dashboard 配置是否合理。

**不会**修改配置、自动修复、下单、撤单、平仓或解锁。

### Dashboard API

```text
GET /dashboard/api/risk-config
```

- 仅支持 **Dashboard Token**（`X-Dashboard-Token` 或 `?token=`）
- **不接受** `RUNTIME_CONTROL_TOKEN`
- 无 token 或错误 token 返回「Dashboard Token 无效」
- 不返回任何 secret/token/API key 明文；仅展示是否已配置及长度

### 返回结构

```json
{
  "成功": true,
  "配置体检": {
    "level": "OK|WARN|ERROR",
    "checks": [{"name": "...", "level": "...", "message": "..."}],
    "summary": {
      "app_version": "1.11.0",
      "enable_trading": false,
      "binance_env": "demo",
      "runtime_control_enabled": true,
      "dashboard_protected": true,
      "journal_protected": true,
      "stats_protected": true,
      "allowed_symbol_count": 3,
      "max_auto_leverage": 20
    }
  }
}
```

### 检查范围

| 检查项 | 说明 |
|--------|------|
| `binance_environment` | demo/testnet vs 实盘 endpoint |
| `enable_trading` | 真实交易开关 |
| `webhook_secret` | Webhook 密钥是否配置、长度 |
| `dashboard_token` | Dashboard Token 保护 |
| `runtime_control` | Runtime Control 启用与 Token |
| `journal_stats_protection` | Journal / Stats API 保护 |
| `allowed_symbols` | 白名单交易对数量与格式 |
| `leverage_policy` | 最大自动杠杆 |
| `order_entry_policy` | 市价/限价入场策略 |
| `protection_policy` | 保护单失败策略与持仓策略 |
| `account_risk_guard` | 账户级风控开关与参数 |
| `dashboard_readonly_guarantee` | Dashboard 只读保证 |

### 风险等级

- 任一检查项为 **ERROR** → 总体 **ERROR**
- 否则任一 **WARN** → 总体 **WARN**
- 否则 **OK**

页面新增 **「风控配置体检」** 区块：总体等级、环境摘要、风控提示表格。接口失败时仅影响该区块，不影响整页。


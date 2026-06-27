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


# V4：市价 / 限价进场说明

本版本新增 `entry_type=market|limit`，用于在 TradingView JSON 中选择进场方式。

## 1. 市价进场

适合突破、追单、必须成交的信号。

```json
{
  "entry_type": "market",
  "signal_price": 60000,
  "max_slippage_pct": 0.3
}
```

字段说明：

- `signal_price`：TradingView 发信号时的参考价格，建议传 `close`。
- `max_slippage_pct`：Binance 实时价格与 `signal_price` 偏差超过该百分比时拒绝开仓。`0.3` 表示 0.3%。
- 如果不传 `signal_price`，机器人无法做滑点校验，只会按 Binance 最新价计算仓位。

## 2. 限价进场

适合回踩、反弹、不想追单的信号。

```json
{
  "entry_type": "limit",
  "limit_price": 60200,
  "limit_timeout_sec": 60,
  "limit_fallback_to_market": false
}
```

执行流程：

```text
收到信号
→ 先按风控计算计划
→ 处理已有持仓和旧挂单
→ 挂限价开仓单
→ 等待 limit_timeout_sec 秒
→ 成交后再挂止损和止盈
→ 超时未成交则撤单
→ 如果 limit_fallback_to_market=true，则未成交部分改成市价补齐
```

注意：限价单未成交时，机器人不会挂止盈止损，因为没有实际仓位。

## 3. .env 新增配置

```env
ALLOW_MARKET_ENTRY=true
ALLOW_LIMIT_ENTRY=true
DEFAULT_ENTRY_TYPE=market
DEFAULT_LIMIT_TIMEOUT_SEC=60
DEFAULT_LIMIT_FALLBACK_TO_MARKET=false
DEFAULT_MAX_SLIPPAGE_PCT=0.30
LIMIT_POLL_INTERVAL_SEC=2
RECV_WINDOW=30000
```

## 4. 示例文件

- `examples/tradingview_alert_v4_market.json`
- `examples/tradingview_alert_v4_limit.json`
- `examples/盈利因子优化版_v4_entry_dynamic_webhook.pine`

## 5. 测试命令

市价计划：

```powershell
curl.exe -X POST http://127.0.0.1:8000/plan -H "Content-Type: application/json" -d "@examples/tradingview_alert_v4_market.json"
```

限价计划：

```powershell
curl.exe -X POST http://127.0.0.1:8000/plan -H "Content-Type: application/json" -d "@examples/tradingview_alert_v4_limit.json"
```

真实测试网执行：

```powershell
curl.exe -X POST http://127.0.0.1:8000/tradingview -H "Content-Type: application/json" -d "@examples/tradingview_alert_v4_market.json"
```

通过 ngrok：

```powershell
curl.exe -k --http1.1 -H "ngrok-skip-browser-warning: true" -X POST https://你的ngrok地址/tradingview -H "Content-Type: application/json" -d "@D:\projects\trade\testapibn\examples\tradingview_alert_v4_market.json"
```

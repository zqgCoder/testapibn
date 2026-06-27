# 开启服务
窗口1：
$env:HTTP_PROXY="http://127.0.0.1:7890"
$env:HTTPS_PROXY="http://127.0.0.1:7890"
$env:NO_PROXY="127.0.0.1,localhost"
窗口2：
curl.exe -X POST http://127.0.0.1:8000/plan -H "Content-Type: application/json" -d "@examples/tradingview_alert_short.json"
其中，plan是计划，tradingview是实盘，需要在env文件修改开关
# ===== 安全开关 =====
# false：只打印计划，不向币安提交订单；true：真的向 BINANCE_BASE_URL 提交订单
ENABLE_TRADING=true


## 模式下单文件
copy examples\tradingview_alert_fixed_usdt.json examples\tradingview_alert_short.json
内容：
{
  "secret": "你的Webhook密钥",
  "signal_id": "BTCUSDT-fixed-usdt-demo-short-001",
  "symbol": "BTCUSDT",
  "side": "sell",
  "risk_mode": "fixed_usdt",
  "risk_usdt": 2,
  "margin_usdt": 50,
  "sl": 61000,
  "tps": [
    {"price": 58500, "qty_pct": 0.5},
    {"price": 57000, "qty_pct": 0.3},
    {"price": 55000, "qty_pct": 0.2}
  ],
  "cancel_before_open": true,
  "working_type": "MARK_PRICE"
}


收到新信号
→ 取消旧的普通委托
→ 取消旧的止损/止盈条件单
→ 如果当前有持仓，先市价平掉
→ 再开新仓
→ 再挂新的止损和分批止盈

目前已经完成：
1. 开多
2. 开空
3. 固定金额风险
4. 账户百分比风险
5. 自动止损
6. 分批止盈
7. 反向信号自动平旧仓
8. 反向信号自动开新仓
9. 取消旧普通挂单
10. 取消旧条件单

curl.exe -k --http1.1 -H "ngrok-skip-browser-warning: true" 
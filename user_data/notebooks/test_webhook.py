import sys
import os
import pandas as pd

# 添加模块路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from live_signal_runner import notify_entry, notify_settlement, DINGTALK_WEBHOOK, KEYWORD

print("==================================================")
print("钉钉群机器人下单/结算消息推送测试")
print(f"当前配置的 Webhook: {DINGTALK_WEBHOOK}")
print(f"当前安全关键词: {KEYWORD}")
print("==================================================")

# 模拟当前 UTC 时间
mock_time = pd.Timestamp.utcnow().floor('min')

print("\n1. 正在模拟发送 [下单通知]...")
# notify_entry(direction, combo, wr, trades, entry_price, entry_time)
notify_entry(
    direction="涨",
    combo="000000_1_1",
    wr=0.6267,
    trades=217,
    entry_price=67250.50,
    entry_time=mock_time
)

print("\n2. 正在模拟发送 [结算通知]...")
# notify_settlement(trade, settle_price)
mock_trade = {
    "entry_time": mock_time,
    "entry_price": 67250.50,
    "direction": "涨",
    "expiry_time": mock_time + pd.Timedelta(minutes=10),
    "combo": "000000_1_1"
}
notify_settlement(mock_trade, settle_price=67320.80)

print("\n==================================================")
print("测试脚本执行完毕，请检查您的钉钉群聊消息！")
print("==================================================")

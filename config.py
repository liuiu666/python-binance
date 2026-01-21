import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# API 配置
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

# 代理配置
PROXY = {
    "http": os.getenv("HTTP_PROXY"),
    "https": os.getenv("HTTPS_PROXY")
}

# 交易配置
SYMBOL = "WETUSDT"   # 交易对
# 策略配置
STRATEGY_NAME = "Trend_Fast_V8" # 策略名称
TIMEFRAME = "1m"    # 时间周期 (1m 极速版)

# 高频交易配置 (已禁用)
HFT_MODE = False          # 是否开启高频模式
HFT_INTERVAL = 1.0        # 高频轮询间隔 (秒)
DEPTH_LIMIT = 5           # 深度获取档位
IMBALANCE_THRESHOLD = 2.0 # 盘口失衡阈值 (多空比 > 2 或 < 0.5)
SCALP_TP = 0.003          # 高频止盈 (0.3%)
SCALP_SL = 0.002          # 高频止损 (0.2%)

# 资金管理配置
RISK_PERCENT = 0.03  # 单笔交易风险比例 (降低到 3% 以减少亏损心理压力)
# 计算公式: 亏损金额 = 总资金 * RISK_PERCENT
# 仓位数量 = 亏损金额 / (止损距离 * 价格)

# 策略参数 (布林带 + RSI 均值回归)
EMA_SLOW_PERIOD = 120 # 慢速均线 (判断大趋势)
EMA_FAST_PERIOD = 20  # 快速均线 (判断短期趋势)
BB_PERIOD = 20       # 布林带周期
BB_STD = 2.0         # 布林带标准差
RSI_PERIOD = 14      # RSI 周期
RSI_OVERBOUGHT = 70  # RSI 超买阈值 (做空信号)
RSI_OVERSOLD = 30    # RSI 超卖阈值 (做多信号)

# 自适应策略参数 (Strategy V6)
ADX_PERIOD = 14      # ADX 周期
ADX_THRESHOLD = 25   # ADX 阈值 (大于此值视为趋势，小于视为震荡)
CCI_PERIOD = 20      # CCI 周期 (用于震荡辅助)

# 辅助风控 (ATR)
ATR_PERIOD = 14      # ATR 周期
MIN_VOLATILITY = 0.001 # 最小波动率 0.1% (1m 周期)


# 利润保护配置 (1m 顺势极速版)
PROFIT_LOCK_ENABLE = True    # 是否开启利润保护
BREAKEVEN_ATR = 1.5          # 盈利达 1.5 ATR (约0.3%) -> 开启保本
TP_TRIGGER_ATR = 4.0         # 盈利达 4.0 ATR (约0.8%) -> 启动移动止盈
TP_CALLBACK_ATR = 1.0        # 移动止盈回调 1.0 ATR -> 止盈出局

# 做T配置 (波段策略本身就是做T，此开关可辅助)
DO_T_ENABLE = False          # 关闭额外的做T，策略本身即为震荡策略
T_RATIO = 0.5                # 做T仓位比例
RSI_BUY_BACK = 50            # 做T接回阈值

FEE_RATE = 0.0005       # 币安合约 Taker 费率 (0.05%)
SLIPPAGE = 0.0005        # 预估滑点 (0.05%)

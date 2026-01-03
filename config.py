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
TIMEFRAME = "15m"    # 时间周期 (升级为 15分钟，抓大波段，过滤噪音)
# QUANTITY = 50      # 已弃用，改用动态仓位管理

# 资金管理配置
RISK_PERCENT = 0.05  # 单笔交易风险比例 (5% 总资金风险)
# 计算公式: 亏损金额 = 总资金 * RISK_PERCENT
# 仓位数量 = 亏损金额 / (止损距离 * 价格) = 亏损金额 / (ATR * ATR_MULTIPLIER_SL)

# 风控配置 (SuperTrend 趋势追踪版)
# SuperTrend 本身即为止损线，不再需要单独的 ATR_MULTIPLIER_SL
# ATR 仅用于辅助计算仓位大小
ATR_PERIOD = 10      # SuperTrend ATR 周期
ATR_MULTIPLIER = 3.0 # SuperTrend 倍数 (3.0 是标准趋势倍数)

# 利润保护配置 (新增)
PROFIT_LOCK_ENABLE = True    # 是否开启利润保护
BREAKEVEN_ATR = 0.8          # 盈利达 0.8 ATR 时 -> 开启保本 (原 1.5 太慢)
TP_TRIGGER_ATR = 2.0         # 盈利达 2.0 ATR 时 -> 启动移动止盈 (原 3.0 太贪)
TP_CALLBACK_ATR = 0.8        # 移动止盈回调 0.8 ATR -> 止盈出局 (原 1.0 太宽)

FEE_RATE = 0.0004       # 币安合约 Taker 费率 (0.04%)
SLIPPAGE = 0.001        # 预估滑点 (0.1%)

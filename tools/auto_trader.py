"""
自动化交易主程序 (Auto Trader)
功能：
1. 实时监控市场，筛选优质币种
2. 结合 LLM 进行交易决策
3. 自动执行开仓、止损、止盈
4. 实时监控持仓，根据最新K线数据调整策略 (每分钟一次)
"""
import sys
import time
import logging
from pathlib import Path
from datetime import datetime

# Setup path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client, LLMAdvisor, FuturesSymbolSelector, FuturesTrader

# Configure logging
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
log_date = datetime.now().strftime("%Y-%m-%d")
log_file = LOG_DIR / f"{log_date}_auto_trader.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(log_file), encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

def main():
    logger.info("=== 启动自动化交易机器人 (Auto Trader) ===")
    
    # 1. 初始化
    settings = Settings.load(ROOT)
    client = create_client(settings)
    selector = FuturesSymbolSelector(client)
    
    # 初始化 Trader 和 Advisor
    trader = FuturesTrader(client)
    advisor = LLMAdvisor(trader)
    
    # 状态变量
    watch_candidate = None # 当前锁定的候选币种
    watch_start_time = None # 开始观察的时间
    MAX_WATCH_MINUTES = 30 # 最大观察时长 (30分钟未入场则换币)
    
    last_active_symbol = None # 上一轮周期的持仓币种

    while True:
        try:
            logger.info(f"--- 周期开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")

            # 1. 获取当前所有活跃持仓（本周期只获取一次）
            active_positions = advisor.trader.get_active_positions()
            active_symbols = {p['symbol'] for p in active_positions}
            logger.info(f"[DATA] active_positions_count={len(active_positions)} symbols={sorted(list(active_symbols))}")

            # 0. 全局挂单清理 (Orphan Order Cleanup)
            # 清理所有"无持仓"币种的遗留挂单，防止僵尸单堆积
            try:
                active_symbols_set = active_symbols
                
                # 获取账户所有挂单 (symbol=None 返回所有)
                # Use local client instead of advisor.client to be safe
                all_open_orders = client.futures_get_open_orders()
                open_orders_count = len(all_open_orders) if isinstance(all_open_orders, list) else 0
                logger.info(f"[DATA] all_open_orders_count={open_orders_count}")
                
                # 找出无持仓但有挂单的币种
                symbols_to_clean = set()
                if isinstance(all_open_orders, list):
                    for o in all_open_orders:
                        s = o.get('symbol')
                        if s and s not in active_symbols_set:
                            symbols_to_clean.add(s)
                
                # 执行清理
                if symbols_to_clean:
                    logger.info(f"[DECISION] orphan_cleanup symbols={sorted(list(symbols_to_clean))}")
                for s in symbols_to_clean:
                    logger.info(f"检测到 {s} 无持仓但有挂单，正在执行清理...")
                    advisor.trader.cancel_all_open_orders(s)
                    
            except Exception as e:
                logger.warning(f"全局挂单清理执行异常 (可能是网络波动): {e}")

            # 2. 检查当前持仓
            # 检查是否有持仓刚结束（如止损/止盈触发，或外部平仓）
            if last_active_symbol and last_active_symbol not in active_symbols:
                logger.info(f"检测到 {last_active_symbol} 持仓已结束，正在清理剩余挂单...")
                try:
                    advisor.trader.cancel_all_open_orders(last_active_symbol)
                    logger.info(f"已清理 {last_active_symbol} 的所有挂单")
                except Exception as e:
                    logger.error(f"清理挂单失败: {e}")
                try:
                    advisor.clear_symbol_state(last_active_symbol)
                except Exception:
                    pass
                last_active_symbol = None

            current_symbol = None
            
            if active_positions:
                # 假设只处理第一个持仓（单币种策略）
                pos = active_positions[0]
                current_symbol = pos['symbol']
                last_active_symbol = current_symbol # 更新当前持仓记录
                amt = float(pos.get('positionAmt', 0))
                
                logger.info(f"监控持仓: {current_symbol}")
                logger.info(f"持仓量: {amt}, 未实现盈亏: {pos.get('unRealizedProfit')} USDT")
                
                # 有持仓时，清空观察对象
                watch_candidate = None
                watch_start_time = None

                # 持仓中，执行实时分析与调整
                closed = advisor.monitor_position(current_symbol, pos=pos)
                if closed:
                    logger.info(f"持仓已由 AI 建议平仓: {current_symbol}")
                    current_symbol = None 
            
            # 3. 如果无持仓，进入选币或入场流程
            if not current_symbol and not active_positions:
                
                # A. 如果没有锁定的候选币，先去选币
                if not watch_candidate:
                    logger.info("无持仓且无观察对象，开始全市场选币...")
                    # 选币 (使用 cheap 模式，仅返回 1 个)
                    candidates = selector.get_smart_candidates(mode="cheap", limit=1)
                    
                    if candidates:
                        watch_candidate = candidates[0]
                        watch_start_time = time.time()
                        logger.info(f"锁定候选标的: {watch_candidate}，进入入场监控阶段")
                    else:
                        logger.info("未找到符合条件的标的，等待下一轮")
                
                # B. 如果有锁定的候选币，询问 AI 是否入场
                if watch_candidate:
                    # 检查是否超时
                    elapsed_min = (time.time() - watch_start_time) / 60
                    if elapsed_min > MAX_WATCH_MINUTES:
                        logger.info(f"观察 {watch_candidate} 已超时 ({int(elapsed_min)}分钟)，放弃并重新选币")
                        watch_candidate = None
                        watch_start_time = None
                        continue # 重新进入循环，下次会去选新币

                    logger.info(f"正在监控入场机会: {watch_candidate} (已观察 {int(elapsed_min)} 分钟)")
                    
                    # LLM 决策
                    decision = advisor.ask_llm(watch_candidate)
                    if decision:
                        logger.info(f"[DECISION] entry action={decision.action} direction={decision.direction} conf={decision.confidence} symbol={watch_candidate}")
                    
                    if decision and decision.action in ["BUY", "SELL"]:
                        logger.info(f"AI 决定入场: {decision.action} {watch_candidate} (信心: {decision.confidence})")
                        success = advisor.execute_trade(watch_candidate, decision)
                        if success:
                            logger.info(f"开仓成功: {watch_candidate}")
                            watch_candidate = None # 开仓成功后清除观察状态
                        else:
                            logger.error("开仓执行失败，继续观察")
                    else:
                        logger.info(f"AI 建议继续等待: {watch_candidate} (信号尚未确认)")
            
            sleep_sec = 120 if active_positions else 60
            logger.info(f"周期结束，等待 {sleep_sec} 秒...")
            time.sleep(sleep_sec)
            
        except KeyboardInterrupt:
            logger.info("用户停止程序")
            break
        except Exception as e:
            logger.error(f"发生错误: {e}")
            time.sleep(60) # 出错后也等待

if __name__ == "__main__":
    main()

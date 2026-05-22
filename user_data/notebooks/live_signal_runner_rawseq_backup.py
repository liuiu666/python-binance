import time
import requests
import pandas as pd
import numpy as np
import os
import json
from datetime import datetime

PID_FILE = "user_data/notebooks/live_signal_runner.pid"
ACTIVE_TRADES_FILE = "user_data/notebooks/active_trades.json"

def enforce_single_instance():
    current_pid = os.getpid()
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r', encoding='utf-8') as f:
                old_pid = int(f.read().strip())
            
            if old_pid != current_pid:
                import subprocess
                # 检查该 PID 的命令行是否属于本脚本以防误杀其他进程
                cmd = f'wmic process where "ProcessId={old_pid}" get CommandLine, Name'
                out = subprocess.check_output(cmd, shell=True).decode('gbk', errors='ignore')
                if "python" in out.lower() and "live_signal_runner.py" in out.lower():
                    print(f"[INFO] 检测到已存在运行中的监听器实例 (PID: {old_pid})，正在将其自动终止关闭...")
                    subprocess.call(f"taskkill /F /PID {old_pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(1) # 稍等 1 秒让资源释放
        except Exception as e:
            print(f"[WARNING] 检查单实例时发生异常: {e}")
            
    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, 'w', encoding='utf-8') as f:
            f.write(str(current_pid))
    except Exception as e:
        print(f"[WARNING] 写入 PID 文件失败: {e}")

def load_active_trades():
    if os.path.exists(ACTIVE_TRADES_FILE):
        try:
            with open(ACTIVE_TRADES_FILE, 'r', encoding='utf-8') as f:
                trades = json.load(f)
                # 将时间字符串转换回 pandas Timestamp 对象
                for t in trades:
                    t['entry_time'] = pd.Timestamp(t['entry_time'])
                    t['expiry_time'] = pd.Timestamp(t['expiry_time'])
                print(f"载入成功：从本地文件读取到了 {len(trades)} 笔未结算交易。")
                return trades
        except Exception as e:
            print(f"[WARNING] 载入活跃交易文件失败: {e}，将重新初始化。")
    return []

def save_active_trades(trades):
    try:
        serializable_trades = []
        for t in trades:
            st = t.copy()
            st['entry_time'] = t['entry_time'].isoformat()
            st['expiry_time'] = t['expiry_time'].isoformat()
            serializable_trades.append(st)
        
        with open(ACTIVE_TRADES_FILE, 'w', encoding='utf-8') as f:
            json.dump(serializable_trades, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"[WARNING] 保存活跃交易文件失败: {e}")

# ==================== CONFIGURATION ====================
# The webhook URL provided by the user
DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=941107d04389a231acc78a135a4fffd73551c6b3fae825924b0348dab0c684df"
KEYWORD = "666"

# Clash local proxy port to connect to Binance API in China
PROXIES = {
    "http": "http://127.0.0.1:7897",
    "https": "http://127.0.0.1:7897"
}

# Elite combos database
ELITE_COMBOS = {
    "CALL": {
        "000100_1_1": {"trades": 169, "wr": 0.6272},
        "000000_1_1": {"trades": 217, "wr": 0.6267},
        "011010_0_1": {"trades": 63, "wr": 0.6190},
        "110100_1_1": {"trades": 139, "wr": 0.6187},
        "010110_0_1": {"trades": 75, "wr": 0.6133},
        "011000_1_1": {"trades": 150, "wr": 0.6000},
        "000011_1_1": {"trades": 107, "wr": 0.5888}
    },
    "PUT": {
        "111011_1_1": {"trades": 145, "wr": 0.6552},
        "101001_0_1": {"trades": 62, "wr": 0.6290},
        "100101_0_1": {"trades": 64, "wr": 0.6250},
        "110101_1_0": {"trades": 132, "wr": 0.6061},
        "111001_1_0": {"trades": 103, "wr": 0.6019},
        "110001_1_1": {"trades": 131, "wr": 0.5878}
    }
}
# =======================================================

def send_dingtalk_notification(title, content):
    payload = {
        "msgtype": "text",
        "text": {
            "content": f"[{KEYWORD} {title}]\n{content}"
        }
    }
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        r = requests.post(DINGTALK_WEBHOOK, json=payload, headers=headers, proxies={"http": None, "https": None}, timeout=5)
        print(f"DingTalk Send Response Status: {r.status_code}")
        if r.status_code != 200:
            print("Response:", r.text[:200])
    except Exception as e:
        print(f"发送钉钉推送失败: {e}")

def notify_entry(direction, combo, wr, trades, entry_price, entry_time):
    china_entry_time = entry_time + pd.Timedelta(hours=8)
    china_expiry_time = china_entry_time + pd.Timedelta(minutes=10)
    
    dir_label = "看涨 (Call)" if direction == "涨" else "看跌 (Put)"
    wins = int(round(trades * wr))
    losses = trades - wins
    pnl = wins * 4.0 - losses * 5.0
    content = (
        f"类型: 下单通知\n"
        f"方向: {dir_label}\n"
        f"信号组合: {combo}\n"
        f"下单价格: {entry_price:.2f} USDT\n"
        f"下单时间: {china_entry_time.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)\n"
        f"预计结算: {china_expiry_time.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)\n"
        f"历史胜率: {wr * 100:.2f}%\n"
        f"触发次数: {trades}次 (成功 {wins}次 / 失败 {losses}次)\n"
        f"假设每次投5U(1.8x赔率)累计收益: {pnl:+.2f} U"
    )
    
    print("\n" + "="*50)
    print("[SIGNAL] 实时下单信号触发！")
    print(content)
    print("="*50)
    
    send_dingtalk_notification("裸K线期权-下单通知", content)

def notify_settlement(trade, settle_price):
    entry_price = trade['entry_price']
    direction = trade['direction']
    combo = trade['combo']
    entry_time = trade['entry_time']
    expiry_time = trade['expiry_time']
    
    china_entry_time = entry_time + pd.Timedelta(hours=8)
    china_expiry_time = expiry_time + pd.Timedelta(hours=8)
    
    if direction == "涨":
        is_win = settle_price > entry_price
    else:
        is_win = settle_price < entry_price
        
    result_str = "赢 (Profit)" if is_win else "输 (Loss)"
    price_change = settle_price - entry_price
    
    dir_label = "看涨 (Call)" if direction == "涨" else "看跌 (Put)"
    content = (
        f"类型: 结算通知\n"
        f"方向: {dir_label}\n"
        f"信号组合: {combo}\n"
        f"下单价格: {entry_price:.2f} USDT\n"
        f"结算价格: {settle_price:.2f} USDT (价差: {price_change:+.2f})\n"
        f"结算结果: {result_str}\n"
        f"下单时间: {china_entry_time.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)\n"
        f"结算时间: {china_expiry_time.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)"
    )
    
    print("\n" + "="*50)
    print("[SIGNAL] 期权交易已结算！")
    print(content)
    print("="*50)
    
    send_dingtalk_notification("裸K线期权-结算通知", content)

def fetch_and_calculate():
    url = "https://fapi.binance.com/fapi/v1/klines"  # USDT-M perpetual futures, matches backtest data
    params = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "limit": 100
    }
    
    try:
        r = requests.get(url, params=params, proxies=PROXIES, timeout=5)
        if r.status_code != 200:
            print(f"Fetch failed with status code: {r.status_code}")
            return None, None
        
        # Parse data
        klines = r.json()
        df = pd.DataFrame(klines, columns=[
            'date', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'qav', 'num_trades', 'taker_base', 'taker_quote', 'ignore'
        ])
        
        # Convert types
        df['date'] = pd.to_datetime(df['date'], unit='ms')
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
            
        # Resample to 2m
        df_2m = df.set_index('date').resample('2min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna().reset_index()
        
        # Compute indicators
        df_2m['dir'] = (df_2m['close'] > df_2m['open']).astype(int)
        
        vol_median = df_2m['volume'].rolling(window=20).median()
        df_2m['vol_high'] = (df_2m['volume'] > (vol_median * 1.4)).astype(int)
        
        full_range = df_2m['high'] - df_2m['low']
        range_mean = full_range.rolling(window=20).mean()
        df_2m['large_range'] = (full_range > (range_mean * 1.4)).astype(int)
        
        df_2m['seq_str'] = ""
        for shift in reversed(range(6)):
            df_2m['seq_str'] = df_2m['seq_str'] + df_2m['dir'].shift(shift).fillna(0).astype(int).astype(str)
            
        df_2m['combo_str'] = df_2m['seq_str'] + "_" + df_2m['vol_high'].astype(str) + "_" + df_2m['large_range'].astype(str)
        
        # Get the latest completed 2m bar (the second to last row, since the last row is the currently forming candle)
        latest_completed_bar = df_2m.iloc[-2]
        
        return latest_completed_bar, df_2m
        
    except Exception as e:
        print(f"Error fetching/calculating: {e}")
        return None, None

def main():
    enforce_single_instance()
    print("==================================================")
    print("BTC 10分钟期权：裸K线序列信号实时监听系统启动中...")
    print(f"钉钉推送URL: {DINGTALK_WEBHOOK}")
    print(f"安全关键词: {KEYWORD}")
    print(f"使用网络代理: {PROXIES['https']}")
    print("==================================================")
    
    last_processed_time = None
    active_trades = load_active_trades()
    
    while True:
        res = fetch_and_calculate()
        if res is not None and res[0] is not None:
            bar, df_2m = res
            bar_time = bar['date']
            combo = bar['combo_str']
            
            # 1. 检查是否有已到期的期权进行结算
            df_lookup = df_2m.set_index('date')
            still_active_trades = []
            trades_changed = False
            
            for trade in active_trades:
                expiry_time = trade['expiry_time']
                if expiry_time in df_lookup.index:
                    # 找到结算周期的K线，进行结算
                    settle_price = df_lookup.loc[expiry_time, 'close']
                    if isinstance(settle_price, pd.Series):
                        settle_price = float(settle_price.iloc[0])
                    else:
                        settle_price = float(settle_price)
                    notify_settlement(trade, settle_price)
                    trades_changed = True
                elif expiry_time > df_2m['date'].max():
                    # 尚未到期，继续保留
                    still_active_trades.append(trade)
                else:
                    # 已经过期但在df_2m中找不到（可能由于缺失K线），强制用当前最新价结算
                    settle_price = bar['close']
                    print(f"[WARNING] 未能找到 {expiry_time} 的K线数据，使用最新价格 {settle_price:.2f} 强制结算。")
                    notify_settlement(trade, settle_price)
                    trades_changed = True
            
            active_trades = still_active_trades
            if trades_changed:
                save_active_trades(active_trades)
            
            # 2. 检查是否有新的信号触发下单
            if last_processed_time is None:
                last_processed_time = bar_time
                china_time = bar_time + pd.Timedelta(hours=8)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 初始化监听成功。最近闭合K线时间: {china_time.strftime('%Y-%m-%d %H:%M:%S')} (北京时间), Combo: {combo}")
            elif bar_time > last_processed_time:
                last_processed_time = bar_time
                china_time = bar_time + pd.Timedelta(hours=8)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 新闭合2m K线时间: {china_time.strftime('%Y-%m-%d %H:%M:%S')} (北京时间), Combo: {combo}")
                
                # Check for signal
                direction = None
                stats = None
                if combo in ELITE_COMBOS['CALL']:
                    direction = "涨"
                    stats = ELITE_COMBOS['CALL'][combo]
                elif combo in ELITE_COMBOS['PUT']:
                    direction = "跌"
                    stats = ELITE_COMBOS['PUT'][combo]
                
                if direction and stats:
                    entry_price = bar['close']
                    # 下单通知
                    notify_entry(direction, combo, stats['wr'], stats['trades'], entry_price, bar_time)
                    # 加入活跃交易队列，10分钟（5个2m K线）后结算
                    expiry_time = bar_time + pd.Timedelta(minutes=10)
                    active_trades.append({
                        "entry_time": bar_time,
                        "entry_price": entry_price,
                        "direction": direction,
                        "expiry_time": expiry_time,
                        "combo": combo,
                        "wr": stats['wr'],
                        "trades": stats['trades']
                    })
                    save_active_trades(active_trades)
                else:
                    print("该时段无精英交易信号触发。")
                    
        # Sleep for 10 seconds before polling again
        time.sleep(10)

if __name__ == '__main__':
    main()

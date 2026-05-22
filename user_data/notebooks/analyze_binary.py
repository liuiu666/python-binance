import os
import json
import zipfile
import pandas as pd
import numpy as np

def analyze_binary_options():
    backtest_dir = 'user_data/backtest_results'
    
    # 1. Read .last_result.json to find the latest backtest zip file
    last_result_path = os.path.join(backtest_dir, '.last_result.json')
    if not os.path.exists(last_result_path):
        print(f"Error: {last_result_path} not found.")
        return
        
    with open(last_result_path, 'r') as f:
        last_result = json.load(f)
    
    latest_zip_name = last_result.get('latest_backtest')
    if not latest_zip_name:
        print("Error: Could not find latest backtest file name.")
        return
        
    zip_path = os.path.join(backtest_dir, latest_zip_name)
    if not os.path.exists(zip_path):
        print(f"Error: Zip file {zip_path} not found.")
        return
        
    print(f"Loading backtest results from: {latest_zip_name}")
    
    # 2. Extract and load the JSON file
    json_filename = latest_zip_name.replace('.zip', '.json')
    with zipfile.ZipFile(zip_path, 'r') as z:
        if json_filename not in z.namelist():
            # Find the JSON file inside
            json_files = [f for f in z.namelist() if f.endswith('.json') and not f.endswith('_config.json')]
            if not json_files:
                print("Error: No backtest result JSON found inside the zip file.")
                return
            json_filename = json_files[0]
            
        data = json.loads(z.read(json_filename))
        
    # Get strategy trades — auto-detect strategy name
    strategies = list(data['strategy'].keys())
    if not strategies:
        print("Error: No strategy found in backtest results.")
        return
    strategy_name = strategies[0]
    print(f"Detected strategy: {strategy_name}")
        
    trades = data['strategy'][strategy_name]['trades']
    if not trades:
        print("No trades found to analyze.")
        return
        
    df = pd.DataFrame(trades)
    print(f"Total standard trades loaded: {len(df)}")
    
    # 3. Apply Binary Options logic (80% payout on Win, -100% on Loss)
    payout = 0.80
    
    # Win condition:
    # - Call (Long, is_short=False): close_rate > open_rate
    # - Put (Short, is_short=True): close_rate < open_rate
    # Loss condition: opposite or equal (conservative)
    
    df['is_win'] = False
    # Call options
    df.loc[(df['is_short'] == False) & (df['close_rate'] > df['open_rate']), 'is_win'] = True
    # Put options
    df.loc[(df['is_short'] == True) & (df['close_rate'] < df['open_rate']), 'is_win'] = True
    
    # Profit calculation: win = +0.80 * stake, loss = -1.00 * stake
    df['binary_profit_ratio'] = np.where(df['is_win'], payout, -1.00)
    
    # Risking $100 per trade
    stake_amount = 100
    df['binary_profit_abs'] = df['binary_profit_ratio'] * stake_amount
    df['cumulative_profit'] = df['binary_profit_abs'].cumsum()
    
    # 4. Generate statistics
    total_trades = len(df)
    win_trades = df['is_win'].sum()
    loss_trades = total_trades - win_trades
    win_rate = win_trades / total_trades if total_trades > 0 else 0
    
    # Call Options specific stats
    calls_df = df[df['is_short'] == False]
    total_calls = len(calls_df)
    win_calls = calls_df['is_win'].sum() if total_calls > 0 else 0
    call_win_rate = win_calls / total_calls if total_calls > 0 else 0
    
    # Put Options specific stats
    puts_df = df[df['is_short'] == True]
    total_puts = len(puts_df)
    win_puts = puts_df['is_win'].sum() if total_puts > 0 else 0
    put_win_rate = win_puts / total_puts if total_puts > 0 else 0
    
    # Expected Return per trade (R-multiple)
    expectancy = (win_rate * payout) - (loss_trades / total_trades * 1.0)
    
    # Final results
    initial_capital = 10000
    final_profit = df['binary_profit_abs'].sum()
    final_capital = initial_capital + final_profit
    max_consecutive_wins = get_max_consecutive(df['is_win'], True)
    max_consecutive_losses = get_max_consecutive(df['is_win'], False)
    
    report = f"""
======================================================================
                 10分钟二元期权 BTC 回测分析报告 (80% 收益率)
======================================================================
回测时间段: {df['open_date'].min()} 至 {df['close_date'].max()}
回测天数: {data['strategy'][strategy_name]['backtest_days']:.2f} 天

【交易总览】
* 总交易笔数 (Total Trades)       : {total_trades} 笔
* 盈利笔数 (Win Trades)           : {win_trades} 笔
* 亏损笔数 (Loss Trades)          : {loss_trades} 笔
* 二元期权胜率 (Win Rate)         : {win_rate*100:.2f}%
* 盈亏临界点胜率 (Break-even WR)   : 55.56%  (低于此值长期交易将亏损)
* 单笔期望回报 (Expectancy)        : {expectancy:.4f} R (每投入1元，期望赚取/亏损)

【期权类型细分】
* 看涨期权 (Call / Long)
  - 总笔数: {total_calls} 笔
  - 胜出: {win_calls} 笔
  - 胜率: {call_win_rate*100:.2f}%
* 看跌期权 (Put / Short)
  - 总笔数: {total_puts} 笔
  - 胜出: {win_puts} 笔
  - 胜率: {put_win_rate*100:.2f}%

【模拟资金表现 (以每笔投入 $100 计)】
* 初始模拟资金 (Initial Capital)   : ${initial_capital}
* 净利润额 (Net Profit)           : ${final_profit:.2f}
* 最终资产 (Final Capital)        : ${final_capital:.2f}
* 模拟总收益率 (Total ROI)        : {final_profit/initial_capital*100:.2f}%
* 最大连续盈利次数 (Max Consec Wins): {max_consecutive_wins}
* 最大连续亏损次数 (Max Consec Loss): {max_consecutive_losses}

======================================================================
"""
    print(report)
    
    # Save report to a text file
    output_report_path = 'user_data/backtest_results/binary_options_report.txt'
    with open(output_report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"Report successfully saved to: {output_report_path}")

def get_max_consecutive(series, val):
    max_consec = 0
    current_consec = 0
    for item in series:
        if item == val:
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0
    return max_consec

if __name__ == '__main__':
    analyze_binary_options()

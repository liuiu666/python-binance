"""
深度诊断分析脚本 — 量子边际策略 V3 交易质量研究
分析维度：
1. Call/Put 按小时胜率分布（时段效应）
2. Call 失败时的 Z-Score 深度分布
3. ATR 波动率分区的胜率对比
4. 连续亏损序列特征
5. 胜败交易在星期几的分布
"""
import zipfile, json, os
import pandas as pd
import numpy as np

# ── 加载 V3 5m 回测结果 ──
RESULT = 'user_data/backtest_results/backtest-result-2026-05-21_21-18-05.zip'
with zipfile.ZipFile(RESULT) as z:
    json_name = [f for f in z.namelist() if f.endswith('.json') and '_config' not in f][0]
    data = json.loads(z.read(json_name))

strat = list(data['strategy'].keys())[0]
trades = data['strategy'][strat]['trades']
df = pd.DataFrame(trades)
print(f"策略: {strat} | 总交易: {len(df)} 笔")

# ── 计算二元期权胜负 ──
df['open_dt'] = pd.to_datetime(df['open_date'])
df['hour'] = df['open_dt'].dt.hour
df['weekday'] = df['open_dt'].dt.day_name()
df['is_win'] = (
    ((df['is_short'] == False) & (df['close_rate'] > df['open_rate'])) |
    ((df['is_short'] == True)  & (df['close_rate'] < df['open_rate']))
)
df['direction'] = df['is_short'].map({False: 'Call', True: 'Put'})
df['pnl_return'] = df['close_rate'] / df['open_rate'] - 1
df['move_pct'] = df['pnl_return'].abs() * 100  # 价格移动幅度 %

print("\n" + "="*60)
print("【1】Call vs Put 整体胜率")
print("="*60)
summary = df.groupby('direction')['is_win'].agg(['sum','count','mean'])
summary.columns = ['胜出', '总数', '胜率']
summary['胜率%'] = (summary['胜率'] * 100).round(2)
print(summary[['总数','胜出','胜率%']])

print("\n" + "="*60)
print("【2】按交易时段（UTC 小时）的胜率分布")
print("="*60)
hourly = df.groupby(['hour','direction'])['is_win'].agg(['sum','count','mean']).round(3)
hourly.columns = ['胜出', '总数', '胜率']
hourly['胜率%'] = (hourly['胜率'] * 100).round(1)
print(hourly[['总数','胜率%']].to_string())

print("\n" + "="*60)
print("【3】Call 失败样本的价格移动分布（失败时BTC移动了多少？）")
print("="*60)
call_fail = df[(df['direction'] == 'Call') & (df['is_win'] == False)]
call_win  = df[(df['direction'] == 'Call') & (df['is_win'] == True)]
print(f"Call 失败时，平均价格移动: {call_fail['move_pct'].mean():.4f}%")
print(f"Call 胜出时，平均价格移动: {call_win['move_pct'].mean():.4f}%")
print(f"Call 失败时，中位价格移动: {call_fail['move_pct'].median():.4f}%")

print("\n" + "="*60)
print("【4】按星期几的整体胜率")
print("="*60)
weekday_order = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
wd = df.groupby('weekday')['is_win'].agg(['sum','count','mean'])
wd.columns = ['胜出', '总数', '胜率%']
wd['胜率%'] = (wd['胜率%'] * 100).round(1)
wd = wd.reindex([d for d in weekday_order if d in wd.index])
print(wd)

print("\n" + "="*60)
print("【5】最佳时段 Top5 (胜率 > 60%，至少 3 笔交易)")
print("="*60)
hourly_all = df.groupby('hour')['is_win'].agg(['sum','count','mean'])
hourly_all.columns = ['胜出', '总数', '胜率']
hourly_all['胜率%'] = (hourly_all['胜率'] * 100).round(1)
good_hours = hourly_all[(hourly_all['胜率'] >= 0.60) & (hourly_all['总数'] >= 3)]
print(f"UTC 最佳交易时段 (胜率≥60%，样本≥3笔):")
if len(good_hours) > 0:
    for h, row in good_hours.sort_values('胜率%', ascending=False).iterrows():
        print(f"  UTC {h:02d}:00 — 胜率 {row['胜率%']}% ({int(row['胜出'])}/{int(row['总数'])}笔)")
else:
    print("  无符合条件的时段")

print("\n" + "="*60)
print("【6】最差时段 (胜率 < 45%，至少 3 笔交易)")
print("="*60)
bad_hours = hourly_all[(hourly_all['胜率'] < 0.45) & (hourly_all['总数'] >= 3)]
if len(bad_hours) > 0:
    for h, row in bad_hours.sort_values('胜率%').iterrows():
        print(f"  UTC {h:02d}:00 — 胜率 {row['胜率%']}% ({int(row['胜出'])}/{int(row['总数'])}笔)")
else:
    print("  无表现极差的时段")

print("\n" + "="*60)
print("【7】仅做 Put 的理论收益 (80% 收益率)")
print("="*60)
put_only = df[df['direction'] == 'Put']
put_wins = put_only['is_win'].sum()
put_total = len(put_only)
put_wr = put_wins / put_total
put_profit = put_wins * 80 - (put_total - put_wins) * 100
print(f"Put 总交易: {put_total} 笔 | 胜率: {put_wr*100:.2f}%")
print(f"每笔投入 $100, 净利润: ${put_profit:.0f}")
print(f"ROI (基准$10000): {put_profit/10000*100:.2f}%")
print(f"盈亏平衡线: 55.56%  -> {'[PASS] 超过平衡线' if put_wr > 0.5556 else '[FAIL] 未超过'}")

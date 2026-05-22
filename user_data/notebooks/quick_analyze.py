import zipfile, json, pandas as pd, numpy as np

RESULT = 'user_data/backtest_results/backtest-result-2026-05-21_21-51-42.zip'
with zipfile.ZipFile(RESULT) as z:
    jname = [f for f in z.namelist() if f.endswith('.json') and '_config' not in f][0]
    data = json.loads(z.read(jname))

strat = list(data['strategy'].keys())[0]
trades = data['strategy'][strat]['trades']
df = pd.DataFrame(trades)
df['is_win'] = (
    ((df['is_short']==False) & (df['close_rate']>df['open_rate'])) |
    ((df['is_short']==True)  & (df['close_rate']<df['open_rate']))
)
df['direction'] = df['is_short'].map({False:'Call', True:'Put'})

total = len(df)
wins = int(df['is_win'].sum())
wr = wins/total
payout = 0.80
exp = wr*payout - (1-wr)*1.0
profit = wins*80 - (total-wins)*100

print("Strategy:", strat)
print("Total trades:", total, "| Wins:", wins, "| Win Rate:", round(wr*100,2), "%")
print("Expectancy:", round(exp,4), "R")
print("Net Profit ($100/trade): $" + str(profit), "| ROI:", round(profit/10000*100,2), "%")
for d, g in df.groupby('direction'):
    dw = int(g['is_win'].sum())
    dt = len(g)
    print(d + ": " + str(dw) + "/" + str(dt) + " = " + str(round(dw/dt*100,2)) + "%")

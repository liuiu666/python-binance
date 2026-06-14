"""
30分钟定时拉取数据 + 自动重跑分析
"""
import subprocess, time, sys, os
from datetime import datetime, timezone

SCRIPT_DIR = "e:/python-binance"
FETCH_SCRIPT = os.path.join(SCRIPT_DIR, "tmp", "fetch_server_1s.py")
ULTIMATE_SCRIPT = os.path.join(SCRIPT_DIR, "tmp", "research_1s_ultimate.py")
FINAL_SCRIPT = os.path.join(SCRIPT_DIR, "tmp", "research_1s_final.py")

def run(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    return result.stdout + result.stderr

print(f"[{datetime.now(timezone.utc).isoformat()}] 启动30分钟轮询")
iteration = 0
while True:
    iteration += 1
    now = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"[轮询 #{iteration}] {now.isoformat()}")
    print(f"{'='*60}")
    
    # 1. 拉取数据
    print("[1/2] 拉取数据...")
    out = run(f'python "{FETCH_SCRIPT}"')
    lines = out.strip().split("\n")
    for l in lines[-5:]:
        print(f"  {l}")
    
    # 2. 重跑最终分析
    print("\n[2/2] 重跑最终分析...")
    out = run(f'python "{ULTIMATE_SCRIPT}"')
    lines = out.strip().split("\n")
    # 打印关键结果
    for l in lines:
        if any(kw in l for kw in ['WR=', '信号', 'CI:', 'P(WR', 'minFold', '推荐', 'Part 2', 'Part 3', 'W=600', 'W=120', '信号数', '胜率', 'PNL']):
            print(l)
    
    # 3. 等待30分钟
    print(f"\n下次拉取: 30分钟后")
    print(f"等待中...")
    time.sleep(1800)

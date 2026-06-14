"""Test unified_collector.py for ~30 seconds, then report results."""
import json, os, subprocess, sys, time

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
TEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collector_test_data")
COLLECTOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "py", "unified_collector.py")

DURATION = 35  # seconds

# Use a separate test directory
os.makedirs(TEST_DIR, exist_ok=True)

env = os.environ.copy()
env["DATA_DIR"] = TEST_DIR
env["COLLECTOR_BACKFILL_1M"] = "1"  # Only 1 day for test
env["COLLECTOR_BACKFILL_1S"] = "2"  # Only 2 min for test
env["COLLECTOR_LOCK_PORT"] = "39899"  # Different port for test

print(f"=== Unified Collector Test ===")
print(f"Duration: {DURATION}s")
print(f"Test data dir: {TEST_DIR}")
print(f"Collector: {COLLECTOR}")
print()

# Start collector as subprocess
proc = subprocess.Popen(
    [sys.executable, COLLECTOR],
    env=env,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
)

print(f"PID: {proc.pid}")
print("Collecting data...")
print()

# Wait with live output
start = time.time()
output_lines = []
while time.time() - start < DURATION:
    line = proc.stdout.readline()
    if line:
        output_lines.append(line.rstrip())
        if len(output_lines) % 10 == 0 or "Error" in line:
            print(f"  [{time.time()-start:.0f}s] {line.rstrip()}")
    if proc.poll() is not None:
        break
    time.sleep(0.1)

# Stop
proc.terminate()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()

# Read remaining output
remaining = proc.stdout.read()
if remaining:
    output_lines.extend(remaining.strip().splitlines())

print(f"\n=== Test Complete ({time.time()-start:.0f}s) ===\n")

# Check outputs
print("=== Output File Check ===")
expected_files = [
    ("current_price.json", "json"),
    ("btcusdt_1s_trades.csv", "csv"),
    ("btcusdt_1m.csv", "csv"),
    ("btcusdt_taker.csv", "csv"),
    ("btcusdt_lsratio.csv", "csv"),
    ("btcusdt_funding.csv", "csv"),
    ("collector_status.json", "json"),
]

all_ok = True
for fname, ftype in expected_files:
    fpath = os.path.join(TEST_DIR, fname)
    exists = os.path.exists(fpath)
    if exists:
        size = os.path.getsize(fpath)
        if ftype == "csv":
            with open(fpath, "r", encoding="utf-8") as f:
                rows = max(0, sum(1 for _ in f) - 1)
            info = f"{rows} rows, {size:,} bytes"
        elif ftype == "json":
            try:
                data = json.load(open(fpath, "r", encoding="utf-8"))
                info = f"valid JSON, {size:,} bytes"
            except Exception as e:
                info = f"INVALID JSON: {e}"
                all_ok = False
        status = "OK" if size > 0 else "EMPTY"
    else:
        info = "NOT FOUND"
        status = "FAIL"
        all_ok = False
    print(f"  {fname:<30} [{status:>5}] {info}")

# Check status file
status_path = os.path.join(TEST_DIR, "collector_status.json")
if os.path.exists(status_path):
    print(f"\n=== Collector Status ===")
    try:
        status = json.load(open(status_path, "r", encoding="utf-8"))
        tasks = status.get("tasks", {})
        for name, info in tasks.items():
            result = info.get("result", {})
            errors = info.get("errors", 0)
            last_ago = info.get("last_run_ago")
            err_str = f" errors={errors}" if errors > 0 else ""
            last_err = info.get("last_error")
            if last_err:
                err_str += f" [{last_err[:60]}]"
            print(f"  {name:<15} last_run={last_ago}s{err_str}  result={result}")
        
        rate = status.get("rate_usage", {})
        for mkt, usage in rate.items():
            print(f"  Rate {mkt}: {usage.get('used', 0)}/{usage.get('limit', '?')} ({usage.get('pct', 0):.1f}%)")
    except Exception as e:
        print(f"  Error reading status: {e}")

# Check price
price_path = os.path.join(TEST_DIR, "current_price.json")
if os.path.exists(price_path):
    try:
        price = json.load(open(price_path))
        print(f"\n=== Price ===")
        print(f"  BTC: ${price.get('price', 'N/A'):,.2f}")
        print(f"  Time: {price.get('time', 'N/A')}")
    except Exception:
        pass

# Check 1s CSV sample
csv_1s_path = os.path.join(TEST_DIR, "btcusdt_1s_trades.csv")
if os.path.exists(csv_1s_path):
    print(f"\n=== 1s Trades Sample (last 3 rows) ===")
    import pandas as pd
    try:
        df = pd.read_csv(csv_1s_path)
        print(f"  Columns: {list(df.columns)}")
        print(f"  Total rows: {len(df)}")
        if len(df) > 0:
            print(f"  Time range: {df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]}")
            print(f"  Last 3 rows:")
            for _, row in df.tail(3).iterrows():
                print(f"    {row['timestamp']}  O={row['open']:.1f} H={row['high']:.1f} "
                      f"L={row['low']:.1f} C={row['close']:.1f}  Vol={row['volume']:.4f} "
                      f"Trades={int(row['trades'])}  TakerRatio={row['taker_buy_sell_ratio']:.3f}")
    except Exception as e:
        print(f"  Error: {e}")

# Check 1m CSV sample
csv_1m_path = os.path.join(TEST_DIR, "btcusdt_1m.csv")
if os.path.exists(csv_1m_path):
    print(f"\n=== 1m Klines Sample (last 3 rows) ===")
    try:
        df = pd.read_csv(csv_1m_path)
        print(f"  Total rows: {len(df)}")
        if len(df) > 0:
            print(f"  Time range: {df['open_time'].iloc[0]} -> {df['open_time'].iloc[-1]}")
            for _, row in df.tail(3).iterrows():
                print(f"    {row['open_time']}  O={row['open']:.1f} H={row['high']:.1f} "
                      f"L={row['low']:.1f} C={row['close']:.1f}  Vol={row['volume']:.4f}")
    except Exception as e:
        print(f"  Error: {e}")

# Print errors from output
errors = [l for l in output_lines if "Error" in l or "error" in l.lower()]
if errors:
    print(f"\n=== Errors in Output ({len(errors)}) ===")
    for e in errors[-10:]:
        print(f"  {e}")

print(f"\n=== Summary ===")
print(f"  All files OK: {all_ok}")
print(f"  Output lines: {len(output_lines)}")

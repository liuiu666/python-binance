"""Small file and JSON helpers for the live signal service."""

import json
import os

import pandas as pd


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "item"):
        try:
            return json_safe(value.item())
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(json_safe(obj), ensure_ascii=False) + "\n")


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(json_safe(obj), f, ensure_ascii=False)
    os.replace(tmp, path)


def read_json_file(path, default=None):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return default
    return default


def tail_jsonl_rows(path, limit=2000, chunk_size=1024 * 1024):
    try:
        if not os.path.exists(path):
            return []
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            data = b""
            while pos > 0 and data.count(b"\n") <= limit:
                step = min(chunk_size, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
            lines = data.splitlines()[-limit:]
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line.decode("utf-8", "ignore")))
            except Exception:
                continue
        return rows
    except Exception:
        return []


def file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def csv_header(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.readline().strip().split(",")
    except OSError:
        return []


def csv_tail_rows(path, limit=120, chunk_size=65536):
    if not os.path.exists(path):
        return []
    header = csv_header(path)
    if not header:
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - chunk_size))
            text = f.read().decode("utf-8", errors="ignore")
        lines = [line for line in text.splitlines() if line.strip()]
        data_lines = [
            line for line in lines
            if line != ",".join(header) and not line.startswith(header[0] + ",")
        ][-limit:]
        rows = []
        for line in data_lines:
            cells = line.split(",")
            if len(cells) < len(header):
                continue
            rows.append({col: cells[idx] for idx, col in enumerate(header)})
        return rows
    except OSError:
        return []


def csv_tail_time(path, time_col):
    rows = csv_tail_rows(path, limit=5, chunk_size=8192)
    for row in reversed(rows):
        if time_col not in row:
            continue
        ts = pd.to_datetime(row[time_col], utc=True, format="ISO8601", errors="coerce")
        if pd.notna(ts):
            return ts
    return None


def age_status(now, name, path, time_col, max_age):
    ts = csv_tail_time(path, time_col)
    age = None if ts is None else now - ts
    reasons = []
    if not os.path.exists(path):
        reasons.append(f"{name}_missing")
    elif ts is None:
        reasons.append(f"{name}_unparseable")
    elif age > max_age:
        reasons.append(f"{name}_stale")
    return {
        "last_time": None if ts is None else str(ts),
        "age_seconds": None if age is None else round(age.total_seconds(), 3),
        "max_age_seconds": round(max_age.total_seconds(), 3),
        "reasons": reasons,
    }


def file_signature(path):
    try:
        st = os.stat(path)
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return None

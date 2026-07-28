from __future__ import annotations

import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
LIVE_BASE_URL = os.environ.get("LIVE_BASE_URL", "http://115.190.218.128:3000").rstrip("/")
SHANGHAI = ZoneInfo("Asia/Shanghai")
TODAY = datetime.now(SHANGHAI).strftime("%Y-%m-%d")

HISTORICAL_REPORT = ROOT / "tmp" / "v9_frequency_v13_true_core_20260717.json"
ARCHIVED_FORWARD_REPORT = ROOT / "tmp" / "forward_strategy_rejection_20260717.json"
FROZEN_MANIFEST = ROOT / "data" / "frozen_second_normal_reversal_v13.json"
OUT_JSON = ROOT / "tmp" / f"forward_strategy_rejection_{TODAY.replace('-', '')}.json"
OUT_MD = ROOT / "docs" / f"forward_strategy_rejection_{TODAY.replace('-', '')}.md"

LOCAL_FILES = {
    "1m": ROOT / "data" / "server_latest" / "btcusdt_1m.csv",
    "1s_trades": ROOT / "data" / "server_latest" / "btcusdt_1s_trades.csv",
    "orderbook_1s": ROOT / "data" / "server_latest" / "btcusdt_orderbook_1s.csv",
}

TARGET_STRATEGY_IDS = {
    "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V9",
    "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_SHADOW",
    "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_FREQ",
}
FORWARD_START_MS = int(datetime.fromisoformat("2026-07-15T11:09:37+00:00").timestamp() * 1000)
SERVER_ALIAS_ID = "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_FREQ"


def fetch_json(path: str) -> dict[str, Any]:
    url = f"{LIVE_BASE_URL}{path}"
    try:
        with urllib.request.urlopen(url, timeout=12) as response:
            payload = response.read().decode("utf-8")
        return {"ok": True, "url": url, "data": json.loads(payload)}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def parse_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:
            return int(value)
        if value > 1_000_000_000:
            return int(value * 1000)
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
        if number > 10_000_000_000:
            return int(number)
        if number > 1_000_000_000:
            return int(number * 1000)
    except ValueError:
        pass
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fmt_time(value: Any) -> str | None:
    ms = parse_ms(value)
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def summarize_trades(rows: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [r for r in rows if r.get("status") in {"won", "lost", "tie"}]
    wins = sum(1 for r in settled if r.get("status") == "won")
    losses = sum(1 for r in settled if r.get("status") == "lost")
    ties = sum(1 for r in settled if r.get("status") == "tie")
    decided = wins + losses
    pnl = round(sum(float(r.get("pnl") or 0) for r in settled), 2)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in sorted(settled, key=lambda r: parse_ms(r.get("openTime") or r.get("signalTime")) or 0):
        equity += float(row.get("pnl") or 0)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "total": len(rows),
        "settled": len(settled),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "winRate": round(wins / decided * 100, 2) if decided else None,
        "pnl": pnl,
        "maxDrawdown": round(max_drawdown, 2),
        "maxLossStreak": max_loss_streak(rows),
    }


def max_loss_streak(rows: list[dict[str, Any]]) -> int:
    streak = 0
    best = 0
    ordered = sorted(rows, key=lambda r: parse_ms(r.get("openTime") or r.get("signalTime")) or 0)
    for row in ordered:
        status = row.get("status")
        if status == "lost":
            streak += 1
            best = max(best, streak)
        elif status == "won":
            streak = 0
    return best


def independent_key(row: dict[str, Any]) -> tuple[str, str, str]:
    signal_ms = parse_ms(row.get("actionableTime") or row.get("signalTime") or row.get("openTime"))
    if signal_ms:
        signal_bucket = datetime.fromtimestamp(signal_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    else:
        signal_bucket = str(row.get("signalTime") or row.get("openTime") or "")
    return (
        str(row.get("direction") or ""),
        str(row.get("duration") or ""),
        signal_bucket,
    )


def dedupe_independent(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(independent_key(row), []).append(row)

    independent = []
    duplicates = []
    for group_rows in groups.values():
        ordered = sorted(group_rows, key=lambda r: parse_ms(r.get("openTime")) or 0)
        independent.append(ordered[0])
        if len(ordered) > 1:
            duplicates.append(ordered)
    independent.sort(key=lambda r: parse_ms(r.get("openTime") or r.get("signalTime")) or 0)
    return independent, duplicates


def tail_line(path: Path, chunk_size: int = 65536) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - chunk_size), os.SEEK_SET)
        data = handle.read().decode("utf-8-sig", errors="ignore")
    lines = [line for line in data.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def csv_latest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path)}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            header = handle.readline().strip()
        last = tail_line(path)
        headers = next(csv.reader([header]))
        values = next(csv.reader([last]))
        row = dict(zip(headers, values))
        time_col = next((c for c in ("timestamp", "time", "open_time", "datetime") if c in row), None)
        latest = row.get(time_col) if time_col else None
        return {
            "exists": True,
            "path": str(path),
            "size": path.stat().st_size,
            "timeColumn": time_col,
            "latest": latest,
            "latestShanghai": fmt_time(latest),
        }
    except Exception as exc:  # noqa: BLE001 - report audit failures instead of hiding them.
        return {"exists": True, "path": str(path), "error": str(exc)}


def local_coverage() -> dict[str, Any]:
    return {name: csv_latest(path) for name, path in LOCAL_FILES.items()}


def signal_snapshot(signal_payload: dict[str, Any], target_only: bool = True) -> list[dict[str, Any]]:
    if not signal_payload.get("ok"):
        return []
    data = signal_payload.get("data") or {}
    out = []
    for strategy_id, sig in data.items():
        if str(strategy_id).startswith("_") or not isinstance(sig, dict):
            continue
        if target_only and strategy_id not in TARGET_STRATEGY_IDS:
            continue
        out.append({
            "strategyId": strategy_id,
            "timeShanghai": sig.get("time_shanghai") or fmt_time(sig.get("time")),
            "signal": sig.get("signal"),
            "reason": sig.get("reason"),
            "detail": sig.get("signal_detail"),
            "confidence": sig.get("confidence"),
            "tradeEnabled": sig.get("trade_enabled"),
            "shadowOnly": sig.get("shadow_only"),
            "blocked": sig.get("safety_blocked") or sig.get("loss_density_blocked") or sig.get("data_health_blocked"),
        })
    return out


def build_report() -> dict[str, Any]:
    history = fetch_json("/api/trade-history?kind=shadow&mode=page&page=1&pageSize=300")
    data_health = fetch_json("/api/data-health")
    second_health = fetch_json("/api/second-data-health")
    orderbook_health = fetch_json("/api/orderbook-health")
    signal = fetch_json("/api/signal?source=codex-latest")
    historical = load_json(HISTORICAL_REPORT)
    archived_forward = load_json(ARCHIVED_FORWARD_REPORT)
    manifest = load_json(FROZEN_MANIFEST)
    alias_start_ms = parse_ms(manifest.get("shadowCollectionStart")) or FORWARD_START_MS

    rows = []
    alias_rows = []
    if history.get("ok"):
        all_rows = list((history.get("data") or {}).get("recent") or [])
        alias_rows = [
            row for row in all_rows
            if row.get("strategyId") == SERVER_ALIAS_ID
            and (parse_ms(row.get("openTime") or row.get("signalTime")) or 0) >= alias_start_ms
        ]
        rows = [
            row for row in all_rows
            if row.get("strategyId") in TARGET_STRATEGY_IDS
            and (parse_ms(row.get("openTime") or row.get("signalTime")) or 0) >= (
                alias_start_ms if row.get("strategyId") == SERVER_ALIAS_ID else FORWARD_START_MS
            )
        ]
    archived_rows = [
        row for row in (archived_forward.get("forwardTrades") or [])
        if row.get("strategyId") in TARGET_STRATEGY_IDS - {SERVER_ALIAS_ID}
        and (parse_ms(row.get("openTime") or row.get("signalTime")) or 0) >= FORWARD_START_MS
    ]
    rows.extend(archived_rows)
    independent, duplicates = dedupe_independent(rows)
    alias_independent, alias_duplicates = dedupe_independent(alias_rows)
    raw_summary = summarize_trades(rows)
    deduped_summary = summarize_trades(independent)
    alias_summary = summarize_trades(alias_independent)

    reasons: list[str] = []
    deployable = False
    if not rows:
        reasons.append("no V9/V13 shadow trades were recorded after the frozen forward boundary")
    if raw_summary["settled"] and raw_summary["winRate"] == 0:
        deployable = False
        reasons.append(f"cumulative raw shadow forward is 0/{raw_summary['settled']}")
    if deduped_summary["settled"] and deduped_summary["winRate"] == 0:
        deployable = False
        reasons.append(f"cumulative deduped independent forward is 0/{deduped_summary['settled']}")
    if duplicates:
        reasons.append("V9/V13 emitted the same-time same-direction idea, so strategy-family independence failed")
    if alias_summary["settled"] < 20:
        reasons.append(f"new frozen-alias forward sample is {alias_summary['settled']}/20")

    target_signals = signal_snapshot(signal)
    all_signals = signal_snapshot(signal, target_only=False)
    if not target_signals:
        reasons.append("V9/V13 are absent from the current live signal set, so forward collection is not active")

    coverage = local_coverage()
    latest_hf_days = {
        name: str(item.get("latestShanghai") or "")
        for name, item in coverage.items()
        if name in {"1s_trades", "orderbook_1s"}
    }
    if any(TODAY not in value for value in latest_hf_days.values()):
        deployable = False
        reasons.append("local high-frequency replay files do not fully cover the cumulative forward window")

    status = "REJECTED_FORWARD_FAIL"
    criteria = {
        "noHistoryOnlyPromotion": True,
        "rejectIfForwardDayWinRatePctEqualsZero": True,
        "rejectIfStrategyFamilyDuplicateSignals": True,
        "rejectIfLocalHighFrequencyReplayMissingToday": True,
        "minimumForwardTradesBeforeRealDeploy": 20,
        "minimumForwardWinRatePctBeforeRealDeploy": 63,
        "maximumForwardLossStreakBeforeRealDeploy": 2,
    }

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "generatedAtShanghai": datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
        "liveBaseUrl": LIVE_BASE_URL,
        "day": TODAY,
        "forwardBoundary": datetime.fromtimestamp(FORWARD_START_MS / 1000, timezone.utc).isoformat(),
        "serverAliasCollectionStart": datetime.fromtimestamp(alias_start_ms / 1000, timezone.utc).isoformat(),
        "historicalCandidate": historical.get("overall", {}),
        "historicalByBranch": historical.get("byBranch", {}),
        "forwardRaw": raw_summary,
        "forwardDedupedIndependent": deduped_summary,
        "newFrozenAliasIndependent": alias_summary,
        "newFrozenAliasDuplicateGroups": len(alias_duplicates),
        "duplicateGroups": [
            [
                {
                    "strategyId": row.get("strategyId"),
                    "direction": row.get("direction"),
                    "status": row.get("status"),
                    "openTimeShanghai": fmt_time(row.get("openTime")),
                    "signalTime": row.get("signalTime"),
                    "openPrice": row.get("openPrice"),
                    "closePrice": row.get("closePrice"),
                    "pnl": row.get("pnl"),
                }
                for row in group
            ]
            for group in duplicates
        ],
        "forwardTrades": [
            {
                "strategyId": row.get("strategyId"),
                "direction": row.get("direction"),
                "status": row.get("status"),
                "openTimeShanghai": fmt_time(row.get("openTime")),
                "signalTime": row.get("signalTime"),
                "openPrice": row.get("openPrice"),
                "closePrice": row.get("closePrice"),
                "pnl": row.get("pnl"),
            }
            for row in sorted(rows, key=lambda r: parse_ms(r.get("openTime") or r.get("signalTime")) or 0)
        ],
        "localCoverage": coverage,
        "liveHealth": {
            "data": (data_health.get("data") or {}) if data_health.get("ok") else data_health,
            "second": (second_health.get("data") or {}) if second_health.get("ok") else second_health,
            "orderbook": (orderbook_health.get("data") or {}) if orderbook_health.get("ok") else orderbook_health,
        },
        "signalSnapshot": target_signals,
        "liveSignalStrategyIds": [row["strategyId"] for row in all_signals],
        "criteria": criteria,
        "verdict": {
            "status": status,
            "deployable": deployable,
            "reasons": reasons,
            "action": "Do not enable real trading. Keep the verified V13 alias shadow-only, collect new forward data, and require local raw replay before promotion.",
        },
    }


def pct(value: Any) -> str:
    return "N/A" if value is None else f"{value}%"


def write_markdown(report: dict[str, Any]) -> None:
    historical = report["historicalCandidate"]
    raw = report["forwardRaw"]
    deduped = report["forwardDedupedIndependent"]
    alias = report["newFrozenAliasIndependent"]
    verdict = report["verdict"]
    signal_rows = report["signalSnapshot"]
    live_signal_ids = report.get("liveSignalStrategyIds", [])
    coverage = report["localCoverage"]

    lines = [
        f"# {report['day']} V9/V13 前向否决审计",
        "",
        f"生成时间：上海 `{report['generatedAtShanghai']}`",
        f"冻结证据起点：`{report['forwardBoundary']}`；服务器别名新采样起点：`{report['serverAliasCollectionStart']}`。",
        "",
        "## 结论",
        "",
        f"- 状态：`{verdict['status']}`",
        f"- 是否可上线：`{str(verdict['deployable']).lower()}`",
        "- 执行动作：V9/V13 不允许实盘上线；冻结 V13 仅保持影子运行并继续收集前向样本。",
        "",
        "拒绝原因：",
    ]
    lines.extend([f"- {reason}" for reason in verdict["reasons"]] or ["- 无"])
    lines.extend([
        "",
        "## 冻结后累计前向结果",
        "",
        f"- 原始 shadow：{raw['wins']}/{raw['settled']}，胜率 `{pct(raw['winRate'])}`，PnL `{raw['pnl']}U`，最大连亏 `{raw['maxLossStreak']}`。",
        f"- 去重后独立机会：{deduped['wins']}/{deduped['settled']}，胜率 `{pct(deduped['winRate'])}`，PnL `{deduped['pnl']}U`，最大连亏 `{deduped['maxLossStreak']}`。",
        f"- 本次参数校验后的新冻结影子：{alias['wins']}/{alias['settled']}，胜率 `{pct(alias['winRate'])}`；晋级门槛按这一栏累计到 20 单。",
        "- 去重口径：同一方向、同一期限、同一 actionable/signal 秒级时间，只算一个独立机会。",
        "",
        (
            f"冻结后累计样本中发现 `{len(report['duplicateGroups'])}` 组 V9/V13 同秒同方向重复信号；同族重复只算一个独立机会。"
            if report["duplicateGroups"]
            else "冻结后累计样本中没有发现 V9/V13 同族重复信号。"
        ),
        "",
        "## 历史候选不能再直接采用",
        "",
        f"- 历史报告：{historical.get('wins', 0)}/{historical.get('trades', 0)}，胜率 `{historical.get('winRate', 'N/A')}%`，PnL `{historical.get('pnlU', 'N/A')}U`。",
        f"- 历史最大回撤 `{historical.get('maxDrawdownU', 'N/A')}U`，历史最大连亏 `{historical.get('maxLossStreak', 'N/A')}`，日均 `{historical.get('tradesPerDay', 'N/A')}` 单。",
        "- 这些历史数字只能说明它曾经是候选；2026-07-17 的首次前向失败后，它不能作为可上线策略。",
        "",
        "## 本地数据覆盖",
        "",
    ])
    for name, item in coverage.items():
        lines.append(f"- `{name}`：最新上海时间 `{item.get('latestShanghai')}`，文件 `{item.get('path')}`。")
    lines.extend([
        "",
        "本地 `data/server_latest` 没有覆盖全部冻结后窗口，因此线上真实 shadow 结果与本地固定延迟回放必须分开报告。",
        "",
        "## 当前信号快照",
        "",
    ])
    for row in signal_rows:
        lines.append(
            f"- `{row['strategyId']}`：signal `{row['signal']}`，reason `{row['reason']}`，时间 `{row['timeShanghai']}`，tradeEnabled `{row['tradeEnabled']}`。"
        )
    if not signal_rows:
        lines.append("- V9/V13 当前均不在实时信号集合中，影子前向采集未启用。")
    if live_signal_ids:
        lines.append(f"- 当前服务器实际信号策略：`{', '.join(live_signal_ids)}`。")
    lines.extend([
        "",
        "## 后续验收标准",
        "",
        "- 不再用历史胜率单独决定上线。",
        "- 实盘前至少要有 20 笔新的前向 shadow 独立机会。",
        "- 前向胜率至少 63%，最大前向连亏不超过 2。",
        "- V9/V13 这类同族重复信号必须去重统计，不能把重复信号当频率。",
        "- 本地秒级成交和订单簿必须覆盖被验证日期，否则只能写“线上前向统计”，不能写“本地完整回放”。",
        "",
    ])
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    report = build_report()
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report)
    verdict = report["verdict"]
    print(json.dumps({
        "status": verdict["status"],
        "deployable": verdict["deployable"],
        "forwardRaw": report["forwardRaw"],
        "forwardDedupedIndependent": report["forwardDedupedIndependent"],
        "newFrozenAliasIndependent": report["newFrozenAliasIndependent"],
        "json": str(OUT_JSON),
        "markdown": str(OUT_MD),
    }, ensure_ascii=False, indent=2))
    return 0 if not verdict["deployable"] else 1


if __name__ == "__main__":
    sys.exit(main())

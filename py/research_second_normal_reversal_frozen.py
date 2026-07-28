"""Frozen, causal audit for the BTC 10-minute second-normal reversal family.

This script intentionally does not search parameters.  It replays the already
frozen V13 shadow configuration on a canonical set of local second/order-book
snapshots, deduplicates overlapping pulls, and separates all observations after
the declared freeze timestamp as holdout evidence.
"""

from __future__ import annotations

import hashlib
import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_v9_all_history_stability as replay  # noqa: E402
from current_v2_augmented_v9_core import AugmentedV9Rules  # noqa: E402


STRATEGY_ID = "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_SHADOW"
FREEZE_TIME = pd.Timestamp("2026-07-15T11:09:37Z")
DELAYS = (0, 5, 6, 10)
OUT_JSON = ROOT / "tmp" / "second_normal_reversal_frozen_audit.json"
OUT_TRADES = ROOT / "tmp" / "second_normal_reversal_frozen_trades.csv"
OUT_MD = ROOT / "docs" / "second_normal_reversal_frozen_audit_20260728.md"
MANIFEST_PATH = ROOT / "data" / "frozen_second_normal_reversal_v13.json"

# Prefer one representative pull per period.  The final two overlap around the
# freeze boundary on purpose; trade-level deduplication below removes repeats.
CANONICAL_DATASETS = {
    "tmp\\latest_pull_20260706_2130\\data",
    "tmp\\latest_live_pull_20260709_101331\\data",
    "tmp\\latest_pull_20260710_203217\\data",
    "tmp\\latest_pull_20260712_migration_fix\\data",
    "tmp\\daily_archive_20260713",
    "tmp\\frozen_position_forward",
    "data/server_latest",
}


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return clean(value.item())
    if pd.isna(value):
        return None
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def object_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def latest_forward_audit() -> tuple[dict[str, Any], Path | None]:
    paths = sorted((ROOT / "tmp").glob("forward_strategy_rejection_*.json"), reverse=True)
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8-sig"))
        summary = report.get("forwardDedupedIndependent")
        if isinstance(summary, dict):
            return report, path
    return {}, None


def find_variant(config: dict[str, Any]) -> dict[str, Any]:
    variants = config.get("strategyVariants") or []
    for row in variants:
        if row.get("id") == STRATEGY_ID:
            return row
    raise KeyError(f"Frozen strategy not found: {STRATEGY_ID}")


def union_hours(intervals: list[tuple[pd.Timestamp, pd.Timestamp]]) -> float:
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    merged: list[list[pd.Timestamp]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum((end - start).total_seconds() for start, end in merged) / 3600.0


def metric(frame: pd.DataFrame, delay: int, coverage_hours: float) -> dict[str, Any]:
    return replay.metric(frame, delay, max(coverage_hours, 1e-9))


def write_markdown(report: dict[str, Any]) -> None:
    holdout = report["delayMetrics"]
    combined = report["combinedObservedForward"]
    promotion = report["newFrozenAliasForward"]
    gates = report["holdoutGates"]
    lines = [
        "# 秒级正态反转冻结审计（2026-07-28）",
        "",
        "## 结论",
        "",
        f"- 状态：`{report['status']}`。",
        "- 真实交易：禁止。当前证据只允许继续研究，尚不允许晋级实盘。",
        f"- 本地冻结后共 `{holdout['6']['frozenHoldout']['trades']}` 单；与 7 月 17 日线上真实影子合并后为 `{combined['trades']}` 单，胜率 `{combined['winRate']}%`。",
        f"- 晋级只统计本次参数校验后的新冻结影子：当前 `{promotion.get('settled', 0)}/20`；本地回放和旧家族证据不得抵扣。",
        "",
        "## 固定方法",
        "",
        f"- 策略：`{report['strategyId']}`。",
        f"- 参数冻结时间：`{report['freezeTime']}`。",
        "- 未搜索、未调整参数；直接读取冻结 V13 影子配置和线上共享因果核心。",
        "- 代表性快照按 `time + signal + branch` 去重，再执行全策略 600 秒冷却。",
        f"- 去重后数据覆盖 `{report['method']['coverageHours']}` 小时，其中冻结后本地未见段 `{report['method']['frozenHoldoutHours']}` 小时。",
        "- 盈亏按每单 5U、盈利 +4U、亏损 -5U。",
        "",
        "## 冻结后本地固定延迟",
        "",
        "| 入场延迟 | 单数 | 胜率 | PnL | 最大回撤 | 最大连亏 |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for delay in DELAYS:
        row = holdout[str(delay)]["frozenHoldout"]
        lines.append(
            f"| {delay}s | {row['trades']} | {row['winRate']}% | {row['pnlU']}U | {row['maxDrawdownU']}U | {row['maxLossStreak']} |"
        )
    lines.extend([
        "",
        "这 5 单在四种延迟下均获胜，但样本不足；其中 4 单来自衰竭订单簿补充分支，不能据此宣称策略稳定。",
        "",
        "## 合并前向证据",
        "",
        f"- 本地固定回放：{combined['localWins']}/{combined['localTrades']}。",
        f"- 2026-07-17 线上真实影子：{combined['externalWins']}/{combined['externalTrades']}。",
        f"- 合并：{combined['wins']}/{combined['trades']}，胜率 `{combined['winRate']}%`，PnL `{combined['pnlU']}U`，最大连续亏损 `{combined['maxLossStreak']}`。",
        "- 线上两单使用真实 actionable/open 时间，不能伪装成某一个固定延迟档；因此只进入综合前向证据，不进入 0/5/6/10 秒表格。",
        "",
        "## 新冻结影子采样纪元",
        "",
        f"- 独立单数：`{promotion.get('settled', 0)}/20`。",
        f"- 胜率：`{promotion.get('winRate') if promotion.get('winRate') is not None else 'N/A'}%`。",
        f"- PnL：`{promotion.get('pnl', 0)}U`；最大回撤：`{promotion.get('maxDrawdown', 0)}U`；最大连亏：`{promotion.get('maxLossStreak', 0)}`。",
        "",
        "## 验收门槛",
        "",
        "| 门槛 | 要求 | 当前 | 通过 |",
        "|---|---:|---:|:---:|",
    ])
    for name, gate in gates.items():
        lines.append(f"| {name} | {gate['required']} | {gate['actual']} | {'是' if gate['pass'] else '否'} |")
    lines.extend([
        "",
        "## 复现",
        "",
        "```powershell",
        "python py/research_second_normal_reversal_frozen.py",
        "```",
        "",
        f"- 配置 SHA-256：`{report['frozenEvidence']['configSha256']}`",
        f"- 冻结策略 SHA-256：`{report['frozenEvidence']['variantSha256']}`",
        f"- 共享核心 SHA-256：`{report['frozenEvidence']['coreSha256']}`",
        "- 交易明细：`tmp/second_normal_reversal_frozen_trades.csv`",
        "- 机器报告：`tmp/second_normal_reversal_frozen_audit.json`",
        "",
        "## 下一步",
        "",
        "保持 `tradeEnabled=false`。服务器使用清单中登记的 V13 别名运行冻结参数；新采样纪元从 `2026-07-28T13:59:23.041Z` 开始，旧参数产生的历史交易不进入冻结验收。",
        "",
        "运行 `python py/manage_frozen_second_normal_shadow.py` 必须得到 `ready=true`；当前信号服务为 `shadow_only=true`、`trade_enabled=false`。累计至少 20 个去重后的新前向机会后再按同一冻结规则复核，期间不得依据输赢调整参数。",
        "",
    ])
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def run(reuse_existing: bool = False) -> dict[str, Any]:
    config_path = ROOT / "data" / "trade_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    cfg = find_variant(config)
    if cfg.get("tradeEnabled") is not False:
        raise RuntimeError("Frozen audit refuses to run unless tradeEnabled is false")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    variant_hash = object_sha256(cfg)
    core_hash = sha256(ROOT / "py" / "current_v2_augmented_v9_core.py")
    if variant_hash != manifest.get("variantSha256"):
        raise RuntimeError("Frozen V13 configuration changed; create a new research version instead of reusing this audit")
    if core_hash != manifest.get("sharedCoreSha256"):
        raise RuntimeError("Frozen shared core changed; create a new research version instead of reusing this audit")
    rules = AugmentedV9Rules.from_config(cfg)

    datasets = [
        item for item in replay.find_datasets()
        if item["name"] in CANONICAL_DATASETS
    ]
    missing = sorted(CANONICAL_DATASETS - {item["name"] for item in datasets})
    if missing:
        raise FileNotFoundError(f"Canonical datasets missing: {missing}")

    audits: list[dict[str, Any]] = []
    intervals: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    if reuse_existing and OUT_JSON.exists() and OUT_TRADES.exists():
        prior = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        audits = list(prior.get("datasetAudits") or [])
        trades = pd.read_csv(OUT_TRADES, encoding="utf-8-sig")
        trades["time"] = pd.to_datetime(trades["time"], utc=True)
        for audit in audits:
            if not audit.get("error"):
                intervals.append((pd.Timestamp(audit["start"]), pd.Timestamp(audit["end"])))
    else:
        trades_parts: list[pd.DataFrame] = []
        for item in datasets:
            part, audit = replay.replay_dataset(item, cfg, rules)
            audits.append(audit)
            if audit.get("error"):
                continue
            start, end = pd.Timestamp(audit["start"]), pd.Timestamp(audit["end"])
            intervals.append((start, end))
            if not part.empty:
                trades_parts.append(part)
        all_candidates = pd.concat(trades_parts, ignore_index=True) if trades_parts else pd.DataFrame()
        if all_candidates.empty:
            trades = all_candidates
        else:
            all_candidates["time"] = pd.to_datetime(all_candidates["time"], utc=True)
            all_candidates = all_candidates.sort_values(["time", "priority", "dataset"])
            all_candidates = all_candidates.drop_duplicates(
                subset=["time", "signal", "branch"], keep="first"
            )
            trades = replay.shared_cooldown(all_candidates, gap_sec=600)
            trades["period"] = "development_reused"
            trades.loc[trades["time"] > FREEZE_TIME, "period"] = "frozen_holdout"
            trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

    total_hours = union_hours(intervals)
    dev_intervals = [(start, min(end, FREEZE_TIME)) for start, end in intervals if start < FREEZE_TIME]
    holdout_intervals = [(max(start, FREEZE_TIME), end) for start, end in intervals if end > FREEZE_TIME]
    dev_hours = union_hours([(a, b) for a, b in dev_intervals if b > a])
    holdout_hours = union_hours([(a, b) for a, b in holdout_intervals if b > a])
    development = trades[trades["time"] <= FREEZE_TIME] if not trades.empty else trades
    holdout = trades[trades["time"] > FREEZE_TIME] if not trades.empty else trades

    delay_metrics = {
        str(delay): {
            "all": metric(trades, delay, total_hours),
            "developmentReused": metric(development, delay, dev_hours),
            "frozenHoldout": metric(holdout, delay, holdout_hours),
        }
        for delay in DELAYS
    }
    holdout6 = delay_metrics["6"]["frozenHoldout"]
    forward_report, external_path = latest_forward_audit()
    external = forward_report.get("forwardDedupedIndependent") or {}
    promotion = forward_report.get("newFrozenAliasIndependent") or {}
    external_trades = int(external.get("settled") or 0)
    external_wins = int(external.get("wins") or 0)
    combined_trades = holdout6["trades"] + external_trades
    combined_wins = holdout6["wins"] + external_wins
    combined_losses = combined_trades - combined_wins
    combined_win_rate = round(combined_wins / combined_trades * 100.0, 2) if combined_trades else None
    combined_pnl = round(holdout6["pnlU"] + float(external.get("pnl") or 0), 2)
    combined_loss_streak = max(holdout6["maxLossStreak"], int(external.get("maxLossStreak") or 0))
    combined_drawdown = max(holdout6["maxDrawdownU"], float(external.get("maxDrawdown") or 0))
    combined = {
        "trades": combined_trades,
        "wins": combined_wins,
        "losses": combined_losses,
        "winRate": combined_win_rate,
        "pnlU": combined_pnl,
        "maxDrawdownU": combined_drawdown,
        "maxLossStreak": combined_loss_streak,
        "localTrades": holdout6["trades"],
        "localWins": holdout6["wins"],
        "externalTrades": external_trades,
        "externalWins": external_wins,
        "externalAudit": str(external_path.relative_to(ROOT)) if external_path else None,
    }
    promotion_trades = int(promotion.get("settled") or 0)
    promotion_win_rate = promotion.get("winRate")
    promotion_drawdown = float(promotion.get("maxDrawdown") or 0)
    promotion_loss_streak = int(promotion.get("maxLossStreak") or 0)
    gates = {
        "minimumNewFrozenAliasTrades": {"required": 20, "actual": promotion_trades, "pass": promotion_trades >= 20},
        "minimumNewFrozenAliasWinRatePct": {"required": 63.0, "actual": promotion_win_rate, "pass": promotion_win_rate is not None and promotion_win_rate >= 63.0},
        "maximumNewFrozenAliasDrawdownU": {"required": 20.0, "actual": promotion_drawdown, "pass": promotion_drawdown <= 20.0},
        "maximumNewFrozenAliasLossStreak": {"required": 2, "actual": promotion_loss_streak, "pass": promotion_loss_streak <= 2},
        "allDelaysProfitable": {
            "required": True,
            "actual": all(delay_metrics[str(delay)]["frozenHoldout"]["pnlU"] > 0 for delay in DELAYS),
            "pass": all(delay_metrics[str(delay)]["frozenHoldout"]["pnlU"] > 0 for delay in DELAYS),
        },
    }
    accepted = all(gate["pass"] for gate in gates.values())

    report = {
        "strategyId": STRATEGY_ID,
        "status": "FORWARD_GATES_PASSED_REVIEW_REQUIRED" if accepted else "REJECTED_FORWARD_GATES",
        "realTradingAllowed": False,
        "freezeTime": FREEZE_TIME,
        "method": {
            "parameterSearch": False,
            "causalSharedCore": True,
            "dedupeKey": ["time", "signal", "branch"],
            "sharedCooldownSec": 600,
            "entryDelaysSec": list(DELAYS),
            "coverageHours": round(total_hours, 3),
            "developmentHours": round(dev_hours, 3),
            "frozenHoldoutHours": round(holdout_hours, 3),
            "canonicalDatasets": sorted(CANONICAL_DATASETS),
        },
        "frozenEvidence": {
            "configSha256": sha256(config_path),
            "variantSha256": variant_hash,
            "coreSha256": core_hash,
            "manifest": str(MANIFEST_PATH.relative_to(ROOT)),
            "rules": clean(rules.__dict__),
        },
        "datasetAudits": audits,
        "delayMetrics": delay_metrics,
        "combinedObservedForward": combined,
        "newFrozenAliasForward": promotion,
        "holdoutGates": gates,
        "verdict": (
            "All frozen forward gates passed; keep real trading disabled pending final review."
            if accepted
            else "Frozen forward gates are incomplete; keep shadow collection active and real trading disabled."
        ),
        "knownExternalForwardEvidence": {
            "independentTrades": external_trades,
            "wins": external_wins,
            "losses": external_trades - external_wins,
            "source": str(external_path.relative_to(ROOT)) if external_path else None,
            "includedInLocalMetrics": False,
        },
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(clean(report))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse-existing", action="store_true", help="Reuse the last raw replay to regenerate reports.")
    args = parser.parse_args()
    result = run(reuse_existing=args.reuse_existing)
    print(json.dumps(clean({
        "status": result["status"],
        "realTradingAllowed": result["realTradingAllowed"],
        "coverage": result["method"],
        "holdoutDelayMetrics": {
            delay: values["frozenHoldout"] for delay, values in result["delayMetrics"].items()
        },
        "combinedObservedForward": result["combinedObservedForward"],
        "gates": result["holdoutGates"],
        "output": str(OUT_JSON),
    }), ensure_ascii=False, indent=2))

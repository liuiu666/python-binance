from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtest import config_hash, fit_development_predict_frozen, guard_status
from src.experiment import load_config, prepare, write_json
from src.metrics import evaluate_predictions, success_assessment
from src.planner import plan_actions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the frozen holdout once after development is fixed.")
    parser.add_argument("--force", action="store_true", help="Allow a rerun and mark it non-pristine.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    reports = ROOT / "reports"
    guard = reports / "frozen_holdout_run.json"
    allowed, pristine = guard_status(guard, args.force)
    if not allowed:
        raise SystemExit("Frozen holdout was already run. Refusing rerun without --force.")
    spec_path = reports / "frozen_spec.json"
    if not spec_path.exists():
        raise SystemExit("Run run_experiment.py first; frozen_spec.json is missing.")
    spec = load_config(spec_path)
    config, samples, provenance = prepare(ROOT)
    if spec.get("configContentSha256") != config_hash(config):
        raise SystemExit("config.json changed after the frozen spec was selected")
    if spec.get("developmentEndExclusive") != config["development_end_exclusive"] or spec.get("frozenStart") != config["frozen_start"]:
        raise SystemExit("frozen boundaries differ from the selected spec")

    raw = fit_development_predict_frozen(samples, config)
    results = {}
    planned_frames = {}
    min_ev = float(spec["selectedMinEv"])
    for name, frame in raw.items():
        plan = plan_actions(
            frame["p_up"].to_numpy(), payout_rate=float(config["payout_rate"]), min_ev=min_ev,
            transition_confidence=frame.get("transition_confidence", None),
            state_support=frame.get("state_support", None),
            uncertainty_penalty=float(config["planner_uncertainty_penalty"]),
            sparse_state_penalty=float(config["planner_sparse_state_penalty"]),
        )
        planned = frame.reset_index(drop=True).copy()
        for column in plan.columns:
            if column != "p_up":
                planned[column] = plan[column].to_numpy()
        planned_frames[name] = planned
        results[name] = evaluate_predictions(planned, float(config["payout_rate"]), float(config["stake"]))

    assessment = success_assessment(results["world_model"], results["logistic"], config["success"])
    report = {
        "status": "pristine_frozen_holdout" if pristine else "non_pristine_forced_rerun",
        "pristine": pristine,
        "selectedSpec": spec,
        "provenance": provenance,
        "results": results,
        "success": assessment,
        "conclusion": "达到目标" if assessment["passed"] else "未达到目标",
    }
    write_json(reports / "frozen_holdout_report.json", report)
    for name, frame in planned_frames.items():
        frame.to_csv(reports / f"frozen_predictions_{name}.csv", index=False, encoding="utf-8-sig")
    write_json(guard, {"pristine": pristine, "conclusion": report["conclusion"], "configSha256": provenance["configSha256"]})
    print(json.dumps({"conclusion": report["conclusion"], "success": assessment, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

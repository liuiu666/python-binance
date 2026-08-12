from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtest import choose_frozen_spec, config_hash, evaluate_thresholds, walk_forward_predictions
from src.experiment import prepare, write_json


def main() -> None:
    reports = ROOT / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    config, samples, provenance = prepare(ROOT)
    predictions = walk_forward_predictions(samples, config)
    candidates = evaluate_thresholds(predictions, config)
    spec = choose_frozen_spec(candidates, config)
    spec["configContentSha256"] = config_hash(config)
    report = {
        "status": "development_only",
        "method": {
            "walkForward": "expanding monthly; two-year initial warmup",
            "holdoutUsed": False,
            "sampleStepMinutes": config["sample_step_minutes"],
            "horizonMinutes": config["horizon_minutes"],
            "payoutRate": config["payout_rate"],
        },
        "provenance": provenance,
        "candidates": candidates,
        "selectedSpec": spec,
    }
    write_json(reports / "development_report.json", report)
    write_json(reports / "frozen_spec.json", spec)
    for name, frame in predictions.items():
        frame.to_csv(reports / f"development_predictions_{name}.csv", index=False, encoding="utf-8-sig")
    print(json.dumps({"selectedSpec": spec, "provenance": provenance}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

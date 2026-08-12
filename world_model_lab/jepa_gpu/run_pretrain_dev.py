from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.pretrain import seed_everything, train
from src.runtime import create_model, dataset_pair, experiment_manifest, load_config, prepare_assets, torch_environment


def main() -> None:
    config = load_config(ROOT)
    environment = torch_environment()
    if not environment["cudaAvailable"]:
        raise SystemExit(f"CUDA unavailable: {environment}")
    assets = prepare_assets(ROOT, config)
    train_data, validation_data = dataset_pair(assets, config, config["data"]["dev_pretrain_end_exclusive"])
    seed_everything(int(config["training"]["seed"]))
    model = create_model(config)
    manifest = experiment_manifest(ROOT, config)
    report = train(model, train_data, validation_data, config, ROOT / "checkpoints" / "dev.pt",
                   manifest=manifest, resume=True)
    report["environment"] = environment
    (ROOT / "reports" / "dev_pretrain_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import torch
from torch.utils.data import DataLoader

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.pretrain import seed_everything, train, validate
from src.runtime import (
    create_model,
    dataset_pair,
    experiment_manifest,
    load_config,
    prepare_assets,
    torch_environment,
)


def main() -> None:
    config = copy.deepcopy(load_config(ROOT))
    training = config["training"]
    training.update(
        {
            "steps": 40,
            "warmup_steps": 5,
            "log_every": 10,
            "save_every": 20,
            "validation_batches": 10,
        }
    )
    environment = torch_environment()
    if not environment["cudaAvailable"]:
        raise SystemExit(f"CUDA unavailable: {environment}")

    assets = prepare_assets(ROOT, config)
    try:
        train_data, validation_data = dataset_pair(
            assets,
            config,
            config["data"]["dev_pretrain_end_exclusive"],
        )
        seed_everything(int(training["seed"]))
        model = create_model(config).cuda()
        validation_loader = DataLoader(
            validation_data,
            batch_size=int(training["batch_size"]),
            shuffle=False,
            num_workers=0,
            pin_memory=True,
            drop_last=True,
        )
        baseline = validate(
            model,
            validation_loader,
            torch.device("cuda"),
            int(training["validation_batches"]),
            training,
        )
        manifest = experiment_manifest(ROOT, config)
        checkpoint = ROOT / "checkpoints" / "smoke.pt"
        checkpoint.unlink(missing_ok=True)
        checkpoint.with_name(f"{checkpoint.name}.tmp").unlink(missing_ok=True)
        report = train(
            model,
            train_data,
            validation_data,
            config,
            checkpoint,
            manifest=manifest,
            resume=False,
        )
        output = {
            "environment": environment,
            "steps": int(training["steps"]),
            "baselineValidation": baseline,
            "trainedValidation": report["validation"],
            "training": report,
        }
        output_path = ROOT / "reports" / "smoke_pretrain_report.json"
        output_path.write_text(
            json.dumps(output, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        for asset in assets:
            asset.close_maps()


if __name__ == "__main__":
    main()

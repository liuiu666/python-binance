from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

import numpy as np
import torch

from src.losses import jepa_loss
from src.model import TemporalJEPA
from src.pretrain import load_checkpoint, restore_rng, save_checkpoint, seed_everything


def small_model() -> TemporalJEPA:
    return TemporalJEPA(
        input_channels=8,
        context_minutes=40,
        patch_minutes=10,
        target_end_offsets_minutes=[10, 30, 60],
        d_model=32,
        encoder_layers=1,
        predictor_layers=1,
        heads=4,
        ffn_dim=64,
        dropout=0.0,
    )


def train_step(
    model: TemporalJEPA,
    optimizer: torch.optim.Optimizer,
    context: torch.Tensor,
    targets: torch.Tensor,
    asset: torch.Tensor,
) -> None:
    model.train()
    optimizer.zero_grad(set_to_none=True)
    output = model(context, targets, asset)
    loss, _ = jepa_loss(output.predicted, output.target)
    loss.backward()
    optimizer.step()
    model.update_target(0.9)


class CheckpointTests(unittest.TestCase):
    def test_interrupted_resume_matches_continuous_training(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.pt"
            seed_everything(23)
            continuous = small_model()
            continuous_optimizer = torch.optim.AdamW(
                parameter
                for parameter in continuous.parameters()
                if parameter.requires_grad
            )
            scaler = torch.amp.GradScaler("cuda", enabled=False)
            context = torch.randn(4, 40, 8)
            targets = torch.randn(4, 3, 10, 8)
            asset = torch.tensor([0, 1, 0, 1])
            train_step(
                continuous,
                continuous_optimizer,
                context,
                targets,
                asset,
            )
            save_checkpoint(
                path,
                continuous,
                continuous_optimizer,
                scaler,
                step=0,
                sample_cursor=4,
                config={},
                manifest={"data": "same"},
                history=[],
            )
            train_step(
                continuous,
                continuous_optimizer,
                context,
                targets,
                asset,
            )

            resumed = small_model()
            resumed_optimizer = torch.optim.AdamW(
                parameter
                for parameter in resumed.parameters()
                if parameter.requires_grad
            )
            checkpoint = load_checkpoint(
                path,
                resumed,
                resumed_optimizer,
                scaler,
                expected_manifest={"data": "same"},
            )
            restore_rng(checkpoint)
            train_step(
                resumed,
                resumed_optimizer,
                context,
                targets,
                asset,
            )
            for name, expected in continuous.state_dict().items():
                torch.testing.assert_close(
                    resumed.state_dict()[name],
                    expected,
                    rtol=0,
                    atol=0,
                    msg=lambda message, name=name: f"{name}: {message}",
                )
            self.assertEqual(
                resumed_optimizer.state_dict()["param_groups"],
                continuous_optimizer.state_dict()["param_groups"],
            )
            for parameter_id, expected_state in continuous_optimizer.state_dict()["state"].items():
                actual_state = resumed_optimizer.state_dict()["state"][parameter_id]
                for key, expected in expected_state.items():
                    if torch.is_tensor(expected):
                        torch.testing.assert_close(
                            actual_state[key],
                            expected,
                            rtol=0,
                            atol=0,
                        )
                    else:
                        self.assertEqual(actual_state[key], expected)

    def test_manifest_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.pt"
            model = small_model()
            optimizer = torch.optim.AdamW(
                parameter
                for parameter in model.parameters()
                if parameter.requires_grad
            )
            scaler = torch.amp.GradScaler("cuda", enabled=False)
            save_checkpoint(
                path,
                model,
                optimizer,
                scaler,
                step=3,
                sample_cursor=128,
                config={"model": "test"},
                manifest={"data": "a"},
                history=[],
            )
            with self.assertRaisesRegex(ValueError, "manifest"):
                load_checkpoint(
                    path,
                    small_model(),
                    expected_manifest={"data": "b"},
                )

    def test_rng_states_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.pt"
            model = small_model()
            optimizer = torch.optim.AdamW(
                parameter
                for parameter in model.parameters()
                if parameter.requires_grad
            )
            scaler = torch.amp.GradScaler("cuda", enabled=False)
            seed_everything(19)
            save_checkpoint(
                path,
                model,
                optimizer,
                scaler,
                step=0,
                sample_cursor=64,
                config={},
                manifest={"data": "same"},
                history=[],
            )
            expected = (
                random.random(),
                float(np.random.random()),
                torch.rand(4),
            )
            checkpoint = load_checkpoint(
                path,
                small_model(),
                expected_manifest={"data": "same"},
            )
            restore_rng(checkpoint)
            actual = (
                random.random(),
                float(np.random.random()),
                torch.rand(4),
            )
            self.assertEqual(expected[0], actual[0])
            self.assertEqual(expected[1], actual[1])
            torch.testing.assert_close(expected[2], actual[2], rtol=0, atol=0)
            self.assertEqual(checkpoint["sampleCursor"], 64)
            self.assertFalse(path.with_name(f"{path.name}.tmp").exists())


if __name__ == "__main__":
    unittest.main()

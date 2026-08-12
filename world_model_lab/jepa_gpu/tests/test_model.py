from __future__ import annotations

import unittest

import torch

from src.losses import jepa_loss, representation_diagnostics
from src.model import TemporalJEPA, model_parameter_count


def small_model(dropout: float = 0.0) -> TemporalJEPA:
    return TemporalJEPA(input_channels=8, context_minutes=40, patch_minutes=10,
                        target_end_offsets_minutes=[10, 30, 60], d_model=32,
                        encoder_layers=2, predictor_layers=1, heads=4,
                        ffn_dim=64, dropout=dropout)


class ModelTests(unittest.TestCase):
    def test_shapes_gradients_and_target_freeze(self):
        model = small_model().train()
        context = torch.randn(4, 40, 8)
        targets = torch.randn(4, 3, 10, 8)
        asset = torch.tensor([0, 1, 0, 1])
        output = model(context, targets, asset)
        self.assertEqual(output.predicted.shape, (4, 3, 32))
        self.assertEqual(output.target.shape, (4, 3, 32))
        loss, _ = jepa_loss(output.predicted, output.target)
        loss.backward()
        self.assertTrue(any(p.grad is not None for p in model.context_encoder.parameters()))
        self.assertTrue(all(p.grad is None and not p.requires_grad for p in model.target_encoder.parameters()))

    def test_ema_updates_target_without_copying_exactly(self):
        model = small_model()
        target_before = next(model.target_encoder.parameters()).detach().clone()
        with torch.no_grad():
            next(model.context_encoder.parameters()).add_(1.0)
        model.update_target(0.5)
        target_after = next(model.target_encoder.parameters()).detach()
        self.assertFalse(torch.equal(target_before, target_after))
        self.assertFalse(torch.equal(target_after, next(model.context_encoder.parameters())))

    def test_diagnostics_and_parameter_budget(self):
        diag = representation_diagnostics(torch.randn(64, 32))
        self.assertGreater(diag["effectiveRank"], 1)
        counts = model_parameter_count(small_model())
        self.assertGreater(counts["total"], counts["trainable"])

    def test_teacher_stays_deterministic_with_dropout(self):
        model = small_model(dropout=0.3).train()
        context = torch.randn(4, 40, 8)
        targets = torch.randn(4, 3, 10, 8)
        asset = torch.tensor([0, 1, 0, 1])
        first = model(context, targets, asset)
        second = model(context, targets, asset)
        self.assertTrue(torch.equal(first.target, second.target))
        self.assertFalse(model.target_encoder.training)

    def test_variance_penalty_changes_online_gradient(self):
        base = 0.25 + torch.randn(8, 3, 32) * 1e-3
        target = torch.randn(8, 3, 32)
        without = base.clone().requires_grad_(True)
        with_penalty = base.clone().requires_grad_(True)
        loss_without, _ = jepa_loss(without, target, variance_weight=0.0)
        loss_with, diagnostics = jepa_loss(with_penalty, target, variance_weight=1.0)
        loss_without.backward()
        loss_with.backward()
        self.assertGreater(diagnostics["variancePenalty"], 0.0)
        self.assertFalse(torch.allclose(without.grad, with_penalty.grad))

    def test_context_anti_collapse_penalties_have_gradient(self):
        predicted = torch.randn(8, 3, 32, requires_grad=True)
        target = torch.randn(8, 3, 32)
        context = (0.25 + torch.randn(8, 32) * 1e-3).requires_grad_(True)
        loss, diagnostics = jepa_loss(
            predicted,
            target,
            context,
            context_variance_weight=1.0,
            context_covariance_weight=1.0,
        )
        loss.backward()
        self.assertGreater(diagnostics["contextVariancePenalty"], 0.0)
        self.assertGreater(diagnostics["contextCovariancePenalty"], 0.0)
        self.assertIsNotNone(context.grad)
        self.assertGreater(float(context.grad.abs().sum()), 0.0)


@unittest.skipUnless(torch.cuda.is_available(), "CUDA unavailable")
class CudaTests(unittest.TestCase):
    def test_amp_forward_backward(self):
        model = small_model().cuda().train()
        context = torch.randn(32, 40, 8, device="cuda")
        targets = torch.randn(32, 3, 10, 8, device="cuda")
        asset = torch.randint(0, 2, (32,), device="cuda")
        with torch.autocast(device_type="cuda", dtype=torch.float16):
            output = model(context, targets, asset)
            loss, _ = jepa_loss(output.predicted, output.target)
        loss.backward()
        self.assertTrue(torch.isfinite(loss))


if __name__ == "__main__":
    unittest.main()

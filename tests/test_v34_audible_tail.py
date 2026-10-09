"""Regression tests for 01778: speech is present in the nominal duration tail.

The contract is not merely to preserve the audio tensor: video must be extended
to the identical physical time or speech preservation would break lip sync.
"""
from __future__ import annotations

import torch

from ComfyUI_H3_Continuum_Join.v3 import assembly
from ComfyUI_H3_Continuum_Join.v3.driving_nodes import (
    H3ContinuumFinalizeDurationV34,
    V34_TIMELINE_EXACT,
    V34_TIMELINE_SAFE,
    V34_TIMELINE_STRICT,
)


def _input(tail_amplitude: float):
    frames = torch.linspace(0.0, 1.0, 345).view(345, 1, 1, 1).expand(345, 2, 2, 3).clone()
    pcm = torch.full((1, 2, 460000), 0.001, dtype=torch.float32)
    pcm[..., -12000:] = tail_amplitude
    plan = {
        "target_frames": 336,
        "decode_groups": [{"net_frames": 175}, {"net_frames": 170}],
        "preserve_final_frame": False,
    }
    return frames, {"waveform": pcm, "sample_rate": 32000}, plan


def _finalize(monkeypatch, amplitude: float, **kwargs):
    monkeypatch.setattr(assembly, "validate_assembly_plan", lambda plan: plan)
    images, audio, plan = _input(amplitude)
    return assembly.finalize_assembled_timeline(
        images=images, audio=audio, assembly_plan=plan,
        exact_total_duration=True, **kwargs,
    )


def test_active_terminal_pcm_keeps_synchronized_natural_duration(monkeypatch):
    images, audio, report = _finalize(monkeypatch, 0.25, preserve_audible_tail=True)
    assert images.shape[0] == 345
    assert audio["waveform"].shape[-1] == 460000
    assert torch.all(audio["waveform"][..., -12000:] == 0.25)
    assert "Audible-tail preservation" in report


def test_silent_padding_still_trims_to_exact_duration(monkeypatch):
    images, audio, report = _finalize(monkeypatch, 0.001, preserve_audible_tail=True)
    assert images.shape[0] == 336
    assert audio["waveform"].shape[-1] == 448000
    assert "Audible-tail preservation" not in report


def test_strict_exact_duration_remains_selectable(monkeypatch):
    images, audio, report = _finalize(monkeypatch, 0.25, preserve_audible_tail=False)
    assert images.shape[0] == 336
    assert audio["waveform"].shape[-1] == 448000
    assert "Audible-tail preservation" not in report


def test_legacy_v34_mode_defaults_to_safety_without_breaking_saved_workflows(monkeypatch):
    monkeypatch.setattr(assembly, "validate_assembly_plan", lambda plan: plan)
    assert H3ContinuumFinalizeDurationV34.INPUT_TYPES()["optional"]["timeline_mode"][1]["default"] == V34_TIMELINE_SAFE
    node = H3ContinuumFinalizeDurationV34()
    for mode, expected in (
        (V34_TIMELINE_SAFE, 345),
        (V34_TIMELINE_EXACT, 345),
        (V34_TIMELINE_STRICT, 336),
    ):
        images, audio, plan = _input(0.25)
        output_images, output_audio, _ = node.finalize(
            images=images, audio=audio, assembly_plan=plan, timeline_mode=mode,
        )
        assert len(output_images) == expected
        assert output_audio["waveform"].shape[-1] == round(expected / 24 * 32000)

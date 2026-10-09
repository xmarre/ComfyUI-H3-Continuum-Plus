"""Sustained exposure drift in a continuous generated shot, not a scene cut."""
import torch

from ComfyUI_H3_Continuum_Join.v3.video_tone_repair import (
    apply_tone_affines_in_place,
    estimate_sustained_tone_anchor,
)


def _clip(*, frames=24, darken=True, scene_cut_at=None, actor=True):
    y = torch.linspace(0.0, 1.0, 80)[:, None]
    x = torch.linspace(0.0, 1.0, 80)[None, :]
    background = torch.stack((
        (0.24 + 0.30 * x + 0.05 * y).expand(80, 80),
        (0.27 + 0.20 * y + 0.08 * x).expand(80, 80),
        (0.31 + 0.14 * x + 0.12 * y).expand(80, 80),
    ), dim=-1)
    prefix = background.repeat(8, 1, 1, 1)
    continuation = []
    for index in range(frames):
        base = background.clone()
        if scene_cut_at is not None and index >= scene_cut_at:
            base = (0.90 - background * 0.75).clamp(0.0, 1.0)
        else:
            if darken:
                base -= 0.007 + min(index, 7) * 0.0025
            if actor:
                # Deliberate subject motion and mouth/face changes are not
                # photometric evidence about the fixed room background.
                left = 15 + (index % 8)
                base[25:53, left : left + 19, :] = torch.tensor((0.55, 0.10, 0.22))
        continuation.append(base.clamp(0.0, 1.0))
    return prefix, torch.stack(continuation)


def test_background_anchor_reduces_multiframe_darkening_without_changing_prefix():
    prefix, continuation = _clip()
    coeff, report = estimate_sustained_tone_anchor(prefix, continuation, trim_frames=0)
    assert report["applied"] is True
    assert len(coeff) == len(continuation)
    assert report["early_drift"] > 0.0035

    buffer = torch.cat((prefix.clone(), continuation.clone()), dim=0)
    original_prefix = buffer[: len(prefix)].clone()
    apply_tone_affines_in_place(buffer, frame_start=len(prefix), affines=coeff)
    assert torch.equal(buffer[: len(prefix)], original_prefix)
    assert buffer.isfinite().all()
    # Top-right textured background does not contain the moving subject.
    before_error = (continuation[3:12, :20, 60:75] - prefix[0, :20, 60:75]).abs().mean()
    after_error = (buffer[len(prefix)+3:len(prefix)+12, :20, 60:75] - prefix[0, :20, 60:75]).abs().mean()
    assert after_error < before_error * 0.40


def test_scene_cut_stops_tone_repair_before_a_new_shot():
    prefix, continuation = _clip(frames=24, scene_cut_at=12)
    coeff, report = estimate_sustained_tone_anchor(prefix, continuation, trim_frames=0)
    assert report["applied"] is True
    assert len(coeff) == 12
    assert report["cut_after_frames"] == 12
    combined = torch.cat((prefix.clone(), continuation.clone()), dim=0)
    source_next_shot = combined[len(prefix) + 12:].clone()
    apply_tone_affines_in_place(combined, frame_start=len(prefix), affines=coeff)
    assert torch.equal(combined[len(prefix) + 12:], source_next_shot)


def test_no_drift_is_not_rewritten():
    prefix, continuation = _clip(darken=False, actor=False)
    coefficients, receipt = estimate_sustained_tone_anchor(prefix, continuation, trim_frames=0)
    assert coefficients is None
    assert receipt["reason"] == "no_qualified_sustained_darkening"


def test_cut_at_boundary_is_not_tone_aligned():
    prefix, continuation = _clip(darken=False, actor=False)
    continuation = (1.0 - continuation).clamp(0, 1)
    coeff, report = estimate_sustained_tone_anchor(prefix, continuation, trim_frames=0)
    assert coeff is None
    assert report["reason"] == "scene_cut_at_boundary"

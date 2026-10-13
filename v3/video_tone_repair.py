"""Bounded decoded-RGB photometric continuity for a *single continuous shot*.

This deliberately does not alter H3 denoising, its exact protected prefix,
camera geometry, or audio. It estimates a small per-frame affine RGB change
from *spatially static* background pixels, independently in each channel.

The diagnostic-only video_tone module measures all joins. This module is an
explicit experimental decoded correction; it never silently replaces the
normal Video Seam Auto path.
"""
from __future__ import annotations

import math

import torch

from .video_tone import _sample_rgb

TONE_POLICY = "decoded_static_background_color_anchor_v1"
MAX_SAMPLE_FRAMES = 96
MIN_STATIC_FRACTION = 0.16
MAX_STATIC_VARIATION = 0.016
MAX_SPATIAL_EDGE = 0.12
MAX_MATCH_DELTA = 0.065
SCENE_CUT_MEAN_DELTA = 0.12
MIN_DARKENING = 0.0035
MAX_AFFINE_GAIN_DELTA = 0.04
MAX_AFFINE_BIAS = 0.055


def _static_reference(previous: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    previous_small = _sample_rgb(previous[-6:])
    reference = previous_small.median(dim=0).values
    variation = (previous_small.amax(dim=0) - previous_small.amin(dim=0)).mean(dim=0)
    luma = (
        reference[0] * 0.2126
        + reference[1] * 0.7152
        + reference[2] * 0.0722
    )
    horizontal = torch.zeros_like(luma)
    vertical = torch.zeros_like(luma)
    horizontal[:, 1:-1] = (luma[:, 2:] - luma[:, :-2]).abs()
    vertical[1:-1, :] = (luma[2:, :] - luma[:-2, :]).abs()
    edges = torch.maximum(horizontal, vertical)
    mask = (
        (variation < MAX_STATIC_VARIATION)
        & (edges < MAX_SPATIAL_EDGE)
        & (reference.mean(dim=0) > 0.025)
        & (reference.mean(dim=0) < 0.975)
    )
    return reference, mask


def _background_pair(reference, candidate, static):
    delta = (reference - candidate).abs().mean(dim=0)
    match = static & (delta < MAX_MATCH_DELTA)
    fraction = float(match.float().mean().item())
    if fraction < MIN_STATIC_FRACTION:
        return None, fraction
    return match, fraction


def _fit_affine(reference, candidate, mask):
    """Iterated robust affine RGB fit on co-located static pixels only."""

    target = reference[:, mask].T.contiguous()
    source = candidate[:, mask].T.contiguous()
    keep = torch.ones(source.shape[0], dtype=torch.bool, device="cpu")
    slope = torch.ones(3)
    bias = torch.zeros(3)
    for _ in range(3):
        x = source[keep]
        y = target[keep]
        if x.shape[0] < 256:
            return None
        xm = x.mean(dim=0)
        ym = y.mean(dim=0)
        dx = x - xm
        dy = y - ym
        variance = (dx * dx).mean(dim=0)
        if bool((variance < 2e-4).any().item()):
            return None
        slope = ((dx * dy).mean(dim=0) / variance).clamp(
            1 - MAX_AFFINE_GAIN_DELTA,
            1 + MAX_AFFINE_GAIN_DELTA,
        )
        bias = (ym - slope * xm).clamp(-MAX_AFFINE_BIAS, MAX_AFFINE_BIAS)
        error = (source * slope + bias - target).abs().mean(dim=1)
        threshold = max(0.004, min(0.035, 1.7 * float(torch.quantile(error, 0.75).item())))
        keep = error <= threshold
    return torch.stack((slope, bias), dim=-1)


def estimate_sustained_tone_anchor(
    previous: torch.Tensor,
    current: torch.Tensor,
    *,
    trim_frames: int,
) -> tuple[torch.Tensor | None, dict]:
    """Return per-output-frame [RGB gain,bias] or None.

    A scene cut *at* the seam is rejected. A later cut terminates correction:
    no photometric correction is transferred across different compositions.
    If no cut occurs, correction is bounded to 96 frames and faded out.
    """
    receipt: dict = {
        "policy": TONE_POLICY,
        "applied": False,
        "reason": "unqualified",
        "authoritative_prefix_modified": False,
        "audio_modified": False,
        "spatial_transform_applied": False,
        "extra_vae_calls": 0,
        "extra_h3_nfe": 0,
    }
    trim = int(trim_frames)
    if (
        not torch.is_tensor(previous)
        or not torch.is_tensor(current)
        or previous.ndim != 4
        or current.ndim != 4
        or len(previous) < 6
        or trim < 0
        or trim >= len(current)
        or previous.shape[1:] != current.shape[1:]
        or previous.shape[-1] < 3
    ):
        receipt["reason"] = "unsupported_geometry"
        return None, receipt

    target_length = min(MAX_SAMPLE_FRAMES, len(current) - trim)
    try:
        reference, static = _static_reference(previous)
        post = _sample_rgb(current[trim : trim + target_length])
    except (ValueError, RuntimeError) as exc:
        receipt["reason"] = f"invalid_decoded_window:{type(exc).__name__}"
        return None, receipt
    if not bool(torch.isfinite(reference).all().item() and torch.isfinite(post).all().item()):
        receipt["reason"] = "nonfinite_input"
        return None, receipt

    if float((reference - post[0]).abs().mean().item()) >= SCENE_CUT_MEAN_DELTA:
        receipt["reason"] = "scene_cut_at_boundary"
        return None, receipt
    affines = []
    drifts = []
    confidence = []
    cut_at = None
    previous_frame = post[0]
    for index, frame in enumerate(post):
        if index > 0 and float((frame - previous_frame).abs().mean().item()) >= SCENE_CUT_MEAN_DELTA:
            cut_at = index
            break
        previous_frame = frame
        mask, fraction = _background_pair(reference, frame, static)
        if mask is None:
            # Do not extrapolate an unverified color transform into moving
            # backgrounds or unrelated compositions.
            break
        params = _fit_affine(reference, frame, mask)
        if params is None:
            break
        drift = ((reference - frame).mean(dim=0))[mask].median()
        drifts.append(float(drift.item()))
        confidence.append(float(fraction))
        affines.append(params)

    if len(affines) < 6:
        receipt.update(reason="insufficient_static_correspondence", supported_frames=len(affines))
        return None, receipt
    early_drift = float(torch.tensor(drifts[2:6]).median().item())
    if not math.isfinite(early_drift) or early_drift < MIN_DARKENING:
        receipt.update(reason="no_qualified_sustained_darkening", early_drift=early_drift)
        return None, receipt

    result = torch.stack(affines)
    # Suppress coefficient jitter caused by local subject motion or quantization.
    if len(result) >= 3:
        smoothed = result.clone()
        smoothed[1:-1] = (result[:-2] + result[1:-1] * 2 + result[2:]) / 4
        result = smoothed
    if cut_at is None and len(result) == MAX_SAMPLE_FRAMES:
        # Fade before the bounded analysis horizon to avoid a late one-frame
        # color discontinuity on a continuous but changing shot.
        fade_length = min(16, len(result))
        for index in range(fade_length):
            weight = (fade_length - 1 - index) / max(1, fade_length - 1)
            row = len(result) - fade_length + index
            result[row, :, 0] = 1 + (result[row, :, 0] - 1) * weight
            result[row, :, 1] *= weight
    receipt.update(
        applied=True,
        reason="static_background_drift",
        corrected_frames=len(result),
        cut_after_frames=cut_at,
        median_static_fraction=float(torch.tensor(confidence).median().item()),
        early_drift=early_drift,
        max_bias=float(result[..., 1].abs().max().item()),
        max_gain_delta=float((result[..., 0] - 1).abs().max().item()),
    )
    return result.contiguous(), receipt


def apply_tone_affines_in_place(
    image_buffer: torch.Tensor,
    *,
    frame_start: int,
    affines: torch.Tensor,
) -> None:
    """Correct *only new* decoded frames, in 4-frame slices, without new video buffers."""
    if not torch.is_tensor(affines) or tuple(affines.shape[1:]) != (3, 2):
        raise ValueError("tone affine coefficients must be [frames,3,2]")
    start = int(frame_start)
    length = int(affines.shape[0])
    if start < 0 or start + length > len(image_buffer):
        raise ValueError("tone correction exceeds the generated suffix")
    with torch.no_grad():
        for offset in range(0, length, 4):
            count = min(4, length - offset)
            block = image_buffer[start + offset : start + offset + count, :, :, :3]
            coeff = affines[offset : offset + count].to(device=block.device, dtype=block.dtype)
            gains = coeff[:, None, None, :, 0]
            biases = coeff[:, None, None, :, 1]
            block.mul_(gains).add_(biases).clamp_(0, 1)


__all__ = ["estimate_sustained_tone_anchor", "apply_tone_affines_in_place", "TONE_POLICY"]

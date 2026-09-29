"""Guarded decoded-space rigid gauge repair for Continuum boundaries.

The repair is intentionally downstream of H3 sampling and VAE decoding.  It is
used only when decoded trajectory evidence shows a coherent whole-frame rigid
impulse at a physical chunk boundary.  One constant translation is then applied
to the complete retained current chunk, preserving every current-chunk
frame-to-frame motion vector and avoiding a later release hitch.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Any

import torch
import torch.nn.functional as F

from .trajectory_diagnostics import measure_decoded_boundary_trajectory

DECODED_RIGID_GAUGE_POLICY = "decoded_chunk_rigid_gauge_v1"

MIN_RESPONSE = 6.0
MIN_EXCESS_MAGNITUDE_PX = 1.5
MAX_EXCESS_MAGNITUDE_PX = 24.0
MAX_ROI_DISAGREEMENT_PX = 1.5
MAX_ROI_MAGNITUDE_RATIO = 1.35
MIN_ROI_DIRECTION_COSINE = 0.97
MIN_FULL_IMPULSE_RATIO = 2.5
MIN_TRIAL_IMPROVEMENT = 0.65
MAX_TRIAL_ROI_REMAINDER_PX = 1.0
MAX_INTERNAL_MOTION_PERTURBATION_PX = 0.75
MIN_TRIAL_RESPONSE = 4.0
STRONG_SCALE_CONFIDENCE = 0.30
STRONG_SCALE_DELTA = 0.015
TRIAL_FRAMES = 6
APPLY_BATCH_FRAMES = 4
_ROIS = ("upper45", "full")


def _vector_norm(dx: float, dy: float) -> float:
    return math.hypot(float(dx), float(dy))


def _first_pair(fields: dict[str, Any]) -> tuple[float, float, float, bool]:
    dx = fields.get("pairwise_dx_px")
    dy = fields.get("pairwise_dy_px")
    response = fields.get("pairwise_response")
    clipped = fields.get("pairwise_clipped")
    if not all(isinstance(values, list) and values for values in (dx, dy, response, clipped)):
        raise ValueError("decoded rigid gauge requires a complete first-pair trajectory")
    return float(dx[0]), float(dy[0]), float(response[0]), bool(clipped[0])


def _pre_motion(fields: dict[str, Any]) -> tuple[float, float]:
    return float(fields["pre_median_dx_px"]), float(fields["pre_median_dy_px"])


def _coherent_scale_event(affine: dict[str, Any] | None) -> bool:
    if not isinstance(affine, dict):
        return False

    for axis in ("x", "y"):
        deltas: list[float] = []
        confidences: list[float] = []
        for roi in _ROIS:
            fields = affine.get(roi)
            if not isinstance(fields, dict):
                break
            boundary = float(fields[f"boundary_scale_{axis}"])
            baseline = float(fields[f"pre_median_scale_{axis}"])
            deltas.append(boundary - baseline)
            confidences.append(float(fields[f"boundary_scale_confidence_{axis}"]))
        if len(deltas) != 2:
            continue
        if min(confidences) < STRONG_SCALE_CONFIDENCE:
            continue
        signs = {1 if value > 0.0 else -1 if value < 0.0 else 0 for value in deltas}
        if len(signs) != 1 or 0 in signs:
            continue
        if min(abs(value) for value in deltas) >= STRONG_SCALE_DELTA:
            return True
    return False


def plan_decoded_rigid_gauge(
    trajectory: dict[str, Any],
    *,
    affine: dict[str, Any] | None,
    scene: dict[str, Any] | None,
) -> dict[str, Any]:
    """Plan a rigid decoded-chunk gauge correction from raw boundary evidence."""

    receipt: dict[str, Any] = {
        "policy": DECODED_RIGID_GAUGE_POLICY,
        "eligible": False,
        "reason": "not_evaluated",
        "whole_retained_segment": True,
        "temporal_release": False,
    }
    if not isinstance(scene, dict) or not bool(scene.get("scene_analysis_available")):
        receipt["reason"] = "scene_analysis_unavailable"
        return receipt
    if bool(scene.get("scene_cut")):
        receipt["reason"] = "scene_cut"
        return receipt
    if _coherent_scale_event(affine):
        receipt["reason"] = "coherent_scale_event_requires_nonrigid_model"
        return receipt

    observations: dict[str, Any] = {}
    excess_vectors: list[tuple[float, float]] = []
    excess_magnitudes: list[float] = []
    for roi in _ROIS:
        fields = trajectory.get(roi)
        if not isinstance(fields, dict):
            receipt["reason"] = "missing_required_roi"
            return receipt
        first_dx, first_dy, response, clipped = _first_pair(fields)
        pre_dx, pre_dy = _pre_motion(fields)
        if clipped:
            receipt["reason"] = f"{roi}_first_pair_clipped"
            return receipt
        if response < MIN_RESPONSE:
            receipt["reason"] = f"{roi}_low_response"
            return receipt

        excess_dx = first_dx - pre_dx
        excess_dy = first_dy - pre_dy
        magnitude = _vector_norm(excess_dx, excess_dy)
        observations[roi] = {
            "first_dx_px": first_dx,
            "first_dy_px": first_dy,
            "pre_median_dx_px": pre_dx,
            "pre_median_dy_px": pre_dy,
            "excess_dx_px": excess_dx,
            "excess_dy_px": excess_dy,
            "excess_magnitude_px": magnitude,
            "response": response,
            "clipped": clipped,
        }
        excess_vectors.append((excess_dx, excess_dy))
        excess_magnitudes.append(magnitude)

    receipt["observations"] = observations
    if min(excess_magnitudes) < MIN_EXCESS_MAGNITUDE_PX:
        receipt["reason"] = "boundary_rigid_impulse_below_floor"
        return receipt
    if max(excess_magnitudes) > MAX_EXCESS_MAGNITUDE_PX:
        receipt["reason"] = "boundary_rigid_impulse_over_bound"
        return receipt

    smaller = max(min(excess_magnitudes), 1.0e-9)
    magnitude_ratio = max(excess_magnitudes) / smaller
    if magnitude_ratio > MAX_ROI_MAGNITUDE_RATIO:
        receipt["reason"] = "boundary_rigid_impulse_roi_magnitude_disagreement"
        receipt["roi_magnitude_ratio"] = magnitude_ratio
        return receipt

    (upper_dx, upper_dy), (full_dx, full_dy) = excess_vectors
    disagreement = _vector_norm(upper_dx - full_dx, upper_dy - full_dy)
    if disagreement > MAX_ROI_DISAGREEMENT_PX:
        receipt["reason"] = "boundary_rigid_impulse_roi_vector_disagreement"
        receipt["roi_vector_disagreement_px"] = disagreement
        return receipt

    dot = upper_dx * full_dx + upper_dy * full_dy
    cosine = dot / max(excess_magnitudes[0] * excess_magnitudes[1], 1.0e-9)
    if cosine < MIN_ROI_DIRECTION_COSINE:
        receipt["reason"] = "boundary_rigid_impulse_roi_direction_disagreement"
        receipt["roi_direction_cosine"] = cosine
        return receipt

    full = trajectory["full"]
    subsequent = [
        _vector_norm(dx, dy)
        for dx, dy in zip(
            full["pairwise_dx_px"][1:4],
            full["pairwise_dy_px"][1:4],
            strict=False,
        )
    ]
    pre_full_dx, pre_full_dy = _pre_motion(full)
    baseline_motion = max(
        _vector_norm(pre_full_dx, pre_full_dy),
        float(median(subsequent)) if subsequent else 0.0,
        0.25,
    )
    impulse_ratio = excess_magnitudes[1] / baseline_motion
    if impulse_ratio < MIN_FULL_IMPULSE_RATIO:
        receipt["reason"] = "boundary_motion_is_not_an_isolated_rigid_impulse"
        receipt["full_impulse_ratio"] = impulse_ratio
        return receipt

    consensus_dx = float(median([upper_dx, full_dx]))
    consensus_dy = float(median([upper_dy, full_dy]))
    consensus_magnitude = _vector_norm(consensus_dx, consensus_dy)
    receipt.update(
        eligible=True,
        reason="coherent_decoded_rigid_boundary_impulse",
        roi_vector_disagreement_px=disagreement,
        roi_direction_cosine=cosine,
        roi_magnitude_ratio=magnitude_ratio,
        full_impulse_ratio=impulse_ratio,
        consensus_excess_dx_px=consensus_dx,
        consensus_excess_dy_px=consensus_dy,
        consensus_excess_magnitude_px=consensus_magnitude,
        proposed_correction_dx_px=-consensus_dx,
        proposed_correction_dy_px=-consensus_dy,
    )
    return receipt


def translate_decoded_frames(
    frames: torch.Tensor,
    *,
    dx: float,
    dy: float,
) -> torch.Tensor:
    """Translate IMAGE frames with edge replication; positive dx/dy moves content right/down."""

    if not torch.is_tensor(frames) or frames.ndim != 4:
        raise ValueError("decoded rigid gauge expects IMAGE [frames,H,W,C]")
    if not frames.is_floating_point():
        raise ValueError("decoded rigid gauge expects floating IMAGE data")
    if not math.isfinite(float(dx)) or not math.isfinite(float(dy)):
        raise ValueError("decoded rigid gauge translation must be finite")

    count, height, width, channels = map(int, frames.shape)
    if count < 1 or height < 2 or width < 2 or channels < 1:
        raise ValueError("decoded rigid gauge requires non-empty spatial frames")

    source = frames.permute(0, 3, 1, 2).to(dtype=torch.float32)
    theta = torch.zeros((count, 2, 3), device=source.device, dtype=source.dtype)
    theta[:, 0, 0] = 1.0
    theta[:, 1, 1] = 1.0
    theta[:, 0, 2] = -2.0 * float(dx) / float(width - 1)
    theta[:, 1, 2] = -2.0 * float(dy) / float(height - 1)
    grid = F.affine_grid(theta, source.shape, align_corners=True)
    translated = F.grid_sample(
        source,
        grid,
        mode="bilinear",
        padding_mode="border",
        align_corners=True,
    )
    return translated.permute(0, 2, 3, 1).to(dtype=frames.dtype)


def _score_trial(
    original: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    before_errors: dict[str, float] = {}
    after_errors: dict[str, float] = {}
    internal_motion_perturbation: dict[str, float] = {}

    for roi in _ROIS:
        original_fields = original[roi]
        candidate_fields = candidate[roi]
        target_dx, target_dy = _pre_motion(original_fields)
        original_dx, original_dy, _response, _clipped = _first_pair(original_fields)
        candidate_dx, candidate_dy, _candidate_response, _candidate_clipped = _first_pair(candidate_fields)
        before_errors[roi] = _vector_norm(original_dx - target_dx, original_dy - target_dy)
        after_errors[roi] = _vector_norm(candidate_dx - target_dx, candidate_dy - target_dy)

        perturbations: list[float] = []
        limit = min(
            4,
            len(original_fields["pairwise_dx_px"]),
            len(candidate_fields["pairwise_dx_px"]),
        )
        for index in range(1, limit):
            perturbations.append(
                _vector_norm(
                    float(candidate_fields["pairwise_dx_px"][index])
                    - float(original_fields["pairwise_dx_px"][index]),
                    float(candidate_fields["pairwise_dy_px"][index])
                    - float(original_fields["pairwise_dy_px"][index]),
                )
            )
        internal_motion_perturbation[roi] = max(perturbations, default=0.0)

    before_mean = sum(before_errors.values()) / len(before_errors)
    after_mean = sum(after_errors.values()) / len(after_errors)
    improvement = 1.0 - after_mean / max(before_mean, 1.0e-9)
    return {
        "before_boundary_error_px": before_errors,
        "after_boundary_error_px": after_errors,
        "before_mean_boundary_error_px": before_mean,
        "after_mean_boundary_error_px": after_mean,
        "mean_improvement_ratio": improvement,
        "internal_motion_perturbation_px": internal_motion_perturbation,
    }


def select_decoded_rigid_gauge_translation(
    previous_images: torch.Tensor,
    current_segment: torch.Tensor,
    *,
    boundary_global_frame: int,
    original_trajectory: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Trial both signs on a bounded prefix and accept only a measured improvement."""

    receipt: dict[str, Any] = {
        **plan,
        "accepted": False,
        "applied": False,
        "candidate_sign_trials": [],
    }
    if not bool(plan.get("eligible")):
        return receipt
    trial_frames = min(TRIAL_FRAMES, int(current_segment.shape[0]))
    if trial_frames < 3:
        receipt["reason"] = "insufficient_trial_frames"
        return receipt

    proposed_dx = float(plan["proposed_correction_dx_px"])
    proposed_dy = float(plan["proposed_correction_dy_px"])
    trials: list[tuple[float, float, dict[str, Any], dict[str, Any]]] = []
    for sign in (1.0, -1.0):
        dx = proposed_dx * sign
        dy = proposed_dy * sign
        candidate_frames = translate_decoded_frames(
            current_segment[:trial_frames],
            dx=dx,
            dy=dy,
        )
        candidate_trajectory = measure_decoded_boundary_trajectory(
            previous_images,
            candidate_frames,
            trim_frames=0,
            boundary_global_frame=int(boundary_global_frame),
            forward_frames=trial_frames,
            previous_transitions=4,
        )
        score = _score_trial(original_trajectory, candidate_trajectory)
        trials.append((dx, dy, candidate_trajectory, score))
        receipt["candidate_sign_trials"].append(
            {
                "dx_px": dx,
                "dy_px": dy,
                **score,
            }
        )

    selected_dx, selected_dy, selected_trajectory, selected_score = min(
        trials,
        key=lambda item: float(item[3]["after_mean_boundary_error_px"]),
    )
    response_ok = True
    unclipped = True
    for roi in _ROIS:
        _dx, _dy, response, clipped = _first_pair(selected_trajectory[roi])
        response_ok = response_ok and response >= MIN_TRIAL_RESPONSE
        unclipped = unclipped and not clipped

    roi_remainder_ok = all(
        float(selected_score["after_boundary_error_px"][roi])
        <= min(
            MAX_TRIAL_ROI_REMAINDER_PX,
            float(selected_score["before_boundary_error_px"][roi]) * 0.5,
        )
        for roi in _ROIS
    )
    internal_motion_stable = all(
        float(selected_score["internal_motion_perturbation_px"][roi])
        <= MAX_INTERNAL_MOTION_PERTURBATION_PX
        for roi in _ROIS
    )
    improved = float(selected_score["mean_improvement_ratio"]) >= MIN_TRIAL_IMPROVEMENT
    accepted = bool(
        response_ok
        and unclipped
        and roi_remainder_ok
        and internal_motion_stable
        and improved
    )
    receipt.update(
        selected_dx_px=selected_dx,
        selected_dy_px=selected_dy,
        selected_trajectory=selected_trajectory,
        selected_score=selected_score,
        trial_response_ok=response_ok,
        trial_unclipped=unclipped,
        trial_roi_remainder_ok=roi_remainder_ok,
        trial_internal_motion_stable=internal_motion_stable,
        accepted=accepted,
        reason=(
            "accepted_decoded_rigid_chunk_gauge"
            if accepted
            else "decoded_rigid_gauge_trial_validation_failed"
        ),
    )
    return receipt


def apply_decoded_rigid_gauge_in_place(
    image_buffer: torch.Tensor,
    *,
    frame_start: int,
    frame_stop: int,
    dx: float,
    dy: float,
    batch_frames: int = APPLY_BATCH_FRAMES,
) -> None:
    """Apply one constant decoded gauge to a retained segment with bounded workspace."""

    frame_start = int(frame_start)
    frame_stop = int(frame_stop)
    batch_frames = max(1, int(batch_frames))
    if frame_start < 0 or frame_stop <= frame_start or frame_stop > int(image_buffer.shape[0]):
        raise ValueError("decoded rigid gauge segment is outside the image buffer")

    for start in range(frame_start, frame_stop, batch_frames):
        stop = min(frame_stop, start + batch_frames)
        translated = translate_decoded_frames(
            image_buffer[start:stop],
            dx=float(dx),
            dy=float(dy),
        )
        image_buffer[start:stop].copy_(translated)


__all__ = [
    "DECODED_RIGID_GAUGE_POLICY",
    "apply_decoded_rigid_gauge_in_place",
    "plan_decoded_rigid_gauge",
    "select_decoded_rigid_gauge_translation",
    "translate_decoded_frames",
]

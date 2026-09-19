"""Bounded decoded-frame trajectory diagnostics for Continuum boundaries."""

from __future__ import annotations

import math
from statistics import median
from typing import Any

import torch
import torch.nn.functional as F

COMPARE_LONG_SIDE = 512
DEFAULT_FORWARD_FRAMES = 6
DEFAULT_PREVIOUS_TRANSITIONS = 4
_EPS = 1.0e-12


def _validate_images(frames: Any, label: str) -> None:
    if not torch.is_tensor(frames) or frames.ndim != 4 or int(frames.shape[-1]) < 3:
        raise ValueError(f"{label} must be IMAGE [T,H,W,C]")
    if min(map(int, frames.shape[:3])) < 1:
        raise ValueError(f"{label} must not be empty")


def _prepare_rgb(frames: torch.Tensor) -> tuple[torch.Tensor, float, float]:
    _validate_images(frames, "trajectory frames")
    rgb = frames[..., :3].detach().to(dtype=torch.float32).permute(0, 3, 1, 2)
    height, width = map(int, rgb.shape[-2:])
    scale = min(1.0, COMPARE_LONG_SIDE / float(max(height, width)))
    target_height = max(8, int(round(height * scale)))
    target_width = max(8, int(round(width * scale)))
    if (target_height, target_width) != (height, width):
        rgb = F.interpolate(
            rgb,
            size=(target_height, target_width),
            mode="bilinear",
            align_corners=False,
        )
    rgb = rgb.to(device="cpu").contiguous()
    if not bool(torch.isfinite(rgb).all().item()):
        raise ValueError("decoded trajectory frames contain NaN or Inf")
    return rgb, target_width / float(width), target_height / float(height)


def _parabolic_peak_offset(left: torch.Tensor, center: torch.Tensor, right: torch.Tensor) -> float:
    denominator = float((left - 2.0 * center + right).item())
    if abs(denominator) <= _EPS:
        return 0.0
    offset = 0.5 * float((left - right).item()) / denominator
    if not math.isfinite(offset):
        return 0.0
    return max(-0.5, min(0.5, offset))


def _phase_correlation_shift(
    left: torch.Tensor,
    right: torch.Tensor,
    *,
    roi_fraction: float,
    max_shift_x: int,
    max_shift_y: int,
) -> dict[str, float | bool]:
    if left.shape != right.shape or left.ndim != 3:
        raise ValueError("decoded trajectory phase correlation requires matching CxHxW frames")
    roi_fraction = float(roi_fraction)
    if not math.isfinite(roi_fraction) or not 0.0 < roi_fraction <= 1.0:
        raise ValueError("decoded trajectory roi_fraction must be in (0, 1]")

    height, width = map(int, left.shape[-2:])
    roi_height = max(4, min(height, int(round(height * roi_fraction))))
    left = left[..., :roi_height, :]
    right = right[..., :roi_height, :]
    left = left - left.mean(dim=(-2, -1), keepdim=True)
    right = right - right.mean(dim=(-2, -1), keepdim=True)

    window_y = torch.hann_window(roi_height, periodic=False, dtype=left.dtype)
    window_x = torch.hann_window(width, periodic=False, dtype=left.dtype)
    window = window_y[:, None] * window_x[None, :]
    left_fft = torch.fft.rfft2(left * window)
    right_fft = torch.fft.rfft2(right * window)
    cross = left_fft.conj() * right_fft
    cross = cross / cross.abs().clamp_min(_EPS)
    cross = cross.mean(dim=0)
    correlation = torch.fft.fftshift(
        torch.fft.irfft2(cross, s=(roi_height, width)).real
    )

    center_y, center_x = roi_height // 2, width // 2
    radius_y = min(int(max_shift_y), max(1, center_y - 1))
    radius_x = min(int(max_shift_x), max(1, center_x - 1))
    region = correlation[
        center_y - radius_y : center_y + radius_y + 1,
        center_x - radius_x : center_x + radius_x + 1,
    ]
    flat_index = int(torch.argmax(region).item())
    region_width = int(region.shape[1])
    peak_y = flat_index // region_width + center_y - radius_y
    peak_x = flat_index % region_width + center_x - radius_x
    sub_y = (
        _parabolic_peak_offset(
            correlation[peak_y - 1, peak_x],
            correlation[peak_y, peak_x],
            correlation[peak_y + 1, peak_x],
        )
        if 0 < peak_y < roi_height - 1
        else 0.0
    )
    sub_x = (
        _parabolic_peak_offset(
            correlation[peak_y, peak_x - 1],
            correlation[peak_y, peak_x],
            correlation[peak_y, peak_x + 1],
        )
        if 0 < peak_x < width - 1
        else 0.0
    )
    dx = float(peak_x - center_x) + sub_x
    dy = float(peak_y - center_y) + sub_y
    peak = float(correlation[peak_y, peak_x].item())
    response = peak / max(float(region.abs().mean().item()), _EPS)
    clipped = (
        peak_y in (center_y - radius_y, center_y + radius_y)
        or peak_x in (center_x - radius_x, center_x + radius_x)
    )
    return {"dx": dx, "dy": dy, "response": response, "clipped": bool(clipped)}


def _trajectory_for_roi(
    previous: torch.Tensor,
    current: torch.Tensor,
    *,
    roi_fraction: float,
    scale_x: float,
    scale_y: float,
    forward_frames: int,
    previous_transitions: int,
) -> dict[str, Any]:
    max_pair_shift = max(4, int(round(COMPARE_LONG_SIDE * 0.02)))
    pairwise_dx: list[float] = []
    pairwise_dy: list[float] = []
    pairwise_response: list[float] = []
    pairwise_clipped: list[bool] = []

    sequence = torch.cat((previous[-1:], current[:forward_frames]), dim=0)
    for index in range(int(sequence.shape[0]) - 1):
        shift = _phase_correlation_shift(
            sequence[index],
            sequence[index + 1],
            roi_fraction=roi_fraction,
            max_shift_x=max_pair_shift,
            max_shift_y=max_pair_shift,
        )
        pairwise_dx.append(float(shift["dx"]) / scale_x)
        pairwise_dy.append(float(shift["dy"]) / scale_y)
        pairwise_response.append(float(shift["response"]))
        pairwise_clipped.append(bool(shift["clipped"]))

    anchor_dx: list[float] = []
    anchor_dy: list[float] = []
    anchor_response: list[float] = []
    anchor_clipped: list[bool] = []
    anchor_shift = max_pair_shift * max(1, forward_frames)
    for index in range(min(forward_frames, int(current.shape[0]))):
        shift = _phase_correlation_shift(
            previous[-1],
            current[index],
            roi_fraction=roi_fraction,
            max_shift_x=anchor_shift,
            max_shift_y=anchor_shift,
        )
        anchor_dx.append(float(shift["dx"]) / scale_x)
        anchor_dy.append(float(shift["dy"]) / scale_y)
        anchor_response.append(float(shift["response"]))
        anchor_clipped.append(bool(shift["clipped"]))

    pre_dx: list[float] = []
    pre_dy: list[float] = []
    pre_response: list[float] = []
    start = max(1, int(previous.shape[0]) - int(previous_transitions))
    for index in range(start, int(previous.shape[0])):
        shift = _phase_correlation_shift(
            previous[index - 1],
            previous[index],
            roi_fraction=roi_fraction,
            max_shift_x=max_pair_shift,
            max_shift_y=max_pair_shift,
        )
        pre_dx.append(float(shift["dx"]) / scale_x)
        pre_dy.append(float(shift["dy"]) / scale_y)
        pre_response.append(float(shift["response"]))

    return {
        "pairwise_dx_px": pairwise_dx,
        "pairwise_dy_px": pairwise_dy,
        "pairwise_response": pairwise_response,
        "pairwise_clipped": pairwise_clipped,
        "pairwise_net_dx_px": float(sum(pairwise_dx)),
        "pairwise_net_dy_px": float(sum(pairwise_dy)),
        "anchor_dx_px": anchor_dx,
        "anchor_dy_px": anchor_dy,
        "anchor_response": anchor_response,
        "anchor_clipped": anchor_clipped,
        "anchor_final_dx_px": float(anchor_dx[-1]),
        "anchor_final_dy_px": float(anchor_dy[-1]),
        "pre_pairwise_dx_px": pre_dx,
        "pre_pairwise_dy_px": pre_dy,
        "pre_pairwise_response": pre_response,
        "pre_median_dx_px": float(median(pre_dx)) if pre_dx else 0.0,
        "pre_median_dy_px": float(median(pre_dy)) if pre_dy else 0.0,
        "post_first3_median_dx_px": float(median(pairwise_dx[:3])),
        "post_first3_median_dy_px": float(median(pairwise_dy[:3])),
    }


def measure_decoded_boundary_trajectory(
    previous_images: torch.Tensor,
    current_raw_images: torch.Tensor,
    *,
    trim_frames: int,
    boundary_global_frame: int,
    forward_frames: int = DEFAULT_FORWARD_FRAMES,
    previous_transitions: int = DEFAULT_PREVIOUS_TRANSITIONS,
) -> dict[str, Any]:
    """Measure decoded VAE framing motion around one retained chunk boundary."""

    _validate_images(previous_images, "previous decoded trajectory frames")
    _validate_images(current_raw_images, "current decoded trajectory frames")
    trim_frames = int(trim_frames)
    forward_frames = int(forward_frames)
    if int(previous_images.shape[0]) < 2:
        raise ValueError("decoded trajectory requires at least two previous frames")
    if trim_frames < 0 or trim_frames >= int(current_raw_images.shape[0]):
        raise ValueError("decoded trajectory trim position is outside current frames")
    available = int(current_raw_images.shape[0]) - trim_frames
    forward_frames = min(max(2, forward_frames), available)

    previous_start = max(
        0,
        int(previous_images.shape[0]) - max(2, int(previous_transitions) + 1),
    )
    previous_rgb, scale_x, scale_y = _prepare_rgb(previous_images[previous_start:])
    current_rgb, current_scale_x, current_scale_y = _prepare_rgb(
        current_raw_images[trim_frames : trim_frames + forward_frames]
    )
    if not math.isclose(scale_x, current_scale_x, rel_tol=0.0, abs_tol=1.0e-9) or not math.isclose(
        scale_y, current_scale_y, rel_tol=0.0, abs_tol=1.0e-9
    ):
        raise ValueError("decoded trajectory geometry changed across boundary")

    return {
        "trajectory_version": 1,
        "boundary_global_frame": int(boundary_global_frame),
        "current_trim_frame": trim_frames,
        "forward_frames": forward_frames,
        "comparison_long_side": COMPARE_LONG_SIDE,
        "upper45": _trajectory_for_roi(
            previous_rgb,
            current_rgb,
            roi_fraction=0.45,
            scale_x=scale_x,
            scale_y=scale_y,
            forward_frames=forward_frames,
            previous_transitions=previous_transitions,
        ),
        "full": _trajectory_for_roi(
            previous_rgb,
            current_rgb,
            roi_fraction=1.0,
            scale_x=scale_x,
            scale_y=scale_y,
            forward_frames=forward_frames,
            previous_transitions=previous_transitions,
        ),
    }

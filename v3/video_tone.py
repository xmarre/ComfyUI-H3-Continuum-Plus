"""Read-only photometric measurements around physical decoded video joins."""

from __future__ import annotations

import torch
import torch.nn.functional as F

LONG_SIDE = 192
PRE_FRAMES = 8
POST_FRAMES = 24
OVERLAP_FRAMES = 22


def _sample_rgb(frames: torch.Tensor) -> torch.Tensor:
    if not torch.is_tensor(frames) or frames.ndim != 4 or frames.shape[-1] < 3:
        raise ValueError("video tone measurements require IMAGE [T,H,W,C]")
    if min(frames.shape[:3]) < 1:
        raise ValueError("video tone measurement window is empty")
    height, width = map(int, frames.shape[1:3])
    scale = min(1.0, LONG_SIDE / max(height, width))
    size = (max(1, round(height * scale)), max(1, round(width * scale)))
    samples = []
    # Bound float32 conversion workspace even for high-resolution half images.
    for start in range(0, len(frames), 4):
        rgb = frames[start : start + 4, ..., :3].detach().to(dtype=torch.float32)
        rgb = rgb.permute(0, 3, 1, 2)
        if (height, width) != size:
            rgb = F.interpolate(rgb, size=size, mode="bilinear", align_corners=False)
        samples.append(rgb.to(device="cpu"))
    result = torch.cat(samples)
    if not bool(torch.isfinite(result).all().item()):
        raise ValueError("video tone measurement window contains NaN or Inf")
    return result


def _luma(rgb: torch.Tensor) -> torch.Tensor:
    return (rgb * rgb.new_tensor((0.2126, 0.7152, 0.0722))[None, :, None, None]).sum(1)


def _profile(rgb: torch.Tensor) -> dict:
    luma = _luma(rgb).flatten(1)
    quantiles = torch.quantile(luma, luma.new_tensor((0.05, 0.95)), dim=1)
    return {
        "rgb_mean": rgb.mean(dim=(2, 3)).tolist(),
        "rgb_std": rgb.std(dim=(2, 3), correction=0).tolist(),
        "luma_mean": luma.mean(1).tolist(),
        "luma_std": luma.std(1, correction=0).tolist(),
        "luma_p05": quantiles[0].tolist(),
        "luma_p95": quantiles[1].tolist(),
    }


def _paired(reference: torch.Tensor, candidate: torch.Tensor) -> dict:
    a = reference.movedim(1, -1).reshape(-1, 3)
    b = candidate.movedim(1, -1).reshape(-1, 3)
    centered_a = a - a.mean(0)
    centered_b = b - b.mean(0)
    variance = centered_a.square().mean(0)
    covariance = (centered_a * centered_b).mean(0)
    return {
        "sampled_rgb_difference_rms": float((b - a).square().mean().sqrt().item()),
        "sampled_rgb_mean_difference": (b.mean(0) - a.mean(0)).tolist(),
        "sampled_channel_gain": [
            float(covariance[i] / variance[i]) if variance[i] > 1.0e-12 else None
            for i in range(3)
        ],
    }


def measure_decoded_video_tone(
    previous: torch.Tensor,
    current: torch.Tensor,
    *,
    trim_frames: int,
    boundary_global_frame: int,
) -> dict:
    """Measure retained frames and plan-aligned duplicate overlap independently.

    Duplicate overlap is a decoder comparison only when the corresponding carried
    latents are identical. Pixel profiles also respond to composition and motion;
    they are observations, not an exposure/contrast correction or quality gate.
    """
    trim = int(trim_frames)
    if not 0 <= trim < len(current):
        raise ValueError("video tone trim must leave a retained frame")
    pre = _sample_rgb(previous[-PRE_FRAMES:])
    post = _sample_rgb(current[trim : trim + POST_FRAMES])
    if pre.shape[1:] != post.shape[1:]:
        raise ValueError("video tone measurement geometry differs across the join")
    result = {
        "policy": "decoded_video_tone_continuity_v1",
        "status": "measured",
        "boundary_global_frame": int(boundary_global_frame),
        "trim_frames": trim,
        "source": "raw_decode_before_current_video_patch",
        "sample_hw": list(pre.shape[-2:]),
        "pre_frame_offsets": list(range(-len(pre), 0)),
        "post_frame_offsets": list(range(len(post))),
        "pre": _profile(pre),
        "post": _profile(post),
        "production_gate": False,
        "production_images_modified": False,
        "extra_h3_nfe": 0,
        "extra_vae_calls": 0,
    }
    count = min(trim, len(previous), OVERLAP_FRAMES)
    overlap = {
        "requires_identical_carried_latents": True,
        "sampled_frames": count,
        "current_frame_start": trim - count,
        "native_h3_overlap_phase": trim >= 5 and (trim - 5) % 17 == 0,
    }
    result["plan_aligned_overlap"] = overlap
    if count == 0:
        overlap.update(status="not_evaluated", reason="no_decoded_overlap")
        return result
    left = _sample_rgb(previous[-count:])
    right = _sample_rgb(current[trim - count : trim])
    overlap.update(status="measured", window=_paired(left, right))
    # Native H3 can differ in the first five frames at a new decode origin and
    # in the final five without matching right context. Keep these domains apart.
    start = max(0, 5 - (trim - count))
    stop = count - 5
    overlap["native_h3_context_interior"] = (
        {"sampled_frames": stop - start, **_paired(left[start:stop], right[start:stop])}
        if stop > start
        else {"sampled_frames": 0, "status": "not_evaluated"}
    )
    overlap["right_context_tail"] = {
        "sampled_frames": min(count, 5),
        **_paired(left[-5:], right[-5:]),
    }
    return result

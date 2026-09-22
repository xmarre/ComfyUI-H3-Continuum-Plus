"""Exact temporal-VAE overlap reuse without an additional decode window.

At an exact Native Masked boundary the carried video prefix is bit-identical to
the previous physical group's latent tail. The current group is already decoded
with its generated suffix as right temporal context. Its last five protected
prefix frames are therefore the already-computed continuous-context version of
the preceding group's terminal five frames.

This module proves that latent relation before decode and permits assembly to
reuse only those already-decoded frames. It adds no sampler lifetime, H3 NFE or
VAE invocation.
"""

from __future__ import annotations

from typing import Any

import torch

from ..temporal import context_slots

VIDEO_DECODE_OVERLAP_CONTRACT = "h3_continuum_exact_video_decode_overlap_v1"
VIDEO_DECODE_REUSE_FRAMES = 5
# Native H3 temporal decode uses a 5-token stride with 2 future-overlap
# tokens. Reusing frames decoded by the current group is exact only when its
# protected prefix contains at least one complete 7-token decode window before
# the boundary. A 5-frame / 2-latent Fast prefix lacks that left context.
VIDEO_DECODE_MIN_PREFIX_LATENTS = 7


def annotate_video_decode_overlaps(
    entries: list[dict[str, Any]],
    groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Prove exact native-phase video overlap for each physical decode group."""

    if len(entries) != len(groups):
        raise ValueError("video decode-overlap entries and groups must have matching lengths")

    output: list[dict[str, Any]] = []
    previous_video: torch.Tensor | None = None
    for index, (entry, original_group) in enumerate(zip(entries, groups, strict=True)):
        group = dict(original_group)
        group.update(
            {
                "video_decode_overlap_contract": VIDEO_DECODE_OVERLAP_CONTRACT,
                "video_decode_overlap_verified": False,
                "video_decode_overlap_prefix_latents": 0,
                "video_decode_overlap_reuse_frames": 0,
                "video_decode_overlap_reason": "unverified",
            }
        )

        video = entry.get("video") if isinstance(entry, dict) else None
        if not torch.is_tensor(video) or video.ndim != 5:
            group["video_decode_overlap_reason"] = "invalid_video_latent"
            output.append(group)
            previous_video = video if torch.is_tensor(video) else None
            continue

        if index == 0:
            group["video_decode_overlap_reason"] = "initial_group"
            output.append(group)
            previous_video = video
            continue

        trim_frames = int(group.get("trim_frames", 0))
        if trim_frames <= 0:
            group["video_decode_overlap_reason"] = "no_protected_video_prefix"
            output.append(group)
            previous_video = video
            continue

        try:
            prefix_latents = int(context_slots(trim_frames))
        except (TypeError, ValueError):
            group["video_decode_overlap_reason"] = "non_native_overlap_phase"
            output.append(group)
            previous_video = video
            continue

        group["video_decode_overlap_prefix_latents"] = prefix_latents
        if prefix_latents < VIDEO_DECODE_MIN_PREFIX_LATENTS:
            group["video_decode_overlap_reason"] = "insufficient_left_decode_context"
        elif previous_video is None or not torch.is_tensor(previous_video) or previous_video.ndim != 5:
            group["video_decode_overlap_reason"] = "previous_video_latent_unavailable"
        elif (
            tuple(previous_video.shape[:2]) != tuple(video.shape[:2])
            or tuple(previous_video.shape[-2:]) != tuple(video.shape[-2:])
            or previous_video.dtype != video.dtype
            or previous_video.device != video.device
        ):
            group["video_decode_overlap_reason"] = "video_latent_geometry_dtype_or_device_changed"
        elif int(previous_video.shape[2]) < prefix_latents or int(video.shape[2]) <= prefix_latents:
            group["video_decode_overlap_reason"] = "insufficient_video_overlap_or_future_context"
        elif not torch.equal(
            previous_video[:, :, -prefix_latents:],
            video[:, :, :prefix_latents],
        ):
            group["video_decode_overlap_reason"] = "exact_video_prefix_not_bit_identical"
        else:
            group["video_decode_overlap_verified"] = True
            group["video_decode_overlap_reuse_frames"] = VIDEO_DECODE_REUSE_FRAMES
            group["video_decode_overlap_reason"] = "exact_carried_prefix"

        output.append(group)
        previous_video = video

    return output


def reuse_exact_video_decode_overlap(
    image_buffer: torch.Tensor,
    raw_images: torch.Tensor,
    *,
    group: dict[str, Any],
    frame_cursor: int,
) -> dict[str, Any]:
    """Reuse already-decoded right-context frames for one proven exact boundary."""

    receipt = {
        "contract": group.get("video_decode_overlap_contract"),
        "verified": bool(group.get("video_decode_overlap_verified", False)),
        "applied": False,
        "reuse_frames": 0,
        "prefix_latents": int(group.get("video_decode_overlap_prefix_latents", 0) or 0),
        "reason": str(group.get("video_decode_overlap_reason", "missing_proof")),
        "extra_h3_nfe": 0,
        "extra_sampler_lifetimes": 0,
        "extra_vae_windows": 0,
    }
    if receipt["contract"] != VIDEO_DECODE_OVERLAP_CONTRACT or not receipt["verified"]:
        return receipt

    reuse_frames = int(group.get("video_decode_overlap_reuse_frames", 0))
    trim_frames = int(group.get("trim_frames", 0))
    frame_cursor = int(frame_cursor)
    if reuse_frames != VIDEO_DECODE_REUSE_FRAMES:
        raise ValueError("verified video decode-overlap proof has an unsupported reuse width")
    if trim_frames < reuse_frames or frame_cursor < reuse_frames:
        raise ValueError("verified video decode-overlap proof is outside the decoded timeline")
    if not torch.is_tensor(image_buffer) or image_buffer.ndim != 4:
        raise ValueError("video decode-overlap reuse requires an IMAGE output buffer")
    if not torch.is_tensor(raw_images) or raw_images.ndim != 4:
        raise ValueError("video decode-overlap reuse requires decoded IMAGE input")
    if int(raw_images.shape[0]) < trim_frames:
        raise ValueError("verified video decode-overlap prefix exceeds decoded input")
    replacement = raw_images[trim_frames - reuse_frames : trim_frames]
    if tuple(replacement.shape[1:]) != tuple(image_buffer.shape[1:]):
        raise ValueError("video decode-overlap reuse geometry changed across the boundary")

    image_buffer[frame_cursor - reuse_frames : frame_cursor].copy_(
        replacement.to(device=image_buffer.device, dtype=image_buffer.dtype)
    )
    receipt.update(
        {
            "applied": True,
            "reuse_frames": reuse_frames,
            "reason": "reused_current_exact_prefix_with_right_context",
        }
    )
    return receipt

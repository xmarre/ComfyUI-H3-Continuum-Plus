"""Bounded lookahead for conditioning with statically known physical geometry."""
from __future__ import annotations

import os

import torch

from ..constants import CONTINUITY_FRAMES, V2_CONTINUITY_AUTO
from ..masked_continuation import CONTINUATION_NATIVE_MASKED
from .physical_sequence import continuation_geometry

MAX_PREFETCH_BYTES = 256 * (1 << 20)


def plan_next_geometry(*, current, continuity, continuation_method,
                       audio_continuity, driving_audio_active, chunks,
                       chunk_seconds, retained_frames, width, height, debug):
    """Plan only context lengths independent of the future generated latents.

    Motion-dependent Auto and insufficient known state capacity stay sequential.
    Motion scores themselves remain measured from the accepted output at runtime.
    """
    if os.environ.get("H3C_CONDITIONING_LOOKAHEAD", "1").lower() in {"0", "false", "off", "no"}:
        return None
    if continuity == V2_CONTINUITY_AUTO:
        if not (continuation_method == CONTINUATION_NATIVE_MASKED
                and audio_continuity and not driving_audio_active):
            return None
        context = 39
    else:
        context = CONTINUITY_FRAMES.get(continuity)
    if context is None or context > int(current.plan["state_capacity_frames"]):
        return None
    index = current.sequence_index + 1
    if index >= chunks:
        return None
    return continuation_geometry(
        sequence_index=index, chunks=chunks, chunk_seconds=chunk_seconds,
        retained_frames=retained_frames + current.net_frames, width=width,
        height=height, continuation_method=continuation_method, debug=debug,
        context_frames=context, clip_index=current.clip_index + 1,
    )


def bounded_cpu_conditioning(value):
    """Return an independent tensor/container snapshot, or decline oversized data.

    One lookahead result is retained at a time, with an explicit byte ceiling.
    No CUDA tensor or generated-output state is retained in the lookahead cache.
    """
    def size(item):
        if torch.is_tensor(item):
            return item.numel() * item.element_size()
        if isinstance(item, dict):
            return sum(size(v) for v in item.values())
        if isinstance(item, (tuple, list)):
            return sum(size(v) for v in item)
        return 0

    if size(value) > MAX_PREFETCH_BYTES:
        return None

    def copy(item):
        if torch.is_tensor(item):
            return item.detach().to(device="cpu", copy=True)
        if isinstance(item, dict):
            return {k: copy(v) for k, v in item.items()}
        if isinstance(item, list):
            return [copy(v) for v in item]
        if isinstance(item, tuple):
            return tuple(copy(v) for v in item)
        return item

    return copy(value)

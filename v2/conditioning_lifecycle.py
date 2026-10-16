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
    """Snapshot only known container and tensor types; never retain opaque GPU owners.

    Reject over-budget payloads without leaving a partially retained cache. Alias
    identity is maintained for tensors repeated inside reference metadata.
    """
    used = 0
    memo = {}

    def snapshot(item):
        nonlocal used
        if torch.is_tensor(item):
            if type(item) is not torch.Tensor:
                raise ValueError("unsupported tensor subclass")
            if id(item) in memo:
                return memo[id(item)]
            nbytes = item.numel() * item.element_size()
            if used + nbytes > MAX_PREFETCH_BYTES:
                raise ValueError("prefetch cap exceeded")
            used += nbytes
            result = item.detach().to(device="cpu", copy=True)
            memo[id(item)] = result
            return result
        if item is None or type(item) in (str, int, float, bool, bytes):
            return item
        if isinstance(item, (torch.dtype, torch.device)):
            return item
        if type(item) is dict:
            if id(item) in memo:
                return memo[id(item)]
            result = {}
            memo[id(item)] = result
            for key, val in item.items():
                if type(key) not in (str, int, float, bool, bytes):
                    raise ValueError("unsupported conditioning mapping key")
                result[key] = snapshot(val)
            return result
        if type(item) in (tuple, list):
            if id(item) in memo:
                return memo[id(item)]
            items = [snapshot(val) for val in item]
            result = tuple(items) if type(item) is tuple else items
            memo[id(item)] = result
            return result
        raise ValueError("unsupported conditioning payload type")

    try:
        return snapshot(value)
    except ValueError:
        return None

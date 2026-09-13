"""Runtime bridge for physical prompt transport.

The geometry itself remains owned by ``v2.sequence`` and existing Continuum
helpers. This module only turns one already-resolved physical descriptor into
Qwen conditioning and compact identity/diagnostic metadata.
"""
from __future__ import annotations

from typing import Any

import torch

from .h3_builder import encode_prompt_conditioning
from .physical_prompts import (
    compile_legacy_nominal,
    compile_physical_prompt,
    physical_metadata,
    physical_prompt_compiler_enabled,
    presentation_digest,
)


def build_presentation_contract(
    *,
    assets: Any,
    include_first: bool,
    include_last: bool,
    reference_assets: Any = None,
    reference_audio_source: Any = None,
    video_presentation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe only Qwen/reference presentation identity, never large tensors."""

    first = bool(include_first and getattr(assets, "first_image", None) is not None)
    last = bool(include_last and getattr(assets, "last_image", None) is not None)
    picture_offset = int(first) + int(last)
    reference_count = int(getattr(reference_assets, "count", 0) or 0)
    public_to_qwen = {
        str(index): index + picture_offset
        for index in range(1, reference_count + 1)
    }
    order = []
    if first:
        order.append("First Frame")
    if last:
        order.append("Last Frame")
    order.extend(f"Reference Image {index}" for index in range(1, reference_count + 1))
    contract: dict[str, Any] = {
        "include_first": first,
        "include_last": last,
        "first_frame_hash": str(getattr(assets, "first_frame_hash", "none")) if first else "none",
        "last_frame_hash": str(getattr(assets, "last_frame_hash", "none")) if last else "none",
        "reference_count": reference_count,
        "reference_combined_hash": str(getattr(reference_assets, "combined_hash", "none")) if reference_assets is not None else "none",
        "reference_image_hashes": list(getattr(reference_assets, "image_hashes", ()) or ()),
        "picture_offset": picture_offset,
        "public_to_qwen_picture": public_to_qwen,
        "presentation_order": order,
    }
    if reference_audio_source is not None:
        contract["reference_audio"] = dict(reference_audio_source.contract)
    if video_presentation is not None:
        contract["video"] = dict(video_presentation)
    return contract


def compile_invocation_prompt(
    plan: dict[str, Any], descriptor: Any, *, legacy_text: str, candidate: bool
):
    """Select physical Timeline compilation without changing opaque plan semantics.

    Fixed/List/migrated legacy plans remain per-invocation opaque instructions.
    In particular, terminal merged opaque plans must keep the existing paired
    ``legacy_text`` emitted by ``_terminal_pair_prompt`` rather than selecting
    only the first covered logical prompt.
    """

    source_kind = str((plan.get("source") or {}).get("kind", "legacy_logical"))
    if candidate and source_kind == "timeline":
        return compile_physical_prompt(plan, descriptor)
    return compile_legacy_nominal(plan, descriptor, text=legacy_text)


def encode_physical_prompt_conditioning(
    *,
    clip: Any,
    plan: dict[str, Any],
    descriptor: Any,
    legacy_text: str,
    assets: Any,
    include_first: bool,
    include_last: bool,
    reference_assets: Any = None,
    reference_audio_assets: Any = None,
    timeline_video_assets: Any = None,
    cache: dict[Any, Any] | None = None,
) -> tuple[list, Any, dict[str, Any], Any]:
    """Compile and encode exactly once for one resolved physical invocation."""

    cache = {} if cache is None else cache
    candidate = physical_prompt_compiler_enabled()
    compiled = compile_invocation_prompt(
        plan, descriptor, legacy_text=legacy_text, candidate=candidate
    )
    include_first_actual = bool(include_first and assets.first_image is not None)
    include_last_actual = bool(include_last and assets.last_image is not None)
    if not candidate:
        # Preserve PR #20's legacy conditioning-cache identity exactly.
        key: Any = (legacy_text, include_first_actual, include_last_actual)
    else:
        key = (
            compiled.compiler_version,
            compiled.text,
            include_first_actual,
            include_last_actual,
            presentation_digest(descriptor.presentation_contract),
        )
    if key not in cache:
        first_image = assets.first_image if include_first_actual else None
        last_image = assets.last_image if include_last_actual else None
        if reference_assets is not None:
            from ..reference import encode_reference_prompt

            cache[key] = encode_reference_prompt(
                clip,
                compiled.text,
                reference_assets,
                first_image=first_image,
                last_image=last_image,
                reference_audio_assets=reference_audio_assets,
                timeline_video_assets=timeline_video_assets,
            )
        else:
            cache[key] = encode_prompt_conditioning(
                clip,
                compiled.text,
                first_image=first_image,
                last_image=last_image,
                reference_audio_assets=reference_audio_assets,
                timeline_video_assets=timeline_video_assets,
            )
    # Reuse identity must be derivable without decoding Timeline Video. The
    # presentation contract therefore carries the deterministic source/adapter
    # selection identity; processed media hashes are diagnostic-only telemetry.
    metadata = physical_metadata(
        descriptor,
        compiled,
        timeline_video=descriptor.presentation_contract.get("video"),
    )
    return cache[key], compiled, metadata, key


def conditioning_telemetry(conditioning: Any) -> dict[str, Any]:
    """Read compact text/layout telemetry from the actual conditioning object."""

    result: dict[str, Any] = {"token_count": None, "tensor_shapes": []}
    if not isinstance(conditioning, list):
        return result
    layouts = []
    for item in conditioning:
        if not isinstance(item, (list, tuple)) or not item:
            continue
        tensor = item[0]
        if torch.is_tensor(tensor):
            shape = [int(value) for value in tensor.shape]
            result["tensor_shapes"].append(shape)
            if result["token_count"] is None and len(shape) >= 2:
                result["token_count"] = int(shape[-2])
        metadata = item[1] if len(item) > 1 and isinstance(item[1], dict) else {}
        compact = {}
        for key, value in metadata.items():
            lowered = str(key).lower()
            if not any(term in lowered for term in ("span", "layout", "offset")):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                compact[str(key)] = value
            elif isinstance(value, (list, tuple)) and len(value) <= 64 and all(
                isinstance(part, (str, int, float, bool)) or part is None for part in value
            ):
                compact[str(key)] = list(value)
        if compact:
            layouts.append(compact)
    if layouts:
        result["packed_layout"] = layouts
    return result

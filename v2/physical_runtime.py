"""Runtime bridge for physical prompt transport.

The geometry itself remains owned by ``v2.sequence`` and existing Continuum
helpers. This module only turns one already-resolved physical descriptor into
Qwen conditioning and compact identity/diagnostic metadata.
"""
from __future__ import annotations

import copy
from dataclasses import replace
from fractions import Fraction
import logging
from typing import Any

import torch

from .h3_builder import encode_prompt_conditioning
from .physical_prompts import (
    PhysicalPromptError,
    canonical_sha256,
    compile_legacy_nominal,
    compile_physical_prompt,
    fraction_string,
    physical_metadata,
    physical_prompt_compiler_enabled,
    presentation_digest,
)

LOG = logging.getLogger("h3_continuum_join")

# V3 keeps V2's strict inner-range parser, but changes the conditioning domain
# for exact Native Masked continuation. Authored instructions that belong only
# to the caller-owned protected prefix must not be presented as fresh generation
# instructions, because H3 timestamps are learned guidance rather than a hard
# per-frame routing mask. 00421 demonstrated the failure mode directly: the
# continuation began by replaying the earliest protected-prefix scene/dialogue.
_RUNTIME_PHYSICAL_COMPILER_VERSION = "physical_timeline_text_v3"
_EXACT_PREFIX_CONTEXT_BODY = (
    "Immutable carried continuation context. This interval already exists in the protected input "
    "and is not new generation. Do not restage or replay content from this protected interval "
    "after new generation begins."
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


def _timeline_plan_with_exact_prefix_context(
    plan: dict[str, Any], descriptor: Any
) -> tuple[dict[str, Any], tuple[Fraction, Fraction] | None]:
    """Replace only exact protected-prefix authored semantics with neutral context.

    The protected prefix is caller-owned and restored exactly at the sampler
    boundary. Repeating its scene/dialogue body in the Qwen timeline can only
    leak stale semantics into the newly generated suffix. Rather than parsing or
    deleting authored text after compilation, inject one highest-priority exact
    interval into a copied schema-2 source before normal interval resolution.
    The full physical local clock is preserved, so suffix timestamps do not move.
    """

    exact = getattr(descriptor, "exact_protected_interval", None)
    if exact is None:
        return plan, None
    if not isinstance(exact, (tuple, list)) or len(exact) != 2:
        raise PhysicalPromptError("physical exact protected interval is invalid")
    start_frame, end_frame = int(exact[0]), int(exact[1])
    if start_frame != int(getattr(descriptor, "global_start_frame", -1)):
        raise PhysicalPromptError("physical exact protected interval must begin at the physical window")
    if end_frame != int(getattr(descriptor, "retained_before", -1)):
        raise PhysicalPromptError("physical exact protected interval must end at retained_before")
    if end_frame <= start_frame or end_frame >= int(getattr(descriptor, "global_end_frame", -1)):
        raise PhysicalPromptError("physical exact protected interval leaves no generated suffix")

    source = plan.get("source")
    if not isinstance(source, dict) or source.get("kind") != "timeline":
        return plan, None

    fps = getattr(descriptor, "fps", None)
    if not isinstance(fps, Fraction) or fps <= 0:
        raise PhysicalPromptError("physical exact protected interval has invalid fps")
    start = Fraction(start_frame, 1) / fps
    end = Fraction(end_frame, 1) / fps

    rewritten = copy.deepcopy(plan)
    rewritten_source = dict(rewritten.get("source") or {})
    sections = list(rewritten_source.get("sections") or [])
    # A source-level override is intentional here: it participates in the same
    # atomic interval resolver as authored sections, while avoiding a second
    # timestamp parser or any post-render text surgery. Negative ordinal keeps
    # the synthetic transport interval out of authored source provenance.
    protected_context = {
        "kind": "override",
        "start": fraction_string(start),
        "end": fraction_string(end),
        "body": _EXACT_PREFIX_CONTEXT_BODY,
        "ordinal": -1_000_000_000,
        "header": "<exact-protected-prefix>",
    }
    rewritten_source["sections"] = [protected_context, *sections]
    rewritten["source"] = rewritten_source
    return rewritten, (start, end)


def _with_runtime_compiler_identity(
    compiled: Any,
    descriptor: Any,
    *,
    protected_interval: tuple[Fraction, Fraction] | None,
):
    if protected_interval is None:
        # V3 is a semantic version only for exact-prefix suppression. Initial
        # Timeline samples and guided-overlap continuations retain V2 identity
        # because their emitted text is byte-for-byte the V2 compiler result.
        return compiled

    diagnostics = tuple(compiled.diagnostics)
    start, end = protected_interval
    diagnostics += (
        {
            "level": "info",
            "code": "H3C-PT206",
            "message": "suppressed authored instructions inside the exact protected prefix so they cannot replay into the generated suffix",
            "global_start": fraction_string(start),
            "global_end": fraction_string(end),
        },
    )
    physical_hash = canonical_sha256(
        {
            "compiler_version": _RUNTIME_PHYSICAL_COMPILER_VERSION,
            "descriptor": descriptor.semantic_dict(),
            "text": compiled.text,
            "presentation_contract": descriptor.presentation_contract,
            "terminal_contract": descriptor.terminal_contract,
        }
    )
    return replace(
        compiled,
        compiler_version=_RUNTIME_PHYSICAL_COMPILER_VERSION,
        diagnostics=diagnostics,
        physical_conditioning_hash=physical_hash,
    )


def compile_invocation_prompt(
    plan: dict[str, Any], descriptor: Any, *, legacy_text: str, candidate: bool
):
    """Select physical Timeline compilation without changing opaque plan semantics.

    Fixed/List/migrated legacy plans remain per-invocation opaque instructions.
    In particular, terminal merged opaque plans must keep the existing paired
    ``legacy_text`` emitted by ``_terminal_pair_prompt`` rather than selecting
    only the first covered logical prompt.

    Timeline V3 additionally treats an exact Native Masked prefix as immutable
    context instead of fresh authored content. This preserves the full physical
    local clock while preventing protected-prefix scene/dialogue instructions
    from being replayed at the start of the generated suffix.
    """

    source_kind = str((plan.get("source") or {}).get("kind", "legacy_logical"))
    if candidate and source_kind == "timeline":
        runtime_plan, protected_interval = _timeline_plan_with_exact_prefix_context(plan, descriptor)
        compiled = compile_physical_prompt(runtime_plan, descriptor)
        return _with_runtime_compiler_identity(
            compiled,
            descriptor,
            protected_interval=protected_interval,
        )
    return compile_legacy_nominal(plan, descriptor, text=legacy_text)


def _validate_physical_timeline_video_assets(
    descriptor: Any,
    timeline_video_assets: Any,
) -> None:
    """Bind the encoded Timeline Video payload to the descriptor-authenticated RGB.

    ``make_normal_descriptor`` hashes the resized CPU RGB presentation before
    Qwen/reference encoding. The legacy sequence call surface still performs the
    VAE encode afterwards; this check prevents a second decode from silently
    presenting different media than the descriptor/storage identity records.
    """

    video = descriptor.presentation_contract.get("video")
    if not isinstance(video, dict):
        return
    if video.get("kind") != "timeline_video" or video.get("adapter") != "physical_window_v1":
        return
    expected_hash = str(video.get("processed_sha256", ""))
    expected_selection = video.get("selection_contract")
    if len(expected_hash) != 64 or not isinstance(expected_selection, dict):
        raise PhysicalPromptError(
            "physical Timeline Video presentation identity is incomplete"
        )
    if timeline_video_assets is None:
        raise PhysicalPromptError(
            "physical Timeline Video descriptor has no encoded presentation"
        )
    actual_hash = str(getattr(timeline_video_assets, "processed_sha256", ""))
    actual_selection = getattr(timeline_video_assets, "selection_contract", None)
    if actual_hash != expected_hash:
        raise PhysicalPromptError(
            "physical Timeline Video processed presentation changed between descriptor and encode"
        )
    if actual_selection != expected_selection:
        raise PhysicalPromptError(
            "physical Timeline Video selection contract changed between descriptor and encode"
        )


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
    source_kind = str((plan.get("source") or {}).get("kind", "legacy_logical"))
    compiled = compile_invocation_prompt(
        plan, descriptor, legacy_text=legacy_text, candidate=candidate
    )
    if candidate and source_kind == "timeline":
        diagnostic_codes = ",".join(
            str(item.get("code", ""))
            for item in compiled.diagnostics
            if isinstance(item, dict) and item.get("code")
        ) or "none"
        LOG.warning(
            "H3 Continuum physical prompt compiler active compiler=%s group=%s "
            "global_frames=[%d,%d) intervals=%d text_sha256=%s fallback=%s diagnostics=%s",
            compiled.compiler_version,
            str(getattr(descriptor, "group_id", "?")),
            int(getattr(descriptor, "global_start_frame", -1)),
            int(getattr(descriptor, "global_end_frame", -1)),
            len(compiled.contributing_intervals),
            compiled.text_sha256,
            compiled.fallback_status,
            diagnostic_codes,
        )
    _validate_physical_timeline_video_assets(descriptor, timeline_video_assets)
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
    video_presentation = descriptor.presentation_contract.get("video")
    timeline_video = (
        video_presentation
        if isinstance(video_presentation, dict)
        and video_presentation.get("kind") == "timeline_video"
        else None
    )
    metadata = physical_metadata(
        descriptor,
        compiled,
        timeline_video=timeline_video,
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

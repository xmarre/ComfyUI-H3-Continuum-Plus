"""Runtime bridge for physical prompt transport.

The geometry itself remains owned by ``v2.sequence`` and existing Continuum
helpers. This module only turns one already-resolved physical descriptor into
Qwen conditioning and compact identity/diagnostic metadata.
"""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from fractions import Fraction
import logging
from typing import Any

import torch

from .h3_builder import encode_prompt_conditioning
from .physical_prompts import (
    PhysicalPromptError,
    TERMINAL_PADDING_COMPILER_VERSION,
    canonical_sha256,
    compile_legacy_nominal,
    compile_physical_prompt,
    fraction_string,
    parse_fraction,
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


def _timeline_plan_for_logical_signal(
    plan: dict[str, Any], descriptor: Any
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Scope physical Timeline compilation to the current logical signal block.

    Continuum outer Timeline headers such as [0-7s] / [7-14s] are routing
    signals when they exactly match configured chunk boundaries. They select
    which authored body belongs to a logical chunk; physical overlap must not
    pull text from adjacent logical chunks.

    Fine-grained top-level timelines keep the existing physical-window behavior.
    Explicit [Chunk N] sections are always routing signals. Multi-logical merged
    invocations are left unchanged.
    """

    source = plan.get("source")
    if not isinstance(source, dict) or source.get("kind") != "timeline":
        return plan, None

    logical_indices = tuple(getattr(descriptor, "logical_indices", ()) or ())
    if len(logical_indices) != 1:
        return plan, None
    logical_index = int(logical_indices[0])
    if logical_index < 0:
        raise PhysicalPromptError("physical prompt logical index is invalid")

    chunk_seconds = parse_fraction(source.get("chunk_seconds", plan.get("chunk_seconds", 0)))
    if chunk_seconds <= 0:
        raise PhysicalPromptError("physical prompt chunk duration is invalid")
    chunk_index = logical_index + 1
    signal_start = Fraction(logical_index, 1) * chunk_seconds
    signal_end = signal_start + chunk_seconds

    sections = list(source.get("sections") or [])
    explicit = [
        item for item in sections
        if isinstance(item, dict)
        and item.get("kind") == "chunk"
        and int(item.get("chunk_index", -1)) == chunk_index
    ]
    exact_time = [
        item for item in sections
        if isinstance(item, dict)
        and item.get("kind") == "time"
        and parse_fraction(item.get("start")) == signal_start
        and parse_fraction(item.get("end")) == signal_end
    ]
    selected = explicit[0] if explicit else (exact_time[0] if exact_time else None)
    if selected is None:
        return plan, None

    rewritten = copy.deepcopy(plan)
    rewritten_source = dict(rewritten.get("source") or {})
    rewritten_source["sections"] = [copy.deepcopy(selected)]
    overrides = rewritten_source.get("overrides")
    if isinstance(overrides, dict):
        key = str(chunk_index)
        rewritten_source["overrides"] = {key: overrides[key]} if key in overrides else {}
    rewritten["source"] = rewritten_source
    return rewritten, {
        "chunk_index": chunk_index,
        "global_start": signal_start,
        "global_end": signal_end,
        "header": str(selected.get("header", f"<chunk:{chunk_index}>")),
    }


def _with_logical_signal_diagnostic(
    compiled: Any, scope: dict[str, Any] | None
):
    if scope is None:
        return compiled
    diagnostics = tuple(compiled.diagnostics) + (
        {
            "level": "info",
            "code": "H3C-PT208",
            "message": "scoped physical Timeline text to the current logical chunk signal; adjacent chunk bodies were not imported by overlap",
            "chunk_index": int(scope["chunk_index"]),
            "global_start": fraction_string(scope["global_start"]),
            "global_end": fraction_string(scope["global_end"]),
            "header": str(scope["header"]),
        },
    )
    return replace(compiled, diagnostics=diagnostics)

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

    This is the behavior validated by decoded-media run 00422. Do not expose an
    authored body that begins inside the exact prefix as active conditioning
    before fresh generation begins: 00509 showed that doing so can duplicate the
    boundary subject and delay the following authored cut.
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
    compiler_version = (
        TERMINAL_PADDING_COMPILER_VERSION
        if compiled.compiler_version == TERMINAL_PADDING_COMPILER_VERSION
        else _RUNTIME_PHYSICAL_COMPILER_VERSION
    )
    physical_hash = canonical_sha256(
        {
            "compiler_version": compiler_version,
            "descriptor": descriptor.semantic_dict(),
            "text": compiled.text,
            "presentation_contract": descriptor.presentation_contract,
            "terminal_contract": descriptor.terminal_contract,
        }
    )
    return replace(
        compiled,
        compiler_version=compiler_version,
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

    Exact logical Timeline signal headers such as [0-7s] / [7-14s] remain
    chunk-routing boundaries. Physical overlap may remap timestamps inside the
    selected chunk body, but it must not import adjacent chunk bodies.

    Timeline V3 additionally treats an exact Native Masked prefix as immutable
    context instead of fresh authored content. This preserves the full physical
    local clock while preventing protected-prefix scene/dialogue instructions
    from being replayed at the start of the generated suffix.
    """

    source_kind = str((plan.get("source") or {}).get("kind", "legacy_logical"))
    if candidate and source_kind == "timeline":
        scoped_plan, logical_scope = _timeline_plan_for_logical_signal(plan, descriptor)
        runtime_plan, protected_interval = _timeline_plan_with_exact_prefix_context(
            scoped_plan, descriptor
        )
        compiled = compile_physical_prompt(
            runtime_plan,
            descriptor,
            neutral_terminal_padding=True,
        )
        compiled = _with_runtime_compiler_identity(
            compiled,
            descriptor,
            protected_interval=protected_interval,
        )
        return _with_logical_signal_diagnostic(compiled, logical_scope)
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


def _bounded_hash_prefix(value: Any) -> str:
    text = str(value or "none")
    if text == "none":
        return text
    return text[:16]


def _physical_presentation_receipt(
    *,
    descriptor: Any,
    assets: Any,
    include_first_requested: bool,
    include_first_actual: bool,
    include_last_requested: bool,
    include_last_actual: bool,
    cache_hit: bool,
) -> dict[str, Any]:
    """Return bounded Qwen/presentation provenance without prompt or image data."""

    presentation = getattr(descriptor, "presentation_contract", None)
    presentation = presentation if isinstance(presentation, dict) else {}
    exact = getattr(descriptor, "exact_protected_interval", None)
    exact_prefix = None
    if isinstance(exact, (tuple, list)) and len(exact) == 2:
        exact_prefix = [int(exact[0]), int(exact[1])]
    reference_hashes = tuple(
        _bounded_hash_prefix(value)
        for value in (presentation.get("reference_image_hashes") or ())
    )
    return {
        "group": str(getattr(descriptor, "group_id", "?")),
        "logical_indices": tuple(
            int(value) for value in (getattr(descriptor, "logical_indices", ()) or ())
        ),
        "include_first_requested": bool(include_first_requested),
        "include_first_actual": bool(include_first_actual),
        "descriptor_include_first": bool(presentation.get("include_first", False)),
        "include_last_requested": bool(include_last_requested),
        "include_last_actual": bool(include_last_actual),
        "descriptor_include_last": bool(presentation.get("include_last", False)),
        "first_asset_present": bool(getattr(assets, "first_image", None) is not None),
        "first_asset_hash": _bounded_hash_prefix(
            getattr(assets, "first_frame_hash", "none")
        ),
        "reference_count": int(presentation.get("reference_count", 0) or 0),
        "reference_hashes": reference_hashes,
        "picture_offset": int(presentation.get("picture_offset", 0) or 0),
        "public_to_qwen_picture": dict(
            presentation.get("public_to_qwen_picture") or {}
        ),
        "presentation_order": tuple(
            str(value) for value in (presentation.get("presentation_order") or ())
        ),
        "cache_hit": bool(cache_hit),
        "exact_prefix": exact_prefix,
    }


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
    cache_hit = key in cache
    presentation_receipt = _physical_presentation_receipt(
        descriptor=descriptor,
        assets=assets,
        include_first_requested=include_first,
        include_first_actual=include_first_actual,
        include_last_requested=include_last,
        include_last_actual=include_last_actual,
        cache_hit=cache_hit,
    )
    LOG.info(
        "H3C-PT210 physical-presentation receipt group=%s logical=%s "
        "include_first_requested=%s include_first_actual=%s descriptor_include_first=%s "
        "include_last_requested=%s include_last_actual=%s descriptor_include_last=%s "
        "first_asset_present=%s first_asset_hash=%s reference_count=%d reference_hashes=%s "
        "picture_offset=%d public_to_qwen=%s presentation_order=%s cache_hit=%s exact_prefix=%s",
        presentation_receipt["group"],
        list(presentation_receipt["logical_indices"]),
        presentation_receipt["include_first_requested"],
        presentation_receipt["include_first_actual"],
        presentation_receipt["descriptor_include_first"],
        presentation_receipt["include_last_requested"],
        presentation_receipt["include_last_actual"],
        presentation_receipt["descriptor_include_last"],
        presentation_receipt["first_asset_present"],
        presentation_receipt["first_asset_hash"],
        presentation_receipt["reference_count"],
        list(presentation_receipt["reference_hashes"]),
        presentation_receipt["picture_offset"],
        presentation_receipt["public_to_qwen_picture"],
        list(presentation_receipt["presentation_order"]),
        presentation_receipt["cache_hit"],
        presentation_receipt["exact_prefix"],
    )
    if not cache_hit:
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


_SAFE_CONDITIONING_PATH_KEYS = {
    "audio_latent",
    "cross_attn",
    "latent",
    "minimax_keyframes",
    "minimax_refs",
    "minimax_token_tags",
    "model_conds",
}


def _conditioning_path_key(key: Any, index: int) -> str:
    name = str(key)
    if name in _SAFE_CONDITIONING_PATH_KEYS:
        return name
    return f"field[{index}]"


def _conditioning_tensor_descriptors(value: Any, *, path: str = "conditioning") -> list[dict[str, Any]]:
    if torch.is_tensor(value):
        return [
            {
                "path": path,
                "shape": [int(part) for part in value.shape],
                "dtype": str(value.dtype),
            }
        ]
    if isinstance(value, dict):
        descriptors: list[dict[str, Any]] = []
        keys = sorted(value, key=lambda item: str(item))
        for index, key in enumerate(keys):
            descriptors.extend(
                _conditioning_tensor_descriptors(
                    value[key],
                    path=f"{path}.{_conditioning_path_key(key, index)}",
                )
            )
        return descriptors
    if isinstance(value, (list, tuple)):
        descriptors = []
        for index, item in enumerate(value):
            descriptors.extend(
                _conditioning_tensor_descriptors(
                    item,
                    path=f"{path}[{index}]",
                )
            )
        return descriptors
    return []


def conditioning_telemetry(conditioning: Any) -> dict[str, Any]:
    """Read compact text/layout telemetry from the actual conditioning object."""

    result: dict[str, Any] = {
        "token_count": None,
        "tensor_shapes": [],
        "tensor_dtypes": [],
        "all_tensors": _conditioning_tensor_descriptors(conditioning),
    }
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
            result["tensor_dtypes"].append(str(tensor.dtype))
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



_PHYSICAL_VALIDATION_MANIFEST_SCHEMA = 1
_PHYSICAL_INTERVAL_RECEIPT_LIMIT = 64


def _bounded_interval_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    exact_names = {
        "kind",
        "logical_index",
        "chunk_index",
        "global_start_frame",
        "global_end_frame",
        "global_start",
        "global_end",
        "local_start",
        "local_end",
        "source_start",
        "source_end",
    }
    result: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)
        lowered = name.lower()
        structural = (
            name in exact_names
            or lowered.endswith("_index")
            or lowered.endswith("_frame")
            or lowered.endswith("_frames")
            or lowered.endswith("_start")
            or lowered.endswith("_end")
        )
        if not structural:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[name] = item
        elif (
            isinstance(item, (list, tuple))
            and len(item) <= 8
            and all(isinstance(part, (str, int, float, bool)) or part is None for part in item)
        ):
            result[name] = list(item)
    return result


def physical_validation_manifest(
    metadata: dict[str, Any],
    conditioning: Any,
) -> dict[str, Any]:
    """Return bounded physical-timeline and conditioning provenance.

    Prompt text, image data and conditioning tensors are intentionally excluded.
    Full contributing-interval identity is retained by digest even when the
    human-readable structural preview is capped.
    """

    if not isinstance(metadata, dict):
        raise PhysicalPromptError("physical validation metadata must be a mapping")
    descriptor = metadata.get("descriptor")
    compiled = metadata.get("compiled")
    if not isinstance(descriptor, dict) or not isinstance(compiled, dict):
        raise PhysicalPromptError("physical validation metadata is incomplete")

    intervals = compiled.get("contributing_intervals")
    if not isinstance(intervals, list):
        intervals = []
    diagnostics = compiled.get("diagnostics")
    if not isinstance(diagnostics, list):
        diagnostics = []
    terminal_padding_interval = None
    terminal_audio_guard_interval = None
    for item in diagnostics:
        if not isinstance(item, dict):
            continue
        if item.get("code") == "H3C-PT217":
            terminal_padding_interval = [
                str(item.get("global_start", "")),
                str(item.get("global_end", "")),
            ]
        elif item.get("code") == "H3C-PT219":
            terminal_audio_guard_interval = [
                str(item.get("global_start", "")),
                str(item.get("global_end", "")),
            ]
    structural_intervals = [
        _bounded_interval_receipt(item)
        for item in intervals[:_PHYSICAL_INTERVAL_RECEIPT_LIMIT]
    ]
    telemetry = conditioning_telemetry(conditioning)
    shapes = list(telemetry.get("tensor_shapes") or ())
    dtypes = list(telemetry.get("tensor_dtypes") or ())
    if len(shapes) != len(dtypes):
        raise PhysicalPromptError("conditioning telemetry shape/dtype accounting diverged")
    tensors = list(telemetry.get("all_tensors") or ())

    return {
        "schema": _PHYSICAL_VALIDATION_MANIFEST_SCHEMA,
        "descriptor_digest": str(metadata.get("descriptor_digest", "")),
        "physical_conditioning_hash": str(metadata.get("physical_conditioning_hash", "")),
        "group": str(descriptor.get("group_id", "?")),
        "logical_indices": list(descriptor.get("logical_indices") or ()),
        "global_frame_interval": [
            int(descriptor.get("global_start_frame", -1)),
            int(descriptor.get("global_end_frame", -1)),
        ],
        "exact_protected_interval": descriptor.get("exact_protected_interval"),
        "retained_suffix_interval": descriptor.get("retained_suffix_interval"),
        "terminal_padding_interval": terminal_padding_interval,
        "terminal_audio_guard_interval": terminal_audio_guard_interval,
        "compiler_version": str(compiled.get("compiler_version", "")),
        "text_sha256": str(compiled.get("text_sha256", "")),
        "interval_count": len(intervals),
        "intervals_sha256": canonical_sha256(intervals),
        "intervals": structural_intervals,
        "intervals_truncated": len(intervals) > _PHYSICAL_INTERVAL_RECEIPT_LIMIT,
        "conditioning": {
            "token_count": telemetry.get("token_count"),
            "tensors": tensors,
            "packed_layout": telemetry.get("packed_layout", []),
        },
    }



def log_physical_validation_manifest(
    metadata: dict[str, Any],
    conditioning: Any,
) -> dict[str, Any]:
    """Log one bounded manifest for the final conditioning passed to sampling."""

    manifest = physical_validation_manifest(metadata, conditioning)
    LOG.info(
        "H3C-PT215 physical-validation manifest=%s",
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
    )
    return manifest

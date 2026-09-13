"""Geometry-first helpers for physical prompt transport.

The helpers in this module intentionally reuse the existing Continuum temporal,
continuation and state contracts. They do not introduce a second rounding path.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..constants import FPS
from ..masked_continuation import (
    CONTINUATION_GUIDE,
    CONTINUATION_NATIVE_MASKED,
    choose_continuation_context_frames,
    plan_continuation_contract,
)
from ..state import make_plan
from ..temporal import (
    largest_context_capacity,
    make_extension_shape,
    make_extension_shape_at_least,
)
from .physical_prompts import (
    make_physical_sample_descriptor,
    physical_metadata,
    physical_prompt_compiler_enabled,
    physical_timeline_video_enabled,
)
from .physical_runtime import build_presentation_contract, compile_invocation_prompt


@dataclass(frozen=True)
class ResolvedInvocationGeometry:
    sequence_index: int
    is_final: bool
    continuation: bool
    clip_index: int
    context_frames: int
    total_frames: int
    net_frames: int
    motion_score: float
    reason: str
    plan: dict[str, Any]


def resolve_normal_geometry(
    *,
    previous_state: dict[str, Any] | None,
    sequence_index: int,
    chunks: int,
    chunk_seconds: float,
    retained_frames: int,
    width: int,
    height: int,
    continuity: str,
    continuation_method: str,
    audio_continuity: bool,
    driving_audio_active: bool,
    debug: bool,
    initial_frame_count: int,
) -> ResolvedInvocationGeometry:
    """Resolve C/F and the existing Plan exactly once for a normal invocation."""

    sequence_index = int(sequence_index)
    is_final = sequence_index == int(chunks) - 1
    if previous_state is None:
        total_frames = int(initial_frame_count)
        clip_index = 1
        plan = make_plan(
            continuation=False,
            clip_index=clip_index,
            total_frames=total_frames,
            trim_frames=0,
            width=int(width),
            height=int(height),
            context_frames=5,
            state_capacity_frames=largest_context_capacity(total_frames),
            requested_extend_seconds=float(chunk_seconds),
            debug=bool(debug),
        )
        return ResolvedInvocationGeometry(
            sequence_index=sequence_index,
            is_final=is_final,
            continuation=False,
            clip_index=clip_index,
            context_frames=0,
            total_frames=total_frames,
            net_frames=total_frames,
            motion_score=0.0,
            reason="initial clip",
            plan=plan,
        )

    context_frames, motion_score, reason = choose_continuation_context_frames(
        method=continuation_method,
        continuity=continuity,
        state=previous_state,
        audio_continuity=bool(audio_continuity),
        driving_audio_active=bool(driving_audio_active),
    )
    desired_cumulative = int(round((sequence_index + 1) * float(chunk_seconds) * FPS))
    requested_new_frames = max(1, desired_cumulative - int(retained_frames))
    if is_final:
        shape = make_extension_shape_at_least(context_frames, requested_new_frames)
    else:
        shape = make_extension_shape(context_frames, requested_new_frames / FPS)
    clip_index = int(previous_state["clip_index"]) + 1
    plan = make_plan(
        continuation=True,
        clip_index=clip_index,
        total_frames=shape.total_frames,
        trim_frames=context_frames,
        width=int(width),
        height=int(height),
        context_frames=context_frames,
        state_capacity_frames=largest_context_capacity(shape.net_new_frames),
        requested_extend_seconds=float(chunk_seconds),
        debug=bool(debug),
    )
    if continuation_method == CONTINUATION_NATIVE_MASKED:
        plan = plan_continuation_contract(plan, continuation_method)
    return ResolvedInvocationGeometry(
        sequence_index=sequence_index,
        is_final=is_final,
        continuation=True,
        clip_index=clip_index,
        context_frames=int(context_frames),
        total_frames=int(shape.total_frames),
        net_frames=int(shape.net_new_frames),
        motion_score=float(motion_score),
        reason=str(reason),
        plan=plan,
    )


def video_presentation_contract(
    *,
    reference_video_source: Any = None,
    timeline_video_source: Any = None,
    logical_index: int,
) -> dict[str, Any] | None:
    if timeline_video_source is not None:
        return {
            "kind": "timeline_video",
            "source_combined_hash": str(timeline_video_source.combined_hash),
            "source_sha256": str(timeline_video_source.source_sha256),
            "adapter": (
                "physical_window_v1"
                if physical_timeline_video_enabled()
                else "legacy_nominal_chunk_v1"
            ),
            "logical_index": int(logical_index),
            "target_width": int(timeline_video_source.target_width),
            "target_height": int(timeline_video_source.target_height),
        }
    if reference_video_source is not None:
        return {
            "kind": "reference_video",
            "contract": dict(reference_video_source.contract),
        }
    return None


def _physical_timeline_video_presentation(
    *,
    timeline_video_source: Any,
    geometry: ResolvedInvocationGeometry,
    retained_before: int,
) -> dict[str, Any]:
    """Authenticate the exact CPU RGB presentation before descriptor hashing.

    Geometry is already resolved by the existing Continuum path. The Timeline
    Video adapter therefore derives its source window from the same R/C/F values
    instead of independently rounding nominal chunk time.
    """

    from ..timeline_video import (
        prepare_timeline_video_physical_frames,
        timeline_video_physical_presentation,
    )

    prepared = prepare_timeline_video_physical_frames(
        timeline_video_source,
        global_start_frame=int(retained_before) - int(geometry.context_frames),
        total_frames=int(geometry.total_frames),
        fps_numerator=int(FPS),
        fps_denominator=1,
    )
    presentation = timeline_video_physical_presentation(
        timeline_video_source, prepared
    )
    selection = presentation.get("selection_contract") or {}
    diagnostics = []
    leading = int(selection.get("leading_clamped_frames", 0) or 0)
    trailing = int(selection.get("trailing_clamped_frames", 0) or 0)
    if leading:
        diagnostics.append(
            {
                "level": "warning",
                "code": "H3C-TV201",
                "message": "physical Timeline Video window begins before the available source; leading samples hold the first source frame",
                "frames": leading,
            }
        )
    if trailing:
        diagnostics.append(
            {
                "level": "warning",
                "code": "H3C-TV202",
                "message": "physical Timeline Video window extends past the available source; trailing samples hold the last source frame",
                "frames": trailing,
            }
        )
    if diagnostics:
        presentation["diagnostics"] = diagnostics
    return presentation


def make_normal_descriptor(
    *,
    geometry: ResolvedInvocationGeometry,
    retained_before: int,
    target_duration_frames: int,
    continuation_method: str,
    initial_state_external: bool,
    assets: Any,
    include_first: bool,
    include_last: bool,
    reference_assets: Any = None,
    reference_audio_source: Any = None,
    reference_video_source: Any = None,
    timeline_video_source: Any = None,
):
    if timeline_video_source is not None and physical_timeline_video_enabled():
        video_presentation = _physical_timeline_video_presentation(
            timeline_video_source=timeline_video_source,
            geometry=geometry,
            retained_before=retained_before,
        )
    else:
        video_presentation = video_presentation_contract(
            reference_video_source=reference_video_source,
            timeline_video_source=timeline_video_source,
            logical_index=geometry.sequence_index,
        )
    presentation = build_presentation_contract(
        assets=assets,
        include_first=include_first,
        include_last=include_last,
        reference_assets=reference_assets,
        reference_audio_source=reference_audio_source,
        video_presentation=video_presentation,
    )
    return make_physical_sample_descriptor(
        group_id=f"chunk:{geometry.sequence_index + 1}",
        logical_indices=(geometry.sequence_index,),
        retained_before=int(retained_before),
        context_frames=int(geometry.context_frames),
        total_frames=int(geometry.total_frames),
        target_duration_frames=int(target_duration_frames),
        continuation_method=str(continuation_method),
        initial_state_origin=(
            "external_state_unknown_history"
            if initial_state_external and geometry.sequence_index == 0
            else "sequence"
        ),
        include_first=bool(include_first),
        include_last=bool(include_last),
        presentation_contract=presentation,
        exact_protected=(
            geometry.context_frames > 0
            and continuation_method == CONTINUATION_NATIVE_MASKED
        ),
        guided_overlap=(
            geometry.context_frames > 0
            and continuation_method == CONTINUATION_GUIDE
        ),
    )


def compile_active_metadata(
    *,
    prompt_plan: dict[str, Any],
    descriptor: Any,
    legacy_text: str,
) -> tuple[Any, dict[str, Any]]:
    candidate = physical_prompt_compiler_enabled()
    compiled = compile_invocation_prompt(
        prompt_plan,
        descriptor,
        legacy_text=legacy_text,
        candidate=candidate,
    )
    video_presentation = descriptor.presentation_contract.get("video")
    timeline_video = (
        video_presentation
        if isinstance(video_presentation, dict)
        and video_presentation.get("kind") == "timeline_video"
        else None
    )
    return compiled, physical_metadata(
        descriptor,
        compiled,
        timeline_video=timeline_video,
    )

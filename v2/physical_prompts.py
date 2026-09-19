"""Physical-timeline prompt transport for MiniMax H3 Continuum.

This module is deliberately pure: it resolves authored prompt source against one
already-resolved physical H3 invocation. It does not encode CLIP/Qwen, allocate
latents, mutate sampler state, or own continuation geometry.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
import hashlib
import json
import os
import re
from typing import Any, Iterable

PHYSICAL_DESCRIPTOR_VERSION = 1
COMPILED_PHYSICAL_PROMPT_VERSION = 1
PHYSICAL_COMPILER_VERSION = "physical_timeline_text_v2"
LEGACY_COMPILER_VERSION = "legacy_nominal_v1"
PHYSICAL_PROMPT_ENV = "H3_CONTINUUM_PHYSICAL_PROMPTS"
PHYSICAL_TIMELINE_VIDEO_ENV = "H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO"
_RENDER_QUANTUM = Decimal("0.000001")
# Recovered 00418 production prompts use an outer bracket Timeline section with
# strict bare range lines (for example ``7-8s:``) inside its body. V1 treated
# those lines as opaque prose. V2 recognizes only this deliberately narrow,
# whole-line form, and only when the ranges form an exact contiguous partition
# of the enclosing timed section. Anything ambiguous stays opaque.
_INNER_RANGE_HEADER = re.compile(
    r"^\s*(?P<start>\d+(?:\.\d+)?)\s*(?:s|sec|seconds)?\s*[-–—]\s*"
    r"(?P<end>\d+(?:\.\d+)?)\s*(?:s|sec|seconds)?\s*:\s*$",
    re.IGNORECASE,
)


class PhysicalPromptError(ValueError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _enabled(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def physical_prompt_compiler_enabled() -> bool:
    """Experimental activation gate; legacy nominal execution remains default."""

    return _enabled(PHYSICAL_PROMPT_ENV)


def physical_timeline_video_enabled() -> bool:
    """Separate matched gate for descriptor-aware Timeline Video extraction."""

    return physical_prompt_compiler_enabled() and _enabled(PHYSICAL_TIMELINE_VIDEO_ENV)


def fraction_string(value: Fraction | int | str | float) -> str:
    item = value if isinstance(value, Fraction) else Fraction(str(value))
    return str(item.numerator) if item.denominator == 1 else f"{item.numerator}/{item.denominator}"


def parse_fraction(value: Any) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value, 1)
    text = str(value).strip()
    if not text:
        raise PhysicalPromptError("empty rational value")
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        return Fraction(int(numerator), int(denominator))
    return Fraction(text)


def _format_seconds(value: Fraction) -> str:
    """Render exact rational seconds with deterministic six-place half-even rounding."""

    numerator = int(value.numerator)
    denominator = int(value.denominator)
    precision = max(28, len(str(abs(numerator))) + len(str(abs(denominator))) + 12)
    with localcontext() as context:
        context.prec = precision
        decimal_value = Decimal(numerator) / Decimal(denominator)
        rounded = decimal_value.quantize(_RENDER_QUANTUM, rounding=ROUND_HALF_EVEN)
    rendered = format(rounded, "f").rstrip("0").rstrip(".")
    return rendered or "0"


@dataclass(frozen=True)
class PhysicalSampleDescriptor:
    group_id: str
    logical_indices: tuple[int, ...]
    fps_numerator: int
    fps_denominator: int
    retained_before: int
    context_frames: int
    total_frames: int
    global_start_frame: int
    global_end_frame: int
    target_duration_frames: int
    continuation_method: str
    initial_state_origin: str
    exact_protected_interval: tuple[int, int] | None
    guided_overlap_interval: tuple[int, int] | None
    retained_suffix_interval: tuple[int, int]
    include_first: bool
    include_last: bool
    last_keyframe_index: int | None
    terminal_contract: dict[str, Any] | None
    presentation_contract: dict[str, Any]

    @property
    def fps(self) -> Fraction:
        return Fraction(self.fps_numerator, self.fps_denominator)

    @property
    def global_start_seconds(self) -> Fraction:
        return Fraction(self.global_start_frame, 1) / self.fps

    @property
    def global_end_seconds(self) -> Fraction:
        return Fraction(self.global_end_frame, 1) / self.fps

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "version": PHYSICAL_DESCRIPTOR_VERSION,
            "group_id": self.group_id,
            "logical_indices": list(self.logical_indices),
            "fps": [self.fps_numerator, self.fps_denominator],
            "retained_before": self.retained_before,
            "context_frames": self.context_frames,
            "total_frames": self.total_frames,
            "global_start_frame": self.global_start_frame,
            "global_end_frame": self.global_end_frame,
            "target_duration_frames": self.target_duration_frames,
            "continuation_method": self.continuation_method,
            "initial_state_origin": self.initial_state_origin,
            "exact_protected_interval": list(self.exact_protected_interval) if self.exact_protected_interval else None,
            "guided_overlap_interval": list(self.guided_overlap_interval) if self.guided_overlap_interval else None,
            "retained_suffix_interval": list(self.retained_suffix_interval),
            "include_first": self.include_first,
            "include_last": self.include_last,
            "last_keyframe_index": self.last_keyframe_index,
            "terminal_contract": self.terminal_contract,
            "presentation_contract": self.presentation_contract,
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.semantic_dict())


def make_physical_sample_descriptor(
    *,
    group_id: str,
    logical_indices: Iterable[int],
    retained_before: int,
    context_frames: int,
    total_frames: int,
    target_duration_frames: int,
    continuation_method: str,
    initial_state_origin: str,
    include_first: bool,
    include_last: bool,
    presentation_contract: dict[str, Any],
    fps_numerator: int = 24,
    fps_denominator: int = 1,
    exact_protected: bool = False,
    guided_overlap: bool = False,
    terminal_contract: dict[str, Any] | None = None,
) -> PhysicalSampleDescriptor:
    retained_before = int(retained_before)
    context_frames = int(context_frames)
    total_frames = int(total_frames)
    if retained_before < 0 or context_frames < 0 or total_frames <= context_frames:
        raise PhysicalPromptError("invalid physical sample geometry")
    start = retained_before - context_frames
    end = start + total_frames
    overlap = (start, retained_before) if context_frames else None
    retained_end = retained_before + total_frames - context_frames
    return PhysicalSampleDescriptor(
        group_id=str(group_id),
        logical_indices=tuple(int(value) for value in logical_indices),
        fps_numerator=int(fps_numerator),
        fps_denominator=int(fps_denominator),
        retained_before=retained_before,
        context_frames=context_frames,
        total_frames=total_frames,
        global_start_frame=start,
        global_end_frame=end,
        target_duration_frames=int(target_duration_frames),
        continuation_method=str(continuation_method),
        initial_state_origin=str(initial_state_origin),
        exact_protected_interval=overlap if exact_protected else None,
        guided_overlap_interval=overlap if guided_overlap else None,
        retained_suffix_interval=(retained_before, retained_end),
        include_first=bool(include_first),
        include_last=bool(include_last),
        last_keyframe_index=total_frames - 1 if include_last else None,
        terminal_contract=dict(terminal_contract) if terminal_contract is not None else None,
        presentation_contract=dict(presentation_contract),
    )


@dataclass(frozen=True)
class CompiledPhysicalPrompt:
    compiler_version: str
    text: str
    text_sha256: str
    contributing_intervals: tuple[dict[str, Any], ...]
    diagnostics: tuple[dict[str, Any], ...]
    fallback_status: str
    overrun: bool
    descriptor_digest: str
    presentation_digest: str
    physical_conditioning_hash: str

    def metadata(self) -> dict[str, Any]:
        return {
            "version": COMPILED_PHYSICAL_PROMPT_VERSION,
            "compiler_version": self.compiler_version,
            "text": self.text,
            "text_sha256": self.text_sha256,
            "contributing_intervals": [dict(item) for item in self.contributing_intervals],
            "diagnostics": [dict(item) for item in self.diagnostics],
            "fallback_status": self.fallback_status,
            "overrun": self.overrun,
            "descriptor_digest": self.descriptor_digest,
            "presentation_digest": self.presentation_digest,
            "physical_conditioning_hash": self.physical_conditioning_hash,
        }


def presentation_digest(contract: dict[str, Any]) -> str:
    return canonical_sha256(contract)


def _compile_result(
    *,
    compiler_version: str,
    text: str,
    intervals: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    fallback_status: str,
    overrun: bool,
    descriptor: PhysicalSampleDescriptor,
) -> CompiledPhysicalPrompt:
    descriptor_digest = descriptor.digest
    presentation_sha = presentation_digest(descriptor.presentation_contract)
    text_digest = text_sha256(text)
    physical_hash = canonical_sha256(
        {
            "compiler_version": compiler_version,
            "descriptor": descriptor.semantic_dict(),
            "text": text,
            "presentation_contract": descriptor.presentation_contract,
            "terminal_contract": descriptor.terminal_contract,
        }
    )
    return CompiledPhysicalPrompt(
        compiler_version=compiler_version,
        text=str(text),
        text_sha256=text_digest,
        contributing_intervals=tuple(intervals),
        diagnostics=tuple(diagnostics),
        fallback_status=str(fallback_status),
        overrun=bool(overrun),
        descriptor_digest=descriptor_digest,
        presentation_digest=presentation_sha,
        physical_conditioning_hash=physical_hash,
    )


def _logical_prompt(plan: dict[str, Any], logical_index: int) -> str:
    prompts = list(plan.get("prompts") or [])
    if not prompts:
        return ""
    index = max(0, min(int(logical_index), len(prompts) - 1))
    return str(prompts[index])


def compile_legacy_nominal(
    plan: dict[str, Any],
    descriptor: PhysicalSampleDescriptor,
    *,
    text: str | None = None,
) -> CompiledPhysicalPrompt:
    logical = descriptor.logical_indices[0] if descriptor.logical_indices else 0
    emitted = _logical_prompt(plan, logical) if text is None else str(text)
    interval = {
        "kind": "legacy_logical",
        "logical_index": int(logical),
        "global_start_frame": descriptor.global_start_frame,
        "global_end_frame": descriptor.global_end_frame,
        "local_start": "0",
        "local_end": fraction_string(Fraction(descriptor.total_frames, 1) / descriptor.fps),
    }
    return _compile_result(
        compiler_version=LEGACY_COMPILER_VERSION,
        text=emitted,
        intervals=[interval],
        diagnostics=[],
        fallback_status="legacy_nominal",
        overrun=False,
        descriptor=descriptor,
    )


def _timeline_source(plan: dict[str, Any]) -> dict[str, Any] | None:
    source = plan.get("source")
    if not isinstance(source, dict) or source.get("kind") != "timeline":
        return None
    if not isinstance(source.get("sections"), list):
        return None
    return source


def _section_interval(section: dict[str, Any], chunk_seconds: Fraction) -> tuple[Fraction, Fraction]:
    if section.get("kind") == "chunk":
        index = int(section["chunk_index"])
        return (Fraction(index - 1, 1) * chunk_seconds, Fraction(index, 1) * chunk_seconds)
    return parse_fraction(section["start"]), parse_fraction(section["end"])


def _strict_inner_ranges(
    section: dict[str, Any],
    *,
    outer_start: Fraction,
    outer_end: Fraction,
) -> list[dict[str, Any]] | None:
    """Return an exact inner partition or ``None`` when the body is ambiguous.

    This deliberately recognizes only the production form recovered from 00418:
    whole-line ``start-end[s]:`` headers inside one already-valid timed section.
    To avoid turning ordinary prose timestamps into routing semantics, refinement
    is accepted only when the first nonblank body line is a header, every header
    has a non-empty body, ranges are increasing/non-overlapping, and the ranges
    exactly and contiguously cover the enclosing section.
    """

    if section.get("kind") != "time":
        return None
    lines = str(section.get("body", "")).splitlines()
    if not lines:
        return None
    first_nonblank = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_nonblank is None or _INNER_RANGE_HEADER.match(lines[first_nonblank]) is None:
        return None

    ranges: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    body: list[str] = []

    def finish() -> bool:
        nonlocal current, body
        if current is None:
            return True
        raw_body = "\n".join(body).strip()
        if not raw_body:
            return False
        current["body"] = raw_body
        ranges.append(current)
        current = None
        body = []
        return True

    for line in lines[first_nonblank:]:
        match = _INNER_RANGE_HEADER.match(line)
        if match is None:
            body.append(line)
            continue
        if not finish():
            return None
        start = parse_fraction(match.group("start"))
        end = parse_fraction(match.group("end"))
        if end <= start:
            return None
        current = {
            "kind": "time",
            "_start": start,
            "_end": end,
            "_inner_range": True,
            "inner_header": line.strip(),
        }
    if not finish() or not ranges:
        return None

    cursor = outer_start
    for item in ranges:
        if item["_start"] != cursor or item["_end"] > outer_end:
            return None
        cursor = item["_end"]
    if cursor != outer_end:
        return None
    return ranges


def inspect_strict_inner_ranges(
    section: dict[str, Any],
    *,
    outer_start: Fraction,
    outer_end: Fraction,
) -> list[dict[str, Any]] | None:
    """Expose the physical compiler's strict-inner grammar for transport validation."""
    ranges = _strict_inner_ranges(section, outer_start=outer_start, outer_end=outer_end)
    if ranges is None:
        return None
    return [
        {
            "start": fraction_string(item["_start"]),
            "end": fraction_string(item["_end"]),
            "header": str(item.get("inner_header", "")),
            "body": str(item.get("body", "")),
        }
        for item in ranges
    ]


def inner_range_header_like(line: str) -> bool:
    return bool(_INNER_RANGE_HEADER.match(str(line)))


def _resolved_candidates(source: dict[str, Any], chunk_seconds: Fraction) -> list[dict[str, Any]]:
    result = []
    for raw in source.get("sections") or []:
        section = dict(raw)
        start, end = _section_interval(section, chunk_seconds)
        if end <= start:
            continue
        ordinal = int(section.get("ordinal", len(result)))
        inner = _strict_inner_ranges(section, outer_start=start, outer_end=end)
        if inner is not None:
            for inner_ordinal, item in enumerate(inner):
                item["ordinal"] = ordinal
                item["inner_ordinal"] = inner_ordinal
                item["outer_header"] = section.get("header")
                result.append(item)
            continue
        section["_start"] = start
        section["_end"] = end
        section["ordinal"] = ordinal
        result.append(section)
    overrides = source.get("overrides") or {}
    if isinstance(overrides, dict):
        for key, body in overrides.items():
            index = int(key)
            result.append(
                {
                    "kind": "override",
                    "chunk_index": index,
                    "body": str(body),
                    "ordinal": -1,
                    "_start": Fraction(index - 1, 1) * chunk_seconds,
                    "_end": Fraction(index, 1) * chunk_seconds,
                }
            )
    return result


def _priority(section: dict[str, Any]) -> tuple[int, int]:
    kind = section.get("kind")
    if kind == "override":
        return (3, -int(section.get("ordinal", 0)))
    if kind == "chunk":
        return (2, -int(section.get("ordinal", 0)))
    return (1, -int(section.get("ordinal", 0)))


def _source_ordinals(items: Iterable[dict[str, Any]]) -> list[int]:
    return sorted(
        {
            int(item.get("ordinal", -1))
            for item in items
            if int(item.get("ordinal", -1)) >= 0
        }
    )


def _earliest_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable = sorted(
        candidates,
        key=lambda item: (item["_start"], -_priority(item)[0], int(item.get("ordinal", 0))),
    )
    return usable[0] if usable else None


def _body_for_atomic_interval(
    candidates: list[dict[str, Any]],
    start: Fraction,
    end: Fraction,
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    active = [item for item in candidates if item["_start"] < end and item["_end"] > start]
    if not active:
        return None, [], []
    chosen = max(active, key=_priority)
    diagnostics: list[dict[str, Any]] = []
    timed = [item for item in active if item.get("kind") == "time"]
    if len(timed) > 1 and chosen.get("kind") == "time":
        diagnostics.append(
            {
                "level": "warning",
                "code": "H3C-PT201",
                "message": "overlapping timed sections resolved by earliest source ordinal",
                "ordinals": sorted(int(item.get("ordinal", 0)) for item in timed),
            }
        )
    return str(chosen.get("body", "")), [chosen], diagnostics


def _join_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    joined: list[dict[str, Any]] = []
    for segment in segments:
        if joined and joined[-1]["body"] == segment["body"] and joined[-1]["end"] == segment["start"]:
            joined[-1]["end"] = segment["end"]
            joined[-1]["sources"].extend(segment["sources"])
            joined[-1]["sources"] = sorted(set(joined[-1]["sources"]))
            joined[-1]["fallback"] = bool(joined[-1]["fallback"] or segment["fallback"])
            continue
        joined.append(dict(segment))
    return joined


def _render_segments(
    segments: list[dict[str, Any]],
    *,
    start: Fraction,
    diagnostics: list[dict[str, Any]],
) -> str:
    """Render ranges without emitting a visually zero-width six-decimal block.

    Exact interval metadata stays untouched. A range that collapses only after
    six-place rendering is folded into an adjacent rendered block while all
    original body text is retained and a diagnostic records the approximation.
    """

    rendered: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for segment in segments:
        local_start = segment["start"] - start
        local_end = segment["end"] - start
        if local_end <= local_start:
            continue
        start_text = _format_seconds(local_start)
        end_text = _format_seconds(local_end)
        if start_text == end_text:
            diagnostics.append(
                {
                    "level": "warning",
                    "code": "H3C-PT204",
                    "message": "sub-microsecond source interval coalesced for six-decimal prompt rendering; exact interval retained in metadata",
                    "local_start": fraction_string(local_start),
                    "local_end": fraction_string(local_end),
                }
            )
            if rendered:
                rendered[-1]["end"] = local_end
                rendered[-1]["bodies"].append(segment["body"].strip())
            else:
                pending.append(segment)
            continue
        bodies = [item["body"].strip() for item in pending]
        bodies.append(segment["body"].strip())
        render_start = pending[0]["start"] - start if pending else local_start
        pending.clear()
        rendered.append(
            {
                "start": render_start,
                "end": local_end,
                "bodies": bodies,
            }
        )
    if pending:
        if rendered:
            rendered[-1]["end"] = pending[-1]["end"] - start
            rendered[-1]["bodies"].extend(item["body"].strip() for item in pending)
        else:
            return "\n\n".join(item["body"].strip() for item in pending)

    blocks = []
    for item in rendered:
        label = f"[{_format_seconds(item['start'])}-{_format_seconds(item['end'])}s]"
        bodies = "\n\n".join(body for body in item["bodies"] if body)
        blocks.append(f"{label}\n{bodies}" if bodies else label)
    return "\n\n".join(blocks)


def compile_physical_prompt(
    plan: dict[str, Any],
    descriptor: PhysicalSampleDescriptor,
) -> CompiledPhysicalPrompt:
    """Compile one physical-local Qwen text sequence from schema-2 source.

    Fixed/List/legacy plans intentionally preserve nominal per-invocation text.
    Only schema-2 Timeline source receives physical interval compilation. V2
    additionally expands a strict, fully partitioning inner ``start-end[s]:``
    schedule when present inside an enclosing timed section; ambiguous prose is
    intentionally left opaque.
    """

    source = _timeline_source(plan)
    if source is None:
        return compile_legacy_nominal(plan, descriptor)

    chunk_seconds = parse_fraction(source.get("chunk_seconds", plan.get("chunk_seconds", 0)))
    chunks = int(plan.get("chunks", 0))
    domain_end = Fraction(chunks, 1) * chunk_seconds
    start = descriptor.global_start_seconds
    end = descriptor.global_end_seconds
    candidates = _resolved_candidates(source, chunk_seconds)
    if not candidates:
        return compile_legacy_nominal(plan, descriptor)

    boundaries = {start, end, Fraction(0, 1), domain_end}
    for item in candidates:
        boundaries.add(item["_start"])
        boundaries.add(item["_end"])
    ordered = sorted(value for value in boundaries if start <= value <= end)
    if not ordered or ordered[0] != start:
        ordered.insert(0, start)
    if ordered[-1] != end:
        ordered.append(end)

    first_item = _earliest_candidate(candidates)
    first_body = str(first_item.get("body", "")) if first_item is not None else ""
    first_sources = _source_ordinals([first_item]) if first_item is not None else []
    previous_body: str | None = None
    previous_sources: list[int] = []
    # Find the authored body immediately before the requested domain end for
    # physical native-grid overrun. Do not pull future authored sections in.
    before_end = [item for item in candidates if item["_start"] < domain_end]
    before_end.sort(key=lambda item: (min(item["_end"], domain_end), _priority(item)), reverse=True)
    overrun_item = before_end[0] if before_end else first_item
    overrun_body = str(overrun_item.get("body", first_body)) if overrun_item is not None else first_body
    overrun_sources = _source_ordinals([overrun_item]) if overrun_item is not None else first_sources
    diagnostics: list[dict[str, Any]] = []
    refined_outer = sorted(
        {
            str(item.get("outer_header"))
            for item in candidates
            if item.get("_inner_range") and item.get("outer_header")
        }
    )
    if refined_outer:
        diagnostics.append(
            {
                "level": "info",
                "code": "H3C-PT205",
                "message": "expanded strict inner timeline ranges recovered from an enclosing timed section",
                "outer_headers": refined_outer,
            }
        )
    segments: list[dict[str, Any]] = []
    fallback_status = "none"
    for left, right in zip(ordered, ordered[1:]):
        if right <= left:
            continue
        fallback = False
        sources: list[int] = []
        if right <= 0:
            body = first_body
            sources = list(first_sources)
            fallback = True
            fallback_status = "unknown_prior_state"
            diagnostics.append(
                {
                    "level": "warning",
                    "code": "H3C-PT202",
                    "message": "physical window precedes the new sequence origin; extended the earliest resolved body as textual lead-in",
                }
            )
        elif left >= domain_end:
            body = overrun_body
            sources = list(overrun_sources)
            fallback = True
            if fallback_status == "none":
                fallback_status = "physical_overrun_hold"
        else:
            body, active, local_diagnostics = _body_for_atomic_interval(candidates, left, right)
            diagnostics.extend(local_diagnostics)
            sources = _source_ordinals(active)
            if body is None:
                fallback = True
                if previous_body is not None:
                    body = previous_body
                    sources = list(previous_sources)
                    if fallback_status == "none":
                        fallback_status = "gap_hold_previous"
                else:
                    body = first_body
                    sources = list(first_sources)
                    if fallback_status == "none":
                        fallback_status = "leading_gap_earliest"
                diagnostics.append(
                    {
                        "level": "warning",
                        "code": "H3C-PT203",
                        "message": "uncovered physical interval resolved by deterministic timeline fallback",
                        "global_start": fraction_string(left),
                        "global_end": fraction_string(right),
                    }
                )
        previous_body = str(body)
        previous_sources = list(sources)
        segments.append(
            {
                "start": left,
                "end": right,
                "body": str(body),
                "sources": sources,
                "fallback": fallback,
            }
        )

    segments = _join_segments(segments)
    preamble = str(source.get("preamble", "")).strip()
    duration = end - start
    if len(segments) == 1 and segments[0]["start"] == start and segments[0]["end"] == end:
        body = segments[0]["body"].strip()
        emitted = f"{preamble}\n\n{body}" if preamble and body else (preamble or body)
    else:
        body = _render_segments(segments, start=start, diagnostics=diagnostics)
        emitted = f"{preamble}\n\n{body}" if preamble and body else (preamble or body)

    interval_metadata = []
    for segment in segments:
        local_start = segment["start"] - start
        local_end = segment["end"] - start
        interval_metadata.append(
            {
                "global_start": fraction_string(segment["start"]),
                "global_end": fraction_string(segment["end"]),
                "local_start": fraction_string(local_start),
                "local_end": fraction_string(local_end),
                "source_ordinals": list(segment["sources"]),
                "fallback": bool(segment["fallback"]),
                "body_sha256": text_sha256(segment["body"]),
            }
        )
    if duration <= 0:
        raise PhysicalPromptError("physical prompt window is empty")
    return _compile_result(
        compiler_version=PHYSICAL_COMPILER_VERSION,
        text=emitted,
        intervals=interval_metadata,
        diagnostics=diagnostics,
        fallback_status=fallback_status,
        overrun=end > domain_end,
        descriptor=descriptor,
    )


def physical_metadata(
    descriptor: PhysicalSampleDescriptor,
    compiled: CompiledPhysicalPrompt,
    *,
    timeline_video: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "descriptor": descriptor.semantic_dict(),
        "descriptor_digest": descriptor.digest,
        "compiled": compiled.metadata(),
        "physical_conditioning_hash": compiled.physical_conditioning_hash,
    }
    if timeline_video is not None:
        result["timeline_video"] = dict(timeline_video)
    return result


def _require_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PhysicalPromptError(f"physical prompt {field} is invalid")
    return int(value)


def _validate_descriptor_semantics(descriptor: dict[str, Any]) -> None:
    if int(descriptor.get("version", -1)) != PHYSICAL_DESCRIPTOR_VERSION:
        raise PhysicalPromptError("physical prompt descriptor is invalid")
    logical_indices = descriptor.get("logical_indices")
    if not isinstance(logical_indices, list) or not logical_indices or not all(
        isinstance(value, int) and not isinstance(value, bool) for value in logical_indices
    ):
        raise PhysicalPromptError("physical prompt logical indices are invalid")
    fps = descriptor.get("fps")
    if (
        not isinstance(fps, list)
        or len(fps) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in fps)
        or int(fps[0]) <= 0
        or int(fps[1]) <= 0
    ):
        raise PhysicalPromptError("physical prompt fps is invalid")
    retained = _require_int(descriptor.get("retained_before"), field="retained_before")
    context = _require_int(descriptor.get("context_frames"), field="context_frames")
    total = _require_int(descriptor.get("total_frames"), field="total_frames")
    start = _require_int(descriptor.get("global_start_frame"), field="global_start_frame")
    end = _require_int(descriptor.get("global_end_frame"), field="global_end_frame")
    target = _require_int(descriptor.get("target_duration_frames"), field="target_duration_frames")
    if retained < 0 or context < 0 or total <= context or target <= 0:
        raise PhysicalPromptError("physical prompt descriptor geometry is invalid")
    if start != retained - context or end != start + total:
        raise PhysicalPromptError("physical prompt descriptor window is inconsistent")
    expected_overlap = [start, retained] if context else None
    exact = descriptor.get("exact_protected_interval")
    guided = descriptor.get("guided_overlap_interval")
    if exact is not None and exact != expected_overlap:
        raise PhysicalPromptError("physical prompt exact protected interval is inconsistent")
    if guided is not None and guided != expected_overlap:
        raise PhysicalPromptError("physical prompt guided overlap interval is inconsistent")
    if exact is not None and guided is not None:
        raise PhysicalPromptError("physical prompt overlap cannot be both exact and guided")
    expected_suffix = [retained, retained + total - context]
    if descriptor.get("retained_suffix_interval") != expected_suffix:
        raise PhysicalPromptError("physical prompt retained suffix interval is inconsistent")
    include_first = descriptor.get("include_first")
    include_last = descriptor.get("include_last")
    if not isinstance(include_first, bool) or not isinstance(include_last, bool):
        raise PhysicalPromptError("physical prompt presentation flags are invalid")
    expected_last_index = total - 1 if include_last else None
    if descriptor.get("last_keyframe_index") != expected_last_index:
        raise PhysicalPromptError("physical prompt last keyframe index is inconsistent")
    presentation = descriptor.get("presentation_contract")
    if not isinstance(presentation, dict):
        raise PhysicalPromptError("physical prompt presentation contract is invalid")
    for field, expected in (("include_first", include_first), ("include_last", include_last)):
        if field in presentation and (
            not isinstance(presentation[field], bool) or presentation[field] != expected
        ):
            raise PhysicalPromptError("physical prompt descriptor/presentation flags are inconsistent")
    terminal = descriptor.get("terminal_contract")
    if terminal is not None and not isinstance(terminal, dict):
        raise PhysicalPromptError("physical prompt terminal contract is invalid")


def validate_physical_metadata(value: Any) -> dict[str, Any]:
    """Validate stored transport metadata by recomputing every persisted digest.

    Stored hashes are integrity fields, not trusted assertions. Reuse therefore
    rejects stale or corrupted descriptor/text/presentation metadata even when a
    duplicated top-level hash still has the expected shape.
    """

    if not isinstance(value, dict):
        raise PhysicalPromptError("physical prompt metadata is missing")
    descriptor = value.get("descriptor")
    compiled = value.get("compiled")
    if not isinstance(descriptor, dict):
        raise PhysicalPromptError("physical prompt descriptor is invalid")
    _validate_descriptor_semantics(descriptor)
    if not isinstance(compiled, dict) or int(compiled.get("version", -1)) != COMPILED_PHYSICAL_PROMPT_VERSION:
        raise PhysicalPromptError("compiled physical prompt metadata is invalid")
    compiler_version = compiled.get("compiler_version")
    text = compiled.get("text")
    if not isinstance(compiler_version, str) or not compiler_version or not isinstance(text, str):
        raise PhysicalPromptError("compiled physical prompt identity is invalid")
    if not isinstance(compiled.get("contributing_intervals"), list):
        raise PhysicalPromptError("compiled physical prompt intervals are invalid")
    if not isinstance(compiled.get("diagnostics"), list):
        raise PhysicalPromptError("compiled physical prompt diagnostics are invalid")
    if not isinstance(compiled.get("fallback_status"), str) or not isinstance(compiled.get("overrun"), bool):
        raise PhysicalPromptError("compiled physical prompt status is invalid")
    timeline_video = value.get("timeline_video")
    if timeline_video is not None and not isinstance(timeline_video, dict):
        raise PhysicalPromptError("physical prompt Timeline Video metadata is invalid")
    presentation_video = descriptor["presentation_contract"].get("video")
    if timeline_video is not None and timeline_video != presentation_video:
        raise PhysicalPromptError("physical prompt Timeline Video metadata is inconsistent")

    descriptor_hash = canonical_sha256(descriptor)
    if str(value.get("descriptor_digest", "")) != descriptor_hash:
        raise PhysicalPromptError("physical prompt descriptor digest mismatch")
    if str(compiled.get("descriptor_digest", "")) != descriptor_hash:
        raise PhysicalPromptError("compiled physical prompt descriptor digest mismatch")

    text_hash = text_sha256(text)
    if str(compiled.get("text_sha256", "")) != text_hash:
        raise PhysicalPromptError("compiled physical prompt text digest mismatch")

    presentation = descriptor["presentation_contract"]
    expected_presentation_hash = presentation_digest(presentation)
    if str(compiled.get("presentation_digest", "")) != expected_presentation_hash:
        raise PhysicalPromptError("compiled physical prompt presentation digest mismatch")

    expected_conditioning_hash = canonical_sha256(
        {
            "compiler_version": compiler_version,
            "descriptor": descriptor,
            "text": text,
            "presentation_contract": presentation,
            "terminal_contract": descriptor.get("terminal_contract"),
        }
    )
    compiled_conditioning_hash = str(compiled.get("physical_conditioning_hash", ""))
    top_conditioning_hash = str(value.get("physical_conditioning_hash", ""))
    if (
        len(expected_conditioning_hash) != 64
        or compiled_conditioning_hash != expected_conditioning_hash
        or top_conditioning_hash != expected_conditioning_hash
    ):
        raise PhysicalPromptError("physical conditioning hash mismatch")
    return value


def physical_metadata_matches(stored: Any, expected: dict[str, Any]) -> bool:
    try:
        stored_value = validate_physical_metadata(stored)
        expected_value = validate_physical_metadata(expected)
    except PhysicalPromptError:
        return False
    return (
        stored_value["physical_conditioning_hash"] == expected_value["physical_conditioning_hash"]
        and stored_value["descriptor_digest"] == expected_value["descriptor_digest"]
        and stored_value.get("timeline_video") == expected_value.get("timeline_video")
    )


def legacy_entry_can_reuse(
    *,
    entry: dict[str, Any],
    descriptor: PhysicalSampleDescriptor,
    compiled: CompiledPhysicalPrompt,
    source_kind: str,
) -> bool:
    """Conservative adapter for Session-1 / Storage-2 entries.

    Continuation entries without physical metadata are never promoted to current
    reuse semantics. An initial opaque Fixed/List sample may be reused only when
    emitted bytes and presentation roles are demonstrably unchanged.
    """

    video = descriptor.presentation_contract.get("video")
    if (
        isinstance(video, dict)
        and video.get("kind") == "timeline_video"
        and video.get("adapter") == "physical_window_v1"
    ):
        return False
    if descriptor.context_frames != 0 or descriptor.retained_before != 0:
        return False
    if source_kind not in {"fixed", "list", "legacy_logical"}:
        return False
    if compiled.compiler_version != LEGACY_COMPILER_VERSION:
        return False
    if str(entry.get("prompt", "")) != compiled.text:
        return False
    plan = entry.get("plan") or {}
    return not isinstance(plan.get("physical_prompt"), dict)

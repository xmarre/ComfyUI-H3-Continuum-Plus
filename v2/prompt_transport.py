"""Managed State Manager -> Impact -> Continuum prompt transport.

The STRING edge remains the only prompt content entering Qwen. This module
validates the request-local sidecar, applies explicit persistent interpretation
intent, and compares pre/post-Impact temporal structure before the existing
schema-2 and physical prompt machinery consumes it.
"""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import re
from typing import Any

from ..constants import (
    PROMPT_FORMAT_AUTO,
    PROMPT_FORMAT_FIXED,
    PROMPT_FORMAT_LIST,
    PROMPT_FORMAT_TIMELINE,
    PROMPT_MODE_FIXED,
    PROMPT_MODE_LIST,
    PROMPT_MODE_TIMELINE,
)
from .physical_prompts import (
    inner_range_header_like,
    inspect_strict_inner_ranges,
    parse_fraction,
)
from . import prompts as prompt_parser

MANAGED_PROMPT_SOURCE_MAGIC = "DSM_H3_PROMPT_SOURCE"
MANAGED_PROMPT_SOURCE_SCHEMA_VERSION = 1
MANAGED_PROMPT_TRANSPORT_VERSION = 1
PROMPT_TRANSPORT_PROVIDER_VERSION = 1
_DECIMAL = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")


def _sha256(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _bounded_error_reason(exc: BaseException, *, limit: int = 240) -> str:
    # Native PromptPlanError messages may include a second "Source:" line that
    # contains authored prompt text. Receipts/logs keep only the bounded reason
    # line; full exceptions remain available to direct parser callers.
    first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return first_line[: max(1, int(limit))]


def _mode_kind(value: Any) -> str | None:
    mapping = {
        PROMPT_FORMAT_FIXED: "fixed",
        PROMPT_FORMAT_LIST: "list",
        PROMPT_FORMAT_TIMELINE: "timeline",
        PROMPT_MODE_FIXED: "fixed",
        PROMPT_MODE_LIST: "list",
        PROMPT_MODE_TIMELINE: "timeline",
    }
    if value == PROMPT_FORMAT_AUTO:
        return None
    return mapping.get(value)


def _format_mode(kind: str) -> str:
    return {"fixed": PROMPT_FORMAT_FIXED, "list": PROMPT_FORMAT_LIST, "timeline": PROMPT_FORMAT_TIMELINE}[kind]


def _validate_prompt_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("prompt_document must be an object")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported prompt_document schema")
    fmt = value.get("format")
    if fmt not in {"inherit", "fixed", "list", "timeline"}:
        raise ValueError("prompt_document format is invalid")
    result = dict(value)
    routing = value.get("routing")
    geometry = value.get("geometry")
    if fmt != "timeline":
        if routing is not None or geometry is not None:
            raise ValueError("non-Timeline document cannot carry routing geometry")
        return result
    if routing not in {"logical_chunks", "physical_timeline"}:
        raise ValueError("Timeline routing is invalid")
    if routing == "physical_timeline":
        if geometry is not None:
            raise ValueError("physical Timeline document cannot carry logical geometry")
        return result
    if not isinstance(geometry, dict):
        raise ValueError("logical Timeline geometry is missing")
    chunks = geometry.get("chunks")
    seconds = geometry.get("chunk_seconds")
    if type(chunks) is not int or not 1 <= chunks <= 16:
        raise ValueError("logical Timeline chunk count is invalid")
    if not isinstance(seconds, str) or _DECIMAL.fullmatch(seconds) is None:
        raise ValueError("logical Timeline chunk_seconds must be an unsigned decimal string")
    try:
        seconds_value = Fraction(seconds)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("logical Timeline chunk_seconds is invalid") from exc
    if seconds_value <= 0:
        raise ValueError("logical Timeline chunk_seconds must be positive")
    return result


def parse_managed_prompt_source(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if value is None or value == "":
        return None, None
    try:
        payload = json.loads(value) if isinstance(value, str) else value
        if not isinstance(payload, dict):
            raise ValueError("managed prompt source must be an object")
        if payload.get("magic") != MANAGED_PROMPT_SOURCE_MAGIC:
            raise ValueError("managed prompt source magic is invalid")
        if type(payload.get("schema_version")) is not int or payload["schema_version"] != MANAGED_PROMPT_SOURCE_SCHEMA_VERSION:
            raise ValueError("managed prompt source schema is unsupported")
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("managed prompt source text is invalid")
        raw_hash = payload.get("raw_text_sha256")
        if not isinstance(raw_hash, str) or raw_hash != _sha256(text):
            raise ValueError("managed prompt source raw text hash mismatch")
        document = _validate_prompt_document(payload.get("prompt_document"))
        revision = payload.get("library_revision")
        if type(revision) is not int or revision < 0:
            raise ValueError("managed prompt source library revision is invalid")
        binding = payload.get("binding")
        if not isinstance(binding, dict):
            raise ValueError("managed prompt source binding is invalid")
        for name in ("manager_node", "text_node", "role", "slot"):
            if not isinstance(binding.get(name), str) or not binding[name]:
                raise ValueError(f"managed prompt source binding {name} is invalid")
        impact = binding.get("impact_node")
        if impact is not None and (not isinstance(impact, str) or not impact):
            raise ValueError("managed prompt source impact binding is invalid")
        if payload.get("queue_contract") != "ordered-impact-v1":
            raise ValueError("managed prompt source queue contract is not verified")
        result = dict(payload)
        result["prompt_document"] = document
        result["binding"] = dict(binding)
        return result, None
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, str(exc)


def _header_tokens(body: str) -> list[str]:
    return [
        line.strip()
        for line in str(body).splitlines()
        if prompt_parser.timeline_header_like(line) or inner_range_header_like(line)
    ]


def _section_skeleton(section: dict[str, Any]) -> dict[str, Any]:
    if section["kind"] == "time":
        start = parse_fraction(section["start_rational"])
        end = parse_fraction(section["end_rational"])
        identity: dict[str, Any] = {"kind": "time", "start": str(start), "end": str(end)}
        inner = inspect_strict_inner_ranges(section, outer_start=start, outer_end=end)
    else:
        identity = {"kind": "chunk", "chunk_index": int(section["index"])}
        inner = None
    identity["inner"] = None if inner is None else [{"start": item["start"], "end": item["end"]} for item in inner]
    identity["header_tokens"] = _header_tokens(section.get("body", ""))
    return identity


def inspect_prompt_structure(text: str) -> dict[str, Any]:
    sections = prompt_parser.parse_timeline_sections(str(text))
    return {
        "preamble_present": bool(prompt_parser.timeline_preamble(str(text))),
        "sections": [_section_skeleton(section) for section in sections],
    }


def classify_prompt_text(text: str) -> str:
    mode = prompt_parser.detect_prompt_mode(str(text))
    return {PROMPT_MODE_FIXED: "fixed", PROMPT_MODE_LIST: "list", PROMPT_MODE_TIMELINE: "timeline"}[mode]


def _logical_geometry_matches(document: dict[str, Any], *, chunks: int, chunk_seconds: float) -> tuple[bool, str | None]:
    geometry = document.get("geometry")
    if not isinstance(geometry, dict):
        return False, "declared logical geometry is missing"
    if int(geometry["chunks"]) != int(chunks):
        return False, "declared chunk count does not match the sampler"
    if Fraction(geometry["chunk_seconds"]) != Fraction(str(chunk_seconds)):
        return False, "declared chunk duration does not match the sampler"
    return True, None


def _logical_signals_match(structure: dict[str, Any], *, chunks: int, chunk_seconds: float) -> tuple[bool, str | None]:
    sections = structure.get("sections") or []
    if len(sections) != int(chunks):
        return False, "logical Timeline must contain exactly one outer signal per chunk"
    kinds = {item.get("kind") for item in sections}
    if len(kinds) != 1:
        return False, "logical Timeline cannot mix timed and Chunk outer signals"
    if kinds == {"chunk"}:
        expected = list(range(1, int(chunks) + 1))
        actual = [int(item.get("chunk_index", 0)) for item in sections]
        if actual != expected:
            return False, "logical Chunk signals are missing, duplicated, or out of order"
        return True, None
    if kinds != {"time"}:
        return False, "logical Timeline outer signal kind is invalid"
    step = Fraction(str(chunk_seconds))
    for index, item in enumerate(sections):
        if parse_fraction(item["start"]) != index * step or parse_fraction(item["end"]) != (index + 1) * step:
            return False, "logical timed signals do not exactly match sampler chunk boundaries"
    return True, None


def _transport_metadata(
    *, status: str, payload: dict[str, Any] | None, expanded_text: str,
    sequence_verified: bool, geometry_match: bool | None, skeleton_match: bool | None,
    fallback_reason: str | None = None, conflict: str | None = None,
    parse_error: str | None = None,
) -> dict[str, Any]:
    document = (payload or {}).get("prompt_document") or {}
    return {
        "transport_version": MANAGED_PROMPT_TRANSPORT_VERSION,
        "status": str(status),
        "declared_format": document.get("format"),
        "declared_routing": document.get("routing"),
        "original_text_sha256": (payload or {}).get("raw_text_sha256"),
        "expanded_text_sha256": _sha256(expanded_text),
        "geometry_match": geometry_match,
        "skeleton_match": skeleton_match,
        "sequence_verified": bool(sequence_verified),
        "fallback_reason": fallback_reason,
        "mode_conflict": conflict,
        "parse_error": parse_error,
    }


def _attach(plan: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    result = dict(plan)
    result["managed_prompt_transport"] = metadata
    return result


def resolve_managed_prompt_plan(
    *, prompt_mode: Any, expanded_text: str, managed_prompt_source_json: Any,
    chunks: int, chunk_seconds: float,
) -> dict[str, Any]:
    payload, parse_error = parse_managed_prompt_source(managed_prompt_source_json)
    if payload is None:
        plan = prompt_parser.make_prompt_plan(mode=prompt_mode, script=expanded_text, chunks=chunks, chunk_seconds=chunk_seconds)
        return _attach(plan, _transport_metadata(
            status="invalid_sidecar" if parse_error else "unverified", payload=None,
            expanded_text=expanded_text, sequence_verified=False, geometry_match=None,
            skeleton_match=None, parse_error=parse_error,
        ))

    document = payload["prompt_document"]
    declared = str(document["format"])
    explicit = _mode_kind(prompt_mode)
    conflict = None
    if explicit is not None and declared != "inherit" and explicit != declared:
        conflict = f"sampler={explicit}, document={declared}"

    if explicit is not None:
        effective = explicit
    elif declared == "inherit":
        plan = prompt_parser.make_prompt_plan(mode=PROMPT_FORMAT_AUTO, script=expanded_text, chunks=chunks, chunk_seconds=chunk_seconds)
        return _attach(plan, _transport_metadata(
            status="inherit_unverified", payload=payload, expanded_text=expanded_text,
            sequence_verified=False, geometry_match=None, skeleton_match=None,
        ))
    else:
        effective = declared

    if effective in {"fixed", "list"}:
        plan = prompt_parser.make_prompt_plan(mode=_format_mode(effective), script=expanded_text, chunks=chunks, chunk_seconds=chunk_seconds)
        return _attach(plan, _transport_metadata(
            status="mode_conflict" if conflict else "document_applied", payload=payload,
            expanded_text=expanded_text, sequence_verified=False, geometry_match=None,
            skeleton_match=None, conflict=conflict,
        ))

    if conflict:
        plan = prompt_parser.make_prompt_plan(mode=PROMPT_FORMAT_TIMELINE, script=expanded_text, chunks=chunks, chunk_seconds=chunk_seconds)
        return _attach(plan, _transport_metadata(
            status="mode_conflict", payload=payload, expanded_text=expanded_text,
            sequence_verified=False, geometry_match=None, skeleton_match=None, conflict=conflict,
        ))

    if declared != "timeline":
        plan = prompt_parser.make_prompt_plan(mode=PROMPT_FORMAT_TIMELINE, script=expanded_text, chunks=chunks, chunk_seconds=chunk_seconds)
        return _attach(plan, _transport_metadata(
            status="explicit_timeline_unverified", payload=payload, expanded_text=expanded_text,
            sequence_verified=False, geometry_match=None, skeleton_match=None,
        ))

    routing = document.get("routing")
    geometry_match: bool | None = None
    try:
        original_structure = inspect_prompt_structure(payload["text"])
        expanded_structure = inspect_prompt_structure(expanded_text)
    except (TypeError, ValueError, prompt_parser.PromptPlanError) as exc:
        fallback = prompt_parser.make_prompt_plan(mode=PROMPT_FORMAT_FIXED, script=expanded_text, chunks=chunks, chunk_seconds=chunk_seconds)
        return _attach(fallback, _transport_metadata(
            status="fallback_fixed", payload=payload, expanded_text=expanded_text,
            sequence_verified=False, geometry_match=False if routing == "logical_chunks" else None,
            skeleton_match=False,
            fallback_reason=f"Timeline structure is invalid: {_bounded_error_reason(exc)}",
        ))

    if routing == "logical_chunks":
        geometry_match, reason = _logical_geometry_matches(document, chunks=chunks, chunk_seconds=chunk_seconds)
        if geometry_match:
            geometry_match, reason = _logical_signals_match(original_structure, chunks=chunks, chunk_seconds=chunk_seconds)
        if not geometry_match:
            fallback = prompt_parser.make_prompt_plan(mode=PROMPT_FORMAT_FIXED, script=expanded_text, chunks=chunks, chunk_seconds=chunk_seconds)
            return _attach(fallback, _transport_metadata(
                status="fallback_fixed", payload=payload, expanded_text=expanded_text,
                sequence_verified=False, geometry_match=False, skeleton_match=None,
                fallback_reason=reason,
            ))

    skeleton_match = original_structure == expanded_structure
    if not skeleton_match:
        fallback = prompt_parser.make_prompt_plan(mode=PROMPT_FORMAT_FIXED, script=expanded_text, chunks=chunks, chunk_seconds=chunk_seconds)
        return _attach(fallback, _transport_metadata(
            status="fallback_fixed", payload=payload, expanded_text=expanded_text,
            sequence_verified=False, geometry_match=geometry_match, skeleton_match=False,
            fallback_reason="Impact expansion changed Timeline structure",
        ))

    plan = prompt_parser.make_prompt_plan(mode=PROMPT_FORMAT_TIMELINE, script=expanded_text, chunks=chunks, chunk_seconds=chunk_seconds)
    if plan.get("mode") != PROMPT_MODE_TIMELINE:
        return _attach(plan, _transport_metadata(
            status="fallback_fixed", payload=payload, expanded_text=expanded_text,
            sequence_verified=False, geometry_match=geometry_match, skeleton_match=True,
            fallback_reason="native Timeline parser used Fixed fallback",
        ))
    return _attach(plan, _transport_metadata(
        status="verified_sequence", payload=payload, expanded_text=expanded_text,
        sequence_verified=True, geometry_match=geometry_match, skeleton_match=True,
    ))


def provider_capabilities() -> dict[str, Any]:
    return {
        "provider_version": PROMPT_TRANSPORT_PROVIDER_VERSION,
        "managed_source_schema_versions": [MANAGED_PROMPT_SOURCE_SCHEMA_VERSION],
        "prompt_document_schema_versions": [1],
        "formats": ["inherit", "fixed", "list", "timeline"],
        "timeline_routings": ["logical_chunks", "physical_timeline"],
        "chunks": {"min": 1, "max": 16},
        "chunk_seconds": {"min": 4.0, "max": 15.0},
    }


PROMPT_TRANSPORT_PROVIDER_V1 = {
    **provider_capabilities(),
    "classify": classify_prompt_text,
    "inspect": inspect_prompt_structure,
}

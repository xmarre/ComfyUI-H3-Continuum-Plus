from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import torch

from ComfyUI_H3_Continuum_Join.constants import (
    PROMPT_FORMAT_AUTO,
    PROMPT_FORMAT_FIXED,
    PROMPT_MODE_FIXED,
    PROMPT_MODE_TIMELINE,
)
from ComfyUI_H3_Continuum_Join.masked_continuation import CONTINUATION_GUIDE
from ComfyUI_H3_Continuum_Join.v2.physical_prompts import make_physical_sample_descriptor
from ComfyUI_H3_Continuum_Join.v2 import physical_runtime
from ComfyUI_H3_Continuum_Join.v2.prompt_transport import (
    MANAGED_PROMPT_SOURCE_MAGIC,
    PROMPT_TRANSPORT_PROVIDER_V1,
)
from ComfyUI_H3_Continuum_Join.v2.prompts import build_sampler_prompt_plan


CANONICAL = (
    "SHARED_ENV_SENTINEL\n\n"
    "[0-5s]\nONE_RED_CUBE_SENTINEL\n\n"
    "[5-10s]\nTWO_GREEN_SPHERE_SENTINEL\n\n"
    "[10-15s]\nTHREE_BLUE_PYRAMID_SENTINEL"
)


def _sidecar(text=CANONICAL, *, fmt="timeline", routing="logical_chunks", chunks=3, seconds="5"):
    document = {"schema_version": 1, "format": fmt}
    if fmt == "timeline":
        document["routing"] = routing
        if routing == "logical_chunks":
            document["geometry"] = {"chunks": chunks, "chunk_seconds": seconds}
    return json.dumps({
        "magic": MANAGED_PROMPT_SOURCE_MAGIC,
        "schema_version": 1,
        "text": text,
        "prompt_document": document,
        "raw_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "library_revision": 42,
        "binding": {
            "manager_node": "10", "text_node": "11", "impact_node": "12",
            "role": "positive", "slot": "default",
        },
        "queue_contract": "ordered-impact-v1",
    })


def _plan(expanded=CANONICAL, *, mode=PROMPT_FORMAT_AUTO, sidecar=None):
    return build_sampler_prompt_plan(
        prompt_mode=mode, prompt_script="legacy", sequence_prompt=expanded,
        prompt_plan=None, chunks=3, chunk_seconds=5.0,
        managed_prompt_source_json=_sidecar() if sidecar is None else sidecar,
    )


def test_direct_managed_timeline_is_verified_without_prompt_writer():
    plan = _plan()
    assert plan["mode"] == PROMPT_MODE_TIMELINE
    assert plan["prompts"] == [
        "SHARED_ENV_SENTINEL\n\nONE_RED_CUBE_SENTINEL",
        "SHARED_ENV_SENTINEL\n\nTWO_GREEN_SPHERE_SENTINEL",
        "SHARED_ENV_SENTINEL\n\nTHREE_BLUE_PYRAMID_SENTINEL",
    ]
    receipt = plan["managed_prompt_transport"]
    assert receipt["status"] == "verified_sequence"
    assert receipt["geometry_match"] is True
    assert receipt["skeleton_match"] is True
    assert receipt["sequence_verified"] is True


def test_fixed_document_keeps_header_looking_text_opaque_under_auto():
    text = "[0-5s]\nThis is literal Fixed prose."
    plan = build_sampler_prompt_plan(
        prompt_mode=PROMPT_FORMAT_AUTO, prompt_script="legacy", sequence_prompt=text,
        prompt_plan=None, chunks=2, chunk_seconds=5.0,
        managed_prompt_source_json=_sidecar(text, fmt="fixed"),
    )
    assert plan["mode"] == PROMPT_MODE_FIXED
    assert plan["prompts"] == [text, text]
    assert plan["managed_prompt_transport"]["status"] == "document_applied"


def test_explicit_fixed_wins_over_timeline_document_without_reinterpretation():
    plan = _plan(mode=PROMPT_FORMAT_FIXED)
    assert plan["mode"] == PROMPT_MODE_FIXED
    assert plan["prompts"] == [CANONICAL] * 3
    assert plan["managed_prompt_transport"]["status"] == "mode_conflict"


def test_declared_geometry_mismatch_falls_back_to_expanded_fixed_text():
    plan = _plan(sidecar=_sidecar(chunks=2))
    assert plan["mode"] == PROMPT_MODE_FIXED
    assert plan["prompts"] == [CANONICAL] * 3
    assert plan["managed_prompt_transport"]["geometry_match"] is False


def test_impact_body_expansion_can_change_prose_without_changing_structure():
    expanded = CANONICAL.replace("ONE_RED_CUBE_SENTINEL", "ONE_RED_CUBE_EXPANDED")
    plan = _plan(expanded=expanded)
    assert plan["mode"] == PROMPT_MODE_TIMELINE
    assert "ONE_RED_CUBE_EXPANDED" in plan["prompts"][0]
    assert plan["managed_prompt_transport"]["skeleton_match"] is True


def test_impact_header_injection_falls_back_instead_of_activating_schedule():
    original = "[0-5s]\nONE\n[5-10s]\nWILDCARD_BODY\n[10-15s]\nTHREE"
    expanded = original.replace("WILDCARD_BODY", "[7-8s]\nINJECTED")
    plan = build_sampler_prompt_plan(
        prompt_mode=PROMPT_FORMAT_AUTO, prompt_script="legacy", sequence_prompt=expanded,
        prompt_plan=None, chunks=3, chunk_seconds=5.0,
        managed_prompt_source_json=_sidecar(original),
    )
    assert plan["mode"] == PROMPT_MODE_FIXED
    assert plan["managed_prompt_transport"]["status"] == "fallback_fixed"
    assert plan["managed_prompt_transport"]["skeleton_match"] is False


def test_invalid_sidecar_hash_preserves_legacy_execution_but_not_verification():
    payload = json.loads(_sidecar())
    payload["raw_text_sha256"] = "0" * 64
    plan = _plan(sidecar=json.dumps(payload))
    assert plan["mode"] == PROMPT_MODE_TIMELINE
    assert plan["managed_prompt_transport"]["status"] == "invalid_sidecar"
    assert plan["managed_prompt_transport"]["sequence_verified"] is False


def test_provider_exposes_native_geometry_and_parser_inspection():
    assert PROMPT_TRANSPORT_PROVIDER_V1["provider_version"] == 1
    assert PROMPT_TRANSPORT_PROVIDER_V1["chunk_seconds"] == {"min": 4.0, "max": 15.0}
    assert PROMPT_TRANSPORT_PROVIDER_V1["classify"](CANONICAL) == "timeline"
    structure = PROMPT_TRANSPORT_PROVIDER_V1["inspect"](CANONICAL)
    assert [item["kind"] for item in structure["sections"]] == ["time", "time", "time"]


class _CaptureClip:
    def __init__(self):
        self.prompt = None

    def tokenize(self, prompt, **_kwargs):
        self.prompt = prompt
        return prompt

    def encode_from_tokens_scheduled(self, tokens):
        return [[torch.zeros((1, 1, 2)), {"captured": tokens}]]


def test_verified_transport_isolated_at_actual_physical_qwen_input_boundary(monkeypatch):
    plan = _plan()
    descriptor = make_physical_sample_descriptor(
        group_id="managed-chunk-2", logical_indices=(1,), retained_before=120,
        context_frames=24, total_frames=144, target_duration_frames=360,
        continuation_method=CONTINUATION_GUIDE, initial_state_origin="sequence",
        include_first=False, include_last=False,
        presentation_contract={"include_first": False, "include_last": False},
        guided_overlap=True,
    )
    monkeypatch.setattr(physical_runtime, "physical_prompt_compiler_enabled", lambda: True)
    clip = _CaptureClip()
    assets = SimpleNamespace(first_image=None, last_image=None)
    _conditioning, compiled, _metadata, _key = physical_runtime.encode_physical_prompt_conditioning(
        clip=clip, plan=plan, descriptor=descriptor, legacy_text=plan["prompts"][1],
        assets=assets, include_first=False, include_last=False,
    )
    assert clip.prompt == compiled.text
    assert "SHARED_ENV_SENTINEL" in clip.prompt
    assert "TWO_GREEN_SPHERE_SENTINEL" in clip.prompt
    assert "ONE_RED_CUBE_SENTINEL" not in clip.prompt
    assert "THREE_BLUE_PYRAMID_SENTINEL" not in clip.prompt

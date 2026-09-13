from __future__ import annotations

from types import SimpleNamespace

from ComfyUI_H3_Continuum_Join.constants import CONTINUITY_OPTIONS, PROMPT_MODE_TIMELINE
from ComfyUI_H3_Continuum_Join.masked_continuation import CONTINUATION_GUIDE
from ComfyUI_H3_Continuum_Join.temporal import align_frame_count_up
from ComfyUI_H3_Continuum_Join.v2 import sequence
from ComfyUI_H3_Continuum_Join.v2.physical_sequence import (
    compile_active_metadata,
    make_normal_descriptor,
    resolve_normal_geometry,
)
from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan


def _assets():
    return SimpleNamespace(
        first_image=None,
        last_image=None,
        first_frame_hash="none",
        last_frame_hash="none",
    )


def _plan():
    return make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-5s]\none",
        chunks=1,
        chunk_seconds=5.0,
    )


def _geometry():
    return resolve_normal_geometry(
        previous_state=None,
        sequence_index=0,
        chunks=1,
        chunk_seconds=5.0,
        retained_frames=0,
        width=96,
        height=64,
        continuity=CONTINUITY_OPTIONS[0],
        continuation_method=CONTINUATION_GUIDE,
        audio_continuity=True,
        driving_audio_active=False,
        debug=False,
        initial_frame_count=align_frame_count_up(round(5.0 * 24)),
    )


def _reuse(stored, plan):
    return sequence._physical_reuse_prefix(
        stored,
        prompt_plan=plan,
        prompts=list(plan["prompts"]),
        chunks=1,
        chunk_seconds=5.0,
        width=96,
        height=64,
        continuity=CONTINUITY_OPTIONS[0],
        continuation_method=CONTINUATION_GUIDE,
        audio_continuity=True,
        driving_audio_active=False,
        debug=False,
        initial_frame_count=align_frame_count_up(round(5.0 * 24)),
        assets=_assets(),
        initial_state_external=False,
        terminal_merge_enabled=False,
    )


def test_legacy_control_rejects_stored_physical_timeline_candidate(monkeypatch):
    plan = _plan()
    geometry = _geometry()

    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "1")
    descriptor = make_normal_descriptor(
        geometry=geometry,
        retained_before=0,
        target_duration_frames=round(5.0 * 24),
        continuation_method=CONTINUATION_GUIDE,
        initial_state_external=False,
        assets=_assets(),
        include_first=True,
        include_last=False,
    )
    compiled, metadata = compile_active_metadata(
        prompt_plan=plan,
        descriptor=descriptor,
        legacy_text=plan["prompts"][0],
    )
    assert compiled.compiler_version != "legacy_nominal_v1"

    stored = [{
        "prompt": plan["prompts"][0],
        "plan": {
            "total_frames": geometry.total_frames,
            "trim_frames": geometry.context_frames,
            "net_frames": geometry.net_frames,
            "physical_prompt": metadata,
        },
    }]

    monkeypatch.delenv("H3_CONTINUUM_PHYSICAL_PROMPTS", raising=False)
    accepted, notes = _reuse(stored, plan)

    assert accepted == []
    assert any("physical descriptor or conditioning identity differs" in note for note in notes)


def test_legacy_control_keeps_metadata_free_legacy_prefix_behavior(monkeypatch):
    monkeypatch.delenv("H3_CONTINUUM_PHYSICAL_PROMPTS", raising=False)
    geometry = _geometry()
    stored = [{
        "prompt": "legacy",
        "plan": {
            "total_frames": geometry.total_frames,
            "trim_frames": geometry.context_frames,
            "net_frames": geometry.net_frames,
        },
    }]

    accepted, notes = _reuse(stored, _plan())

    assert accepted == stored
    assert notes == []

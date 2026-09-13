from __future__ import annotations

from types import SimpleNamespace

import torch

from ComfyUI_H3_Continuum_Join.constants import (
    CONTINUITY_OPTIONS,
    PROMPT_MODE_FIXED,
    PROMPT_MODE_TIMELINE,
)
from ComfyUI_H3_Continuum_Join.masked_continuation import CONTINUATION_GUIDE
from ComfyUI_H3_Continuum_Join.temporal import (
    align_frame_count_up,
    audio_latent_t,
    video_latent_t,
)
from ComfyUI_H3_Continuum_Join.v2 import sequence
from ComfyUI_H3_Continuum_Join.v2.physical_sequence import (
    compile_active_metadata,
    make_normal_descriptor,
    resolve_normal_geometry,
)
from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan
from ComfyUI_H3_Continuum_Join.v2.session import make_chunk_entry, make_session, validate_session


class _Nested:
    def __init__(self, members):
        self.members = members

    def unbind(self):
        return self.members


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


def _fixed_plan():
    return make_prompt_plan(
        mode=PROMPT_MODE_FIXED,
        script="one",
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


def _valid_legacy_initial(plan):
    geometry = _geometry()
    latent = {
        "samples": _Nested(
            (
                torch.zeros((1, 24, video_latent_t(geometry.total_frames), 4, 6)),
                torch.zeros((1, 32, 2, audio_latent_t(geometry.total_frames))),
            )
        )
    }
    return make_chunk_entry(
        latent=latent,
        plan=geometry.plan,
        prompt=plan["prompts"][0],
        prompt_hash=plan["hashes"][0],
        seed=1,
        context_frames=0,
        motion_score=0.0,
        reused=True,
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


def test_candidate_can_reuse_equivalent_legacy_fixed_initial_and_serialize_session(monkeypatch):
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "1")
    monkeypatch.delenv("H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO", raising=False)
    plan = _fixed_plan()
    stored = [_valid_legacy_initial(plan)]

    accepted, notes = _reuse(stored, plan)

    assert accepted == stored
    assert any("accepted conservative schema-1 initial legacy conditioning adapter" in note for note in notes)
    physical_settings = sequence._physical_settings(accepted, plan)
    assert physical_settings["candidate_enabled"] is True
    assert physical_settings["compiler_version"] == "legacy_nominal_v1"

    session = make_session(
        chunks=accepted,
        width=96,
        height=64,
        chunk_seconds=5.0,
        identity_hash="none",
        model_fingerprint_value="f" * 64,
        parent_session_id=None,
        reroll_from_chunk=0,
        settings={"physical_prompt_contract": physical_settings},
    )

    assert session["settings"]["physical_prompt_contract"][
        "legacy_initial_adapter_pending_revalidation"
    ] is True
    assert validate_session(session) is session

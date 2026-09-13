import copy

import pytest
import torch

from ComfyUI_H3_Continuum_Join.state import make_plan
from ComfyUI_H3_Continuum_Join.v2 import session_io
from ComfyUI_H3_Continuum_Join.v2.session import (
    SessionValidationError,
    entry_to_state,
    make_chunk_entry,
    make_session,
    validate_session,
)


def _latent(frame_count=124):
    import sys
    from types import ModuleType

    # The session entry accepts a ComfyUI-like object exposing unbind().
    class Nested:
        def __init__(self, parts):
            self.parts = parts
        def unbind(self):
            return self.parts

    video_t = 37 if frame_count == 124 else 42
    audio_t = 207 if frame_count == 124 else 235
    return {
        "samples": Nested(
            (
                torch.randn(1, 24, video_t, 4, 6),
                torch.randn(1, 32, 2, audio_t),
            )
        )
    }


def _initial_entry(*, reused):
    plan = make_plan(
        continuation=False,
        clip_index=1,
        total_frames=124,
        trim_frames=0,
        width=96,
        height=64,
        context_frames=22,
        state_capacity_frames=39,
        requested_extend_seconds=5,
        debug=False,
    )
    return make_chunk_entry(
        latent=_latent(),
        plan=plan,
        prompt="p",
        prompt_hash="0" * 64,
        seed=1,
        context_frames=0,
        motion_score=0.0,
        reused=reused,
    )


def _physical_contract(*, timeline_video=False):
    return {
        "contract_version": 1,
        "compiler_version": "legacy_nominal_v1",
        "candidate_enabled": True,
        "timeline_video_physical_enabled": bool(timeline_video),
        "prompt_source_digest": "a" * 64,
    }


def test_session_roundtrip_and_last_state(tmp_path, monkeypatch):
    entry = _initial_entry(reused=False)
    session = make_session(
        chunks=[entry],
        width=96,
        height=64,
        chunk_seconds=5,
        identity_hash="none",
        model_fingerprint_value="f" * 64,
        parent_session_id=None,
        reroll_from_chunk=0,
        settings={},
    )
    validate_session(session)
    state = entry_to_state(session["chunks"][0])
    assert state["capacity_frames"] == 39

    monkeypatch.setattr(session_io, "session_directory", lambda: tmp_path)
    tensor_path, json_path = session_io.save_session(session, prefix="test", slot=1)
    assert tensor_path.exists() and json_path.exists()
    loaded = session_io.load_session(prefix="test", slot=1)
    assert torch.equal(loaded["chunks"][0]["video"], session["chunks"][0]["video"])
    assert loaded["session_id"] == session["session_id"]


def test_session2_marks_reused_metadata_free_legacy_initial_for_revalidation():
    session = make_session(
        chunks=[_initial_entry(reused=True)],
        width=96,
        height=64,
        chunk_seconds=5,
        identity_hash="none",
        model_fingerprint_value="f" * 64,
        parent_session_id=None,
        reroll_from_chunk=0,
        settings={"physical_prompt_contract": _physical_contract()},
    )

    physical = session["settings"]["physical_prompt_contract"]
    assert physical["legacy_initial_adapter_pending_revalidation"] is True
    assert "physical_prompt" not in session["chunks"][0]["plan"]
    assert validate_session(session) is session

    tampered = copy.deepcopy(session)
    tampered["chunks"][0]["reused"] = False
    with pytest.raises(SessionValidationError, match="physical prompt contract is missing"):
        validate_session(tampered)


def test_session2_does_not_mark_unproven_or_physical_timeline_legacy_initial():
    with pytest.raises(SessionValidationError, match="physical prompt contract is missing"):
        make_session(
            chunks=[_initial_entry(reused=False)],
            width=96,
            height=64,
            chunk_seconds=5,
            identity_hash="none",
            model_fingerprint_value="f" * 64,
            parent_session_id=None,
            reroll_from_chunk=0,
            settings={"physical_prompt_contract": _physical_contract()},
        )

    with pytest.raises(SessionValidationError, match="physical prompt contract is missing"):
        make_session(
            chunks=[_initial_entry(reused=True)],
            width=96,
            height=64,
            chunk_seconds=5,
            identity_hash="none",
            model_fingerprint_value="f" * 64,
            parent_session_id=None,
            reroll_from_chunk=0,
            settings={
                "physical_prompt_contract": _physical_contract(timeline_video=True)
            },
        )


def test_model_fingerprint_can_add_continuum_wrapper_without_mutating_model():
    from types import SimpleNamespace

    from ComfyUI_H3_Continuum_Join.v2.session import model_fingerprint

    base = SimpleNamespace(diffusion_model=object())
    model = SimpleNamespace(
        model=base,
        model_options={"transformer_options": {}},
        wrappers={},
        model_dtype=lambda: "float32",
        model_size=lambda: 1,
    )
    wrapped = SimpleNamespace(
        model=base,
        model_options={"transformer_options": {}},
        wrappers={
            "apply_model": {
                "h3_continuum_join.apply_model.v1": [object()],
            }
        },
        model_dtype=model.model_dtype,
        model_size=model.model_size,
    )

    logical = model_fingerprint(
        model,
        extra_wrapper_keys=("h3_continuum_join.apply_model.v1",),
    )

    assert logical == model_fingerprint(wrapped)
    assert model.wrappers == {}

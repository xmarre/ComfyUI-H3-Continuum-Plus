from __future__ import annotations

from types import SimpleNamespace

import torch

from ComfyUI_H3_Continuum_Join.constants import CONTINUITY_OPTIONS, PROMPT_MODE_TIMELINE
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
from ComfyUI_H3_Continuum_Join.v2.session import entry_to_state, make_chunk_entry


class _Nested:
    def __init__(self, members):
        self.members = members

    def unbind(self):
        return self.members


def _latent(width: int, height: int, frames: int):
    return {
        "samples": _Nested(
            (
                torch.zeros(
                    (1, 24, video_latent_t(frames), height // 16, width // 16)
                ),
                torch.zeros((1, 32, 2, audio_latent_t(frames))),
            )
        )
    }


def _assets():
    return SimpleNamespace(
        first_image=None,
        last_image=None,
        first_frame_hash="none",
        last_frame_hash="none",
    )


def _timeline(section6: str):
    bodies = [
        "section 1",
        "section 2",
        "section 3",
        "section 4",
        "section 5",
        section6,
        "section 7",
    ]
    script = "\n".join(
        f"[{2 * index}-{2 * index + 2}s]\n{body}"
        for index, body in enumerate(bodies)
    )
    return make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script=script,
        chunks=2,
        chunk_seconds=7.0,
    )


def _entries(plan):
    width, height = 96, 64
    retained = 0
    previous_state = None
    entries = []
    assets = _assets()
    initial_frames = align_frame_count_up(round(7.0 * 24))
    target_frames = round(2 * 7.0 * 24)

    for index in range(2):
        geometry = resolve_normal_geometry(
            previous_state=previous_state,
            sequence_index=index,
            chunks=2,
            chunk_seconds=7.0,
            retained_frames=retained,
            width=width,
            height=height,
            continuity=CONTINUITY_OPTIONS[0],
            continuation_method=CONTINUATION_GUIDE,
            audio_continuity=True,
            driving_audio_active=False,
            debug=False,
            initial_frame_count=initial_frames,
        )
        descriptor = make_normal_descriptor(
            geometry=geometry,
            retained_before=retained,
            target_duration_frames=target_frames,
            continuation_method=CONTINUATION_GUIDE,
            initial_state_external=False,
            assets=assets,
            include_first=False,
            include_last=False,
        )
        _compiled, metadata = compile_active_metadata(
            prompt_plan=plan,
            descriptor=descriptor,
            legacy_text=plan["prompts"][index],
        )
        entry_plan = dict(geometry.plan)
        entry_plan["physical_prompt"] = metadata
        entry = make_chunk_entry(
            latent=_latent(width, height, geometry.total_frames),
            plan=entry_plan,
            prompt=plan["prompts"][index],
            prompt_hash=plan["hashes"][index],
            seed=100 + index,
            context_frames=geometry.context_frames,
            motion_score=geometry.motion_score,
            reused=False,
        )
        entries.append(entry)
        previous_state = entry_to_state(entry)
        retained += geometry.net_frames
    return entries


def test_sequential_physical_reuse_stops_at_hidden_source_change(monkeypatch):
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "1")
    original = _timeline("section 6 original")
    edited = _timeline("section 6 edited")

    # The legacy logical view loses section 6, so nominal hashes are identical.
    assert original["hashes"] == edited["hashes"]
    assert original["source"]["source_digest"] != edited["source"]["source_digest"]

    stored = _entries(original)
    accepted, notes = sequence._physical_reuse_prefix(
        stored,
        prompt_plan=edited,
        prompts=list(edited["prompts"]),
        chunks=2,
        chunk_seconds=7.0,
        width=96,
        height=64,
        continuity=CONTINUITY_OPTIONS[0],
        continuation_method=CONTINUATION_GUIDE,
        audio_continuity=True,
        driving_audio_active=False,
        debug=False,
        initial_frame_count=align_frame_count_up(round(7.0 * 24)),
        assets=_assets(),
        initial_state_external=False,
        terminal_merge_enabled=False,
    )

    assert len(accepted) == 1
    assert any("before chunk 2" in note for note in notes)
    assert any("later stored chunks were not considered" in note for note in notes)


def test_sequential_physical_reuse_accepts_exact_match(monkeypatch):
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "1")
    plan = _timeline("section 6")
    stored = _entries(plan)

    accepted, notes = sequence._physical_reuse_prefix(
        stored,
        prompt_plan=plan,
        prompts=list(plan["prompts"]),
        chunks=2,
        chunk_seconds=7.0,
        width=96,
        height=64,
        continuity=CONTINUITY_OPTIONS[0],
        continuation_method=CONTINUATION_GUIDE,
        audio_continuity=True,
        driving_audio_active=False,
        debug=False,
        initial_frame_count=align_frame_count_up(round(7.0 * 24)),
        assets=_assets(),
        initial_state_external=False,
        terminal_merge_enabled=False,
    )

    assert len(accepted) == 2
    assert not any("differs" in note for note in notes)

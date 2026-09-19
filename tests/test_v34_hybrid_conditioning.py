from __future__ import annotations

import torch

from ComfyUI_H3_Continuum_Join.conditioning import conditioning_mode_from_presence
from ComfyUI_H3_Continuum_Join.constants import MARK_VIDEO_CONTEXT
from ComfyUI_H3_Continuum_Join.continuation import POLICY_REPLACE, prepare_conditioning
from ComfyUI_H3_Continuum_Join.masked_continuation import prepare_masked_conditioning
from ComfyUI_H3_Continuum_Join.reference import (
    REFERENCE_SIZE_MATCH_OUTPUT,
    ReferenceAssets,
    encode_reference_prompt,
)
from ComfyUI_H3_Continuum_Join.v2.h3_builder import attach_keyframes


class _Clip:
    def __init__(self):
        self.items = None
        self.prompt = None

    def tokenize(self, prompt, *, minimax_ref_items):
        self.items = list(minimax_ref_items)
        self.prompt = prompt
        return prompt

    def encode_from_tokens_scheduled(self, tokens):
        return [[torch.zeros(1, 1, 2), {}]]


def _reference_assets() -> ReferenceAssets:
    image = torch.zeros((1, 32, 32, 3))
    latent = torch.zeros((1, 24, 1, 2, 2))
    return ReferenceAssets(
        images=(image,),
        latents=(latent,),
        image_hashes=("1" * 64,),
        combined_hash="2" * 64,
        size_mode=REFERENCE_SIZE_MATCH_OUTPUT,
    )


def _hybrid_conditioning(*, frame_count: int = 141):
    clip = _Clip()
    conditioning = encode_reference_prompt(
        clip,
        "<Picture 1> remains the same person",
        _reference_assets(),
    )
    first_latent = torch.ones((1, 24, 1, 2, 2))
    last_latent = torch.full((1, 24, 1, 2, 2), 2.0)
    conditioning = attach_keyframes(
        conditioning,
        frame_count=frame_count,
        first_latent=first_latent,
        last_latent=last_latent,
    )
    return clip, conditioning, first_latent, last_latent


def test_core_shaped_reference_presentation_and_keyframes_coexist():
    clip, conditioning, first_latent, last_latent = _hybrid_conditioning()

    assert len(clip.items) == 1
    assert clip.items[0]["type"] == "image"
    metadata = conditioning[0][1]
    assert [ref["kind"] for ref in metadata["minimax_refs"]] == ["image"]
    assert metadata["minimax_keyframes"] == [
        {"resolved_frame_index": 0, "latent": first_latent},
        {"resolved_frame_index": 140, "latent": last_latent},
    ]
    assert metadata["minimax_frame_count"] == 141


def test_hybrid_qwen_presentation_includes_keyframes_and_keeps_reference_numbering():
    clip = _Clip()
    first = torch.ones((1, 32, 32, 3))
    last = torch.full((1, 32, 32, 3), 2.0)
    references = _reference_assets()

    conditioning = encode_reference_prompt(
        clip,
        "<Picture 1> remains the same person; <Picture 2> is unavailable",
        references,
        first_image=first,
        last_image=last,
    )

    assert clip.items[0]["data"] is first
    assert clip.items[1]["data"] is last
    assert clip.items[2]["data"] is references.images[0]
    assert clip.prompt == (
        "<Picture 3> remains the same person; <Picture 4> is unavailable"
    )
    assert [ref["kind"] for ref in conditioning[0][1]["minimax_refs"]] == [
        "image"
    ]


def test_hybrid_conditioning_cache_scopes_first_to_sequence_start_and_last_to_final(monkeypatch):
    from types import SimpleNamespace

    from ComfyUI_H3_Continuum_Join import reference
    from ComfyUI_H3_Continuum_Join.v2 import sequence

    calls = []

    def fake_encode(_clip, prompt, _references, **kwargs):
        calls.append((prompt, kwargs["first_image"], kwargs["last_image"]))
        return [[torch.zeros(1), {}]]

    monkeypatch.setattr(reference, "encode_reference_prompt", fake_encode)
    assets = SimpleNamespace(first_image="first", last_image="last")
    cache = {}
    sequence._conditioning_cache(
        clip=object(),
        prompts=["opening"],
        assets=assets,
        final_has_last_frame=False,
        reference_assets=object(),
        reference_audio_assets=None,
        timeline_video_assets=None,
        include_first_frame=True,
        cache=cache,
    )
    sequence._conditioning_cache(
        clip=object(),
        prompts=["finish"],
        assets=assets,
        final_has_last_frame=True,
        reference_assets=object(),
        reference_audio_assets=None,
        timeline_video_assets=None,
        include_first_frame=False,
        cache=cache,
    )

    assert calls == [
        ("opening", "first", None),
        ("finish", None, "last"),
    ]


def test_hybrid_conditioning_cache_separates_initial_and_continuation_presentations(monkeypatch):
    from types import SimpleNamespace

    from ComfyUI_H3_Continuum_Join import reference
    from ComfyUI_H3_Continuum_Join.v2 import sequence

    calls = []

    def fake_encode(_clip, prompt, _references, **kwargs):
        calls.append((prompt, kwargs["first_image"], kwargs["last_image"]))
        return [[torch.zeros(1), {}]]

    monkeypatch.setattr(reference, "encode_reference_prompt", fake_encode)
    assets = SimpleNamespace(first_image="first", last_image=None)
    cache = {}
    common = {
        "clip": object(),
        "prompts": ["same"],
        "assets": assets,
        "final_has_last_frame": False,
        "reference_assets": object(),
        "reference_audio_assets": None,
        "timeline_video_assets": None,
        "cache": cache,
    }
    sequence._conditioning_cache(**common, include_first_frame=True)
    sequence._conditioning_cache(**common, include_first_frame=False)

    assert calls == [
        ("same", "first", None),
        ("same", None, None),
    ]
    assert set(cache) == {
        ("same", True, False),
        ("same", False, False),
    }


def test_native_masked_hybrid_drops_only_prefix_keyframe_and_keeps_reference_and_last():
    _clip, conditioning, _first_latent, last_latent = _hybrid_conditioning()

    output = prepare_masked_conditioning(
        conditioning,
        context_frames=39,
        new_frame_count=141,
    )
    metadata = output[0][1]

    assert [ref["kind"] for ref in metadata["minimax_refs"]] == ["image"]
    assert metadata["minimax_keyframes"] == [
        {"resolved_frame_index": 140, "latent": last_latent}
    ]
    assert metadata["minimax_frame_count"] == 141


def test_guide_hybrid_replaces_first_with_context_and_keeps_reference_and_last():
    _clip, conditioning, _first_latent, last_latent = _hybrid_conditioning(
        frame_count=124
    )

    output = prepare_conditioning(
        conditioning,
        video_context=torch.zeros((1, 24, 7, 2, 2)),
        audio_context=None,
        audio_grid_offset=0.0,
        context_frames=22,
        new_frame_count=141,
        first_frame_policy=POLICY_REPLACE,
        preserve_last_frame=True,
    )
    metadata = output[0][1]

    assert metadata["minimax_keyframes"] == [
        {"resolved_frame_index": 140, "latent": last_latent}
    ]
    refs = metadata["minimax_refs"]
    assert [ref["kind"] for ref in refs] == ["image", "video"]
    assert refs[1][MARK_VIDEO_CONTEXT] is True
    assert metadata["minimax_frame_count"] == 141


def _patch_sampling_observation(monkeypatch):
    import ComfyUI_H3_Continuum_Join.run_storage as storage

    monkeypatch.setattr(storage, "_model_signature", lambda *a, **k: ({"model": 1}, True))
    monkeypatch.setattr(storage, "_clip_signature", lambda *a, **k: ({"clip": 1}, True))
    monkeypatch.setattr(
        storage,
        "_video_vae_signature",
        lambda *a, **k: ({"video_vae": 1}, True),
    )
    monkeypatch.setattr(storage, "_sampler_signature", lambda *a, **k: ({"sampler": 1}, True))
    return storage


def _sampling_contract(
    monkeypatch,
    *,
    has_first: bool,
    has_last: bool,
    first_hash: str = "a" * 64,
    last_hash: str = "b" * 64,
):
    storage = _patch_sampling_observation(monkeypatch)
    reference_contract = _reference_assets().contract
    mode = conditioning_mode_from_presence(
        has_first=has_first,
        has_last=has_last,
        has_reference=True,
    )
    contract, safe, reasons = storage.build_sampling_contract(
        model=object(),
        model_fingerprint_value="model",
        clip=object(),
        video_vae=object(),
        sampler=object(),
        sigmas=torch.tensor([1.0, 0.0]),
        prompt_plan={
            "chunks": 3,
            "hashes": ["3" * 64, "4" * 64, "5" * 64],
            "mode": "fixed",
        },
        width=96,
        height=64,
        chunk_seconds=5.0,
        continuity="Strong — 39 frames (Experimental)",
        audio_continuity=True,
        base_seed=1,
        reroll_from_chunk=0,
        reroll_nonce=0,
        first_frame_hash=first_hash if has_first else "none",
        last_frame_hash=last_hash if has_last else "none",
        strict_compatibility=False,
        reference_contract=reference_contract,
        conditioning_mode=mode,
    )
    assert safe, reasons
    return contract


def test_run_storage_hybrid_first_and_reference_fingerprint_both_inputs(monkeypatch):
    first = _sampling_contract(
        monkeypatch,
        has_first=True,
        has_last=False,
        first_hash="a" * 64,
    )
    changed = _sampling_contract(
        monkeypatch,
        has_first=True,
        has_last=False,
        first_hash="c" * 64,
    )

    assert first["global"]["conditioning_mode"] == "i2va"
    assert first["global"]["first_frame_hash"] == "a" * 64
    assert first["global"]["reference"] == _reference_assets().contract
    assert first["chunk_contract_hashes"] != changed["chunk_contract_hashes"]


def test_run_storage_hybrid_last_and_reference_invalidates_only_final_chunk(monkeypatch):
    first = _sampling_contract(
        monkeypatch,
        has_first=False,
        has_last=True,
        last_hash="b" * 64,
    )
    changed = _sampling_contract(
        monkeypatch,
        has_first=False,
        has_last=True,
        last_hash="d" * 64,
    )

    assert first["global"]["conditioning_mode"] == "last_only"
    assert first["global"]["reference"] == _reference_assets().contract
    assert first["chunk_contract_hashes"][:2] == changed["chunk_contract_hashes"][:2]
    assert first["chunk_contract_hashes"][2] != changed["chunk_contract_hashes"][2]


def test_run_storage_first_last_reference_tracks_first_globally_and_last_at_tail(monkeypatch):
    baseline = _sampling_contract(
        monkeypatch,
        has_first=True,
        has_last=True,
        first_hash="a" * 64,
        last_hash="b" * 64,
    )
    changed_first = _sampling_contract(
        monkeypatch,
        has_first=True,
        has_last=True,
        first_hash="c" * 64,
        last_hash="b" * 64,
    )
    changed_last = _sampling_contract(
        monkeypatch,
        has_first=True,
        has_last=True,
        first_hash="a" * 64,
        last_hash="d" * 64,
    )

    assert baseline["global"]["conditioning_mode"] == "fl2va"
    assert baseline["global"]["first_frame_hash"] == "a" * 64
    assert baseline["global"]["reference"] == _reference_assets().contract
    assert baseline["chunk_contract_hashes"] != changed_first["chunk_contract_hashes"]
    assert baseline["chunk_contract_hashes"][:2] == changed_last["chunk_contract_hashes"][:2]
    assert baseline["chunk_contract_hashes"][2] != changed_last["chunk_contract_hashes"][2]

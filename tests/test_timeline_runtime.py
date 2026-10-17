"""Exercise the actual native sequence loop with deterministic CPU sample fixtures."""

from types import SimpleNamespace
import torch
import pytest
from ComfyUI_H3_Continuum_Join.constants import DIAGNOSTICS_BASIC, V2_CONTINUITY_OPTIONS
from ComfyUI_H3_Continuum_Join.v2 import sequence
from ComfyUI_H3_Continuum_Join.v2.h3_builder import IdentityAssets
from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan
from ComfyUI_H3_Continuum_Join.temporal import video_latent_t, audio_latent_t
from ComfyUI_H3_Continuum_Join.timeline.model import (
    blank_project,
    compile_project,
    validate,
    digest,
)
from ComfyUI_H3_Continuum_Join.timeline.runtime import TimelineInputs
from ComfyUI_H3_Continuum_Join.reference import ReferenceAssets
from ComfyUI_H3_Continuum_Join.masked_continuation import (
    CONTINUATION_GUIDE,
    continuation_method_scope,
)


class Nested:
    def __init__(self, parts):
        self.parts = parts

    def unbind(self):
        return self.parts


class Clip:
    def __init__(self):
        self.inputs = []

    def tokenize(self, text, **kwargs):
        self.inputs.append((text, kwargs))
        return text

    def encode_from_tokens_scheduled(self, tokens):
        return [[torch.zeros(1, 2, 3), {}]]


@pytest.mark.parametrize("physical", ["0", "1"])
def test_references_materialize_at_actual_invocation_and_invalidate_suffix(
    monkeypatch, physical
):
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", physical)
    p = blank_project()
    p.update(width=64, height=64)
    windows = compile_project(p)["jobs"][0]
    provider = TimelineInputs(p, windows, None)
    calls = []

    def materialize(index, **kwargs):
        calls.append(index)
        value = torch.full((1, 64, 64, 3), float(index))
        refs = ReferenceAssets(
            (value,),
            (torch.zeros(1, 24, 2, 4, 4),),
            (digest(index),),
            digest(index),
            "Match Output",
        )
        return refs, None, None, None, None

    provider.materialize = materialize
    provider.checkpoint = lambda *a: None
    assets = IdentityAssets(None, None, None, None, "none")
    monkeypatch.setattr(sequence, "prepare_identity_assets", lambda *a, **k: assets)
    monkeypatch.setattr(sequence, "encode_identity_latents", lambda *a, **k: assets)
    monkeypatch.setattr(sequence, "check_comfy_h3_runtime", lambda: [])
    monkeypatch.setattr(sequence, "accelerator_summary", lambda _: "CPU test")
    monkeypatch.setattr(sequence, "clone_model_for_chunk", lambda model, **k: model)

    def empty(w, h, n):
        return {
            "samples": Nested(
                [
                    torch.zeros(1, 24, video_latent_t(n), h // 16, w // 16),
                    torch.zeros(1, 32, 2, audio_latent_t(n)),
                ]
            )
        }

    monkeypatch.setattr(sequence, "empty_h3_latent", empty)
    monkeypatch.setattr(sequence, "sample_chunk", lambda **kwargs: kwargs["latent"])
    model = SimpleNamespace(
        model=SimpleNamespace(diffusion_model=SimpleNamespace()),
        model_options={},
        wrappers={},
        model_dtype=lambda: torch.float32,
        model_size=lambda: 0,
    )
    clip = Clip()
    plan = make_prompt_plan(mode="Fixed", script="same text", chunks=3, chunk_seconds=7)
    args = dict(
        model=model,
        clip=clip,
        video_vae=None,
        audio_vae=None,
        sampler=None,
        sigmas=torch.tensor([1.0, 0.0]),
        first_frame=None,
        last_frame=None,
        prompt_plan=plan,
        width=64,
        height=64,
        continuity=V2_CONTINUITY_OPTIONS[1],
        base_seed=1,
        audio_continuity=True,
        exact_total_duration=False,
        diagnostics_mode=DIAGNOSTICS_BASIC,
        reroll_from_chunk=0,
        reroll_nonce=0,
        strict_compatibility=False,
        debug=False,
        latent_only=True,
        timeline_inputs=provider,
    )
    with continuation_method_scope(CONTINUATION_GUIDE):
        entries, _, session, _ = sequence.run_sequence(**args)
    assert calls == [0, 1, 2]
    assert len(clip.inputs) == 3
    assert [
        float(options["minimax_ref_items"][0]["data"][0, 0, 0, 0])
        for _, options in clip.inputs
    ] == [0, 1, 2]
    assert len(provider.manifests) == 3
    assert provider.manifests[0]["descriptor"]["total_frames"] == 175
    assert provider.manifests[1]["window"]["range"] == [168, 336]
    calls.clear()
    provider.manifests = []
    provider.windows[1]["generation_hash"] = digest("changed reference")
    with continuation_method_scope(CONTINUATION_GUIDE):
        entries, _, _, _ = sequence.run_sequence(**args, session=session)
    assert calls == [1, 2]
    assert entries[0]["reused"] is True


def test_final_audio_only_changes_do_not_change_sampling_hash():
    def project():
        from ComfyUI_H3_Continuum_Join.timeline.model import validate

        p = blank_project()
        p["assets"] = [{"id": "a" * 64}]
        p["tracks"] = [
            {
                "id": "video",
                "role": "video_guide",
                "clips": [
                    {
                        "id": "clip",
                        "asset_id": "a" * 64,
                        "start": 0,
                        "end": 336,
                        "source_in": "0",
                        "speed": "1",
                        "padding": "hold",
                    }
                ],
            }
        ]
        return validate(p)

    from ComfyUI_H3_Continuum_Join.timeline.model import compile_window

    p = project()
    c = p["tracks"][0]["clips"][0]
    c["audio_role"] = "final"
    a = compile_window(p, 0)
    c["gain"] = "0.2"
    c["fade_out"] = "1"
    b = compile_window(p, 0)
    assert a["generation_hash"] == b["generation_hash"]
    assert a["final_hash"] != b["final_hash"]


@pytest.mark.parametrize("physical", ["0", "1"])
def test_encoder_cache_reuses_identical_inputs_but_not_changed_references(
    monkeypatch, physical
):
    from ComfyUI_H3_Continuum_Join.v2.physical_runtime import (
        encode_physical_prompt_conditioning,
        build_presentation_contract,
    )
    from ComfyUI_H3_Continuum_Join.v2.physical_prompts import (
        make_physical_sample_descriptor,
    )

    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", physical)
    assets = IdentityAssets(None, None, None, None, "none")
    clip = Clip()
    cache = {}
    plan = make_prompt_plan(mode="Fixed", script="same text", chunks=3, chunk_seconds=7)
    metadata = []
    for i in range(3):
        refs = None
        if i == 2:
            refs = ReferenceAssets(
                (torch.zeros(1, 64, 64, 3),),
                (torch.zeros(1, 24, 2, 4, 4),),
                (digest("image"),),
                digest("set"),
                "Match Output",
            )
        presentation = build_presentation_contract(
            assets=assets,
            include_first=False,
            include_last=False,
            reference_assets=refs,
        )
        presentation["timeline_studio"] = {
            "logical_range": [i * 168, (i + 1) * 168],
            "generation_hash": digest(i),
        }
        d = make_physical_sample_descriptor(
            group_id=f"chunk:{i + 1}",
            logical_indices=(i,),
            retained_before=i * 175,
            context_frames=0,
            total_frames=175,
            target_duration_frames=504,
            continuation_method=CONTINUATION_GUIDE,
            initial_state_origin="sequence",
            include_first=False,
            include_last=False,
            presentation_contract=presentation,
        )
        _, _, meta, _ = encode_physical_prompt_conditioning(
            clip=clip,
            plan=plan,
            descriptor=d,
            legacy_text="same text",
            assets=assets,
            include_first=False,
            include_last=False,
            reference_assets=refs,
            cache=cache,
        )
        metadata.append(meta)
    assert len(clip.inputs) == 2
    assert metadata[0]["descriptor_digest"] != metadata[1]["descriptor_digest"]


def _timed_project():
    p = blank_project()
    p["prompts"]["global"] = "A woman walks."
    p["assets"] = [{"id": "a" * 64, "kind": "image", "alias": "Alice"}]
    p["tracks"] = [
        {
            "id": "pr",
            "role": "prompt",
            "clips": [
                {"id": "c1", "start": 0, "end": 84, "text": "she smiles"},
                {"id": "c2", "start": 200, "end": 300, "text": "@Alice turns"},
            ],
        },
        {
            "id": "ref",
            "role": "image_reference",
            "clips": [{"id": "r", "asset_id": "a" * 64, "start": 336, "end": 504}],
        },
    ]
    return validate(p)


def test_timed_regions_are_rebased_onto_each_physical_invocation_clock():
    from ComfyUI_H3_Continuum_Join.v2.physical_prompts import (
        compile_physical_prompt,
        make_physical_sample_descriptor,
    )

    plan = compile_project(_timed_project())
    provider = TimelineInputs(plan["project"], plan["jobs"][0], None)
    timeline = provider.invocation_plan({}, 1)
    descriptor = make_physical_sample_descriptor(
        group_id="g",
        logical_indices=[1],
        retained_before=175,
        context_frames=22,
        total_frames=192,
        target_duration_frames=504,
        continuation_method="native_masked",
        initial_state_origin="none",
        include_first=False,
        include_last=False,
        presentation_contract={},
    )
    text = compile_physical_prompt(timeline, descriptor).text
    # Region [200,300) starts 25 frames after the 175-frame first chunk and
    # 22 native context frames precede it inside this sample.
    assert "[1.958333-6.125s]\nA woman walks.\n@Alice turns" in text
    assert "she smiles" not in text and "(no prompt)" not in text
    assert "8.333" not in text


def test_override_replaces_inherited_text_only_inside_its_span():
    p = _timed_project()
    p["prompts"].update(mode="List", script='["inherited one", "inherited two"]')
    p["tracks"][0]["clips"] = [
        {"id": "o", "start": 42, "end": 84, "text": "override", "override": True}
    ]
    window = compile_project(p)["jobs"][0]["windows"][0]
    assert [s["text"] for s in window["segments"]] == [
        "A woman walks.\ninherited one",
        "A woman walks.\noverride",
        "A woman walks.\ninherited one",
    ]


def test_alias_without_active_reference_warns_and_keeps_text():
    windows = compile_project(_timed_project())["jobs"][0]["windows"]
    assert "@Alice turns" in windows[1]["segments"][1]["text"]
    assert any("@Alice" in w for w in windows[1]["warnings"])
    assert not windows[2]["warnings"]

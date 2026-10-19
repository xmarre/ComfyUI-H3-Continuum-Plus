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
    monkeypatch.setenv("H3C_CONDITIONING_LOOKAHEAD", "0")
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


def test_authored_strict_inner_timeline_uses_native_physical_clock():
    p = blank_project()
    p["prompts"].update(
        mode="Timeline",
        script="[0-7s]\nfirst\n[7-14s]\n7-10s:\nturn\n10-14s:\nwalk\n[14-21s]\nlast",
    )
    plan = compile_project(p)
    provider = TimelineInputs(plan["project"], plan["jobs"][0], None)
    assert provider.windows[1]["timed_prompt"]
    assert [s["range"] for s in provider.windows[1]["segments"]] == [
        [168, 240],
        [240, 336],
    ]
    from ComfyUI_H3_Continuum_Join.v2.physical_prompts import (
        make_physical_sample_descriptor,
    )
    from ComfyUI_H3_Continuum_Join.v2.physical_runtime import compile_invocation_prompt

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
    compiled = compile_invocation_prompt(
        provider.invocation_plan({}, 1), descriptor, legacy_text="", candidate=True
    )
    text = compiled.text
    assert "[0-3.625s]\nturn" in text
    assert "first" not in text and "last" not in text


def test_audio_scope_follows_actual_multi_job_output_offset():
    from ComfyUI_H3_Continuum_Join.timeline.model import compile_window

    p = blank_project()
    p.update(
        duration_frames=168 * 200, selection=[0, 168 * 200], assets=[{"id": "a" * 64}]
    )
    late = 168 * 144 + 100
    p["tracks"] = [
        {
            "id": "audio",
            "role": "audio",
            "clips": [
                {
                    "id": "bed",
                    "asset_id": "a" * 64,
                    "start": 0,
                    "end": 168 * 200,
                    "audio_role": "guide",
                },
                {
                    "id": "later",
                    "asset_id": "a" * 64,
                    "start": late,
                    "end": late + 20,
                    "audio_role": "guide",
                },
            ],
        }
    ]
    plan = compile_project(p)
    provider = TimelineInputs(plan["project"], plan["jobs"][8], None)
    provider.set_offset(168 * 128 + 120)
    scope = provider.job["guide_scope"]
    assert scope == [168 * 128 + 120, 168 * 144 + 154]
    before = provider.hashes
    changed = validate(p)
    changed["tracks"][0]["clips"][1]["gain"] = "0.2"
    assert all(
        compile_window(changed, i, guide_scope=scope)["generation_hash"] != old
        for i, old in zip(range(128, 144), before)
    )


def test_rejected_causal_parent_invalidates_descendant():
    from ComfyUI_H3_Continuum_Join.timeline.nodes import stale_take

    p = blank_project()
    windows = compile_project(p)["jobs"][0]["windows"]

    def record(index, parent=None):
        return {
            "id": str(index + 1) * 32,
            "indices": [index],
            "input_hashes": [windows[index]["generation_hash"]],
            "guide_scope": [0, 538],
            "parent_take": parent,
        }

    parent = record(0)
    child = record(1, parent["id"])
    assert not stale_take(p, child, [parent, child])
    p["rejected"] = [parent["id"]]
    assert stale_take(p, child, [parent, child])


def test_normal_queue_after_reject_requests_native_auto_nonce_without_reusing_rejected_schedule():
    from ComfyUI_H3_Continuum_Join.timeline.nodes import take_seed_request

    p = blank_project()
    p["selection"] = [168, 336]
    p["accepted_windows"] = [{"first_chunk": 0, "stop_chunk": 3, "take_id": "b" * 32}]
    job = compile_project(p)["jobs"][0]
    latest = {
        "id": "a" * 32,
        "indices": [job["start_chunk"]],
        "settings_hash": "settings",
    }
    p["rejected"] = [latest["id"]]
    boundary, nonce, note = take_seed_request(
        p, job, [latest], None, "settings", "storage", 0, 7
    )
    assert boundary == 2 and nonce == 0 and "fresh" in note
    # A resumed causal job must start at its first physical invocation.
    job["requires_previous_job"] = True
    assert take_seed_request(p, job, [latest], None, "settings", "storage", 0, 7)[
        :2
    ] == (1, 0)
    # User-entered explicit reroll parameters retain deterministic replay.
    assert take_seed_request(p, job, [latest], None, "settings", "storage", 1, 7)[
        :2
    ] == (1, 7)


def test_normal_queue_restores_selected_reroll_seed_contract():
    from ComfyUI_H3_Continuum_Join.timeline.nodes import take_seed_request
    from ComfyUI_H3_Continuum_Join.run_storage import _apply_nonce_contract

    p = blank_project()
    job = compile_project(p)["jobs"][0]
    selected = {
        "storage_name": "storage",
        "sampling_contract": {"reroll_from_chunk": 2, "effective_reroll_nonce": 7},
    }
    boundary, nonce, _ = take_seed_request(
        p, job, [], selected, "settings", "storage", 0, 0
    )
    assert (boundary, nonce) == (2, 7)
    contract = {
        "global": {},
        "prompt_hashes": ["a", "b", "c"],
        "chunk_count": 3,
        "last_frame_hash": "",
        "nonce_lineage_sha256": "x",
        "nonce_request_sha256": "y",
    }
    original = _apply_nonce_contract(
        {**contract, "reroll_from_chunk": 2}, requested_nonce=7, effective_nonce=7
    )
    restored = _apply_nonce_contract(
        {**contract, "reroll_from_chunk": boundary},
        requested_nonce=nonce,
        effective_nonce=nonce,
    )
    assert original["chunk_contract_hashes"] == restored["chunk_contract_hashes"]
    baseline = _apply_nonce_contract(
        {**contract, "reroll_from_chunk": 0}, requested_nonce=0, effective_nonce=0
    )
    assert original["chunk_contract_hashes"] != baseline["chunk_contract_hashes"]


def test_conveyor_connections_feed_only_selected_refs_per_chunk_and_preserve_order():
    p = blank_project()
    p["conveyor_reference_slots"] = {"0": [3], "1": [], "2": [1, 3]}
    p = validate(p)
    provider = TimelineInputs(p, compile_project(p)["jobs"][0], None)
    # Socket 1 contains a V3.4 bundle of two references; socket 3 contains one.
    provider.reference_slot_sizes = (2, 0, 1, 0, 0, 0, 0, 0)
    provider.set_offset(0)
    assert [provider.picture_offset_for(i) for i in range(3)] == [1, 0, 3]
    refs = ReferenceAssets(
        images=tuple(torch.full((1, 8, 8, 3), x) for x in (0.1, 0.2, 0.3)),
        latents=(None, None, None),
        image_hashes=("image-a", "image-b", "image-c"),
        combined_hash=digest(["image-a", "image-b", "image-c"]),
        size_mode="Match Output",
    )
    only_c = provider.select_persistent_references(refs, 0)
    assert only_c.image_hashes == ("image-c",)
    assert torch.equal(only_c.images[0], refs.images[2])
    assert provider.select_persistent_references(refs, 1) is None
    assert provider.select_persistent_references(refs, 2).image_hashes == (
        "image-a", "image-b", "image-c"
    )

    # The same queued project without explicit selections inherits every
    # connected Image Conveyor output, preserving all existing workflows.
    legacy = blank_project()
    legacy_provider = TimelineInputs(
        legacy, compile_project(legacy)["jobs"][0], None
    )
    legacy_provider.reference_slot_sizes = provider.reference_slot_sizes
    legacy_provider.set_offset(0)
    assert legacy_provider.select_persistent_references(refs, 0) is refs
    assert legacy_provider.picture_offset_for(0) == 3


def test_conveyor_chunk_mask_changes_stale_take_hash_and_picture_alias_offsets():
    from ComfyUI_H3_Continuum_Join.timeline.nodes import stale_take

    p = blank_project()
    img = "a" * 64
    p["assets"] = [{"id": img, "alias": "Alice"}]
    p["tracks"] = [{
        "id": "ref", "role": "image_reference",
        "clips": [{"id": "clip", "asset_id": img, "start": 0, "end": 336}],
    }]
    p["prompts"]["global"] = "@Alice"
    p["conveyor_reference_slots"] = {"0": [3], "1": []}
    p = validate(p)
    provider = TimelineInputs(p, compile_project(p)["jobs"][0], None)
    provider.reference_slot_sizes = (1, 0, 1, 0, 0, 0, 0, 0)
    provider.set_offset(0)
    assert provider.windows[0]["aliases"] == {"Alice": "<Picture 2>"}
    assert provider.windows[1]["aliases"] == {"Alice": "<Picture 1>"}
    record = {
        "id": "c" * 32, "indices": [0, 1],
        "input_hashes": provider.hashes,
        "picture_offset": 2,
        "reference_slot_sizes": list(provider.reference_slot_sizes),
    }
    assert not stale_take(p, record, [record])
    p["conveyor_reference_slots"]["1"] = [1]
    assert stale_take(p, record, [record])

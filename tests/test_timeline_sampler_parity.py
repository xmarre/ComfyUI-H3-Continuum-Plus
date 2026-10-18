"""Public Timeline facade through the real native CPU sequence/storage loop.

Fixtures replace trained encoders/sampling, not parameter routing or persistence.
"""

import copy
from types import SimpleNamespace

import pytest
import torch
from PIL import Image

from ComfyUI_H3_Continuum_Join import run_storage
from ComfyUI_H3_Continuum_Join.constants import V2_CONTINUITY_OPTIONS
from ComfyUI_H3_Continuum_Join.masked_continuation import CONTINUATION_GUIDE
from ComfyUI_H3_Continuum_Join.temporal import video_latent_t, audio_latent_t
from ComfyUI_H3_Continuum_Join.timeline import nodes
from ComfyUI_H3_Continuum_Join.timeline.media import ProjectStore
from ComfyUI_H3_Continuum_Join.timeline.model import blank_project, compile_project
from ComfyUI_H3_Continuum_Join.v2 import sequence, session_io
from ComfyUI_H3_Continuum_Join.v3.driving_nodes import (
    H3ContinuumSamplerV34,
    _DRIVING_AUDIO_PLAN_KEY,
)
from tests.test_native_masked_sequence import _install_common_runtime
from tests.test_timeline_runtime import Clip
from tests.test_v31_run_storage import _prompt_graph


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    events = []
    native = _install_common_runtime(monkeypatch, events)
    store = ProjectStore(tmp_path / "input")
    monkeypatch.setattr(nodes, "project_store", lambda: store)
    monkeypatch.setattr(run_storage, "_output_root", lambda: tmp_path / "output")
    monkeypatch.setattr(session_io, "session_directory", lambda: tmp_path)
    native["model"].model.diffusion_model = torch.nn.Linear(1, 1)
    clip = Clip()
    clip.cond_stage_model = torch.nn.Linear(1, 1)
    clip.patcher = SimpleNamespace()
    clip.tokenizer = SimpleNamespace(
        qwen3vl_32b=SimpleNamespace(tokenizer=SimpleNamespace())
    )
    video = SimpleNamespace(
        first_stage_model=torch.nn.Linear(1, 1), patcher=SimpleNamespace()
    )
    video.encode = lambda images: torch.zeros(
        1, 24, 1 if images.shape[0] == 1 else video_latent_t(images.shape[0]), 4, 6
    )
    audio = SimpleNamespace(
        first_stage_model=torch.nn.Linear(1, 1),
        patcher=SimpleNamespace(),
        audio_sample_rate=240,
    )
    audio.encode = lambda pcm: torch.zeros(1, 32, 2, round(pcm.shape[1] * 40 / 240))
    p = blank_project()
    p.update(
        width=96, height=64, chunk_frames=120, duration_frames=360, selection=[0, 360]
    )
    p["prompts"] = {
        "global": "",
        "mode": "List",
        "script": '["first", "second", "third"]',
    }
    graph = _prompt_graph()
    graph["31"] = {
        "class_type": "VAELoader",
        "inputs": {"vae_name": "minimax_h3_audio_vae.safetensors"},
    }
    graph["42"]["class_type"] = "H3ContinuumTimelineSampler"
    graph["42"]["inputs"]["audio_vae"] = ["31", 0]
    args = dict(
        timeline=compile_project(p),
        model=native["model"],
        clip=clip,
        video_vae=video,
        audio_vae=audio,
        sampler=SimpleNamespace(sampler_function=lambda: None),
        sigmas=native["sigmas"],
        job_index=0,
        base_seed=42,
        continuity=V2_CONTINUITY_OPTIONS[1],
        continuation_method=CONTINUATION_GUIDE,
        regenerate_from=0,
        reroll_nonce=0,
        emit_refine_state=False,
        diagnostics="Basic",
        conditioning_lookahead=False,
        prompt=graph,
        unique_id="42",
    )
    calls = []
    original = sequence.sample_chunk

    def sample(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(sequence, "sample_chunk", sample)
    return args, calls, events, store, tmp_path


def test_every_native_control_has_an_explicit_timeline_owner():
    native = H3ContinuumSamplerV34.INPUT_TYPES()
    schema = nodes.H3ContinuumTimelineSampler.INPUT_TYPES()
    available = {**schema["required"], **schema["optional"]}
    owners = {
        "chunks": "Studio duration / selection",
        "chunk_seconds": "Studio chunk length",
        "reroll_from_chunk": "regenerate_from",
        "emit_refine_conditioning": "emit_refine_state",
    }
    assert set(native["required"]) | set(native["optional"]) <= set(available) | set(
        owners
    )
    for name in (
        "audio_continuity",
        "reference_size",
        "video_reference_size",
        "conditioning_lookahead",
        "width",
        "height",
    ):
        assert available[name] == {**native["required"], **native["optional"]}[name]
    assert schema["required"]["base_seed"][1]["control_after_generate"]
    assert nodes.H3ContinuumTimelineSampler.RETURN_NAMES[-1] == "driving_audio"


def test_dimensions_reach_native_latents_and_invalidate_storage(runtime):
    args, calls, _, _, _ = runtime
    args.update(width=128, height=96)
    original = copy.deepcopy(args["timeline"])
    first = nodes.H3ContinuumTimelineSampler().sample(**args)
    assert first[0][0]["samples"].shape[-2:] == (6, 8)
    assert first[3]["source_canvas"] == [96, 64]
    assert first[3]["generation_size"] == [128, 96]
    assert args["timeline"] == original
    second = nodes.H3ContinuumTimelineSampler().sample(**args)
    assert second[3]["reused_chunks"] == 3
    assert len(calls) == 3
    args["width"] = 160
    third = nodes.H3ContinuumTimelineSampler().sample(**args)
    assert third[0][0]["samples"].shape[-2:] == (6, 10)
    assert third[3]["reused_chunks"] == 0


def test_dimension_change_never_silently_reuses_a_different_resolution_parent(runtime):
    args, calls, _, _, _ = runtime
    p = args["timeline"]["project"]
    p.update(duration_frames=120 * 17, selection=[0, 120 * 17])
    args["timeline"] = compile_project(p)
    nodes.H3ContinuumTimelineSampler().sample(**args)
    assert len(calls) == 16
    args.update(job_index=1, width=128, height=96)
    with pytest.raises(ValueError, match="preceding job's valid continuation state"):
        nodes.H3ContinuumTimelineSampler().sample(**args)
    assert len(calls) == 16


def test_public_controls_reach_native_sampling_and_storage_off(runtime):
    args, calls, _, store, directory = runtime
    args.update(
        audio_continuity=False,
        show_preview=False,
        debug=True,
        strict_compatibility=True,
        run_storage="Off",
    )
    first = nodes.H3ContinuumTimelineSampler().sample(**args)
    second = nodes.H3ContinuumTimelineSampler().sample(**args)
    assert len(calls) == 6
    assert all(c["enable_preview"] is False for c in calls)
    assert first[3]["sampling_contract"]["global"]["audio_continuity"] is False
    assert first[3]["sampling_contract"]["global"]["strict_compatibility"] is False
    assert first[3]["reused_chunks"] == second[3]["reused_chunks"] == 0
    assert not (directory / "output").exists()
    assert (
        session_io.load_session(prefix=first[3]["session_prefix"], slot=1)["settings"][
            "audio_continuity"
        ]
        is False
    )
    assert not nodes.stale_take(args["timeline"]["project"], first[3], [first[3]])


def test_storage_resume_and_scoped_size_change_regenerates(runtime):
    args, calls, _, _, _ = runtime
    args["run_name"] = "explicit-run"
    first = nodes.H3ContinuumTimelineSampler().sample(**args)
    assert len(calls) == 3
    second = nodes.H3ContinuumTimelineSampler().sample(**args)
    assert second[3]["reused_chunks"] == 3
    assert len(calls) == 3
    assert "explicit-run" in first[3]["storage_name"]
    args["reference_size"] = "Max Identity"
    third = nodes.H3ContinuumTimelineSampler().sample(**args)
    assert third[3]["reused_chunks"] == 0
    assert len(calls) == 6
    assert third[3]["settings_hash"] != first[3]["settings_hash"]


def test_socket_references_and_scoped_aliases_are_both_conditioned(runtime):
    args, calls, _, store, directory = runtime
    path = directory / "picture.png"
    Image.new("RGB", (96, 64), (100, 50, 10)).save(path)
    with path.open("rb") as stream:
        asset = store.assets.register(stream, "picture.png")
    p = args["timeline"]["project"]
    p["assets"] = [asset]
    p["prompts"] = {"global": "<Picture 1> meets @Lead", "mode": "Fixed", "script": ""}
    p["tracks"] = [
        {
            "id": "refs",
            "role": "image_reference",
            "clips": [
                {
                    "id": "portrait",
                    "asset_id": asset["id"],
                    "start": 0,
                    "end": 360,
                    "source_in": "0",
                    "source_out": "1",
                    "alias": "Lead",
                }
            ],
        }
    ]
    args["timeline"] = compile_project(p)
    args["reference_image_8"] = torch.ones(1, 64, 96, 3)
    result = nodes.H3ContinuumTimelineSampler().sample(**args)
    assert len(calls) == 3
    for text, kwargs in args["clip"].inputs:
        assert len(kwargs["minimax_ref_items"]) == 2
        assert float(kwargs["minimax_ref_items"][0]["data"][0, 0, 0, 0]) == 1
        assert "<Picture 2>" in text
    assert result[3]["picture_offset"] == 1
    assert not nodes.stale_take(args["timeline"]["project"], result[3], [result[3]])


def test_driving_audio_uses_project_clock_and_preserves_final_source(runtime):
    args, calls, _, _, _ = runtime
    p = args["timeline"]["project"]
    p["selection"] = [120, 360]
    args["timeline"] = compile_project(p)
    source = torch.arange(3600, dtype=torch.float32)[None, None].repeat(1, 2, 1)
    args["driving_audio"] = {"waveform": source, "sample_rate": 240}
    result = nodes.H3ContinuumTimelineSampler().sample(**args)
    assert len(calls) == 2
    selected = result[-1]
    n = selected["waveform"].shape[-1]
    assert torch.equal(selected["waveform"], source[..., 1200 : 1200 + n])
    assert torch.equal(
        result[2][_DRIVING_AUDIO_PLAN_KEY]["waveform"], selected["waveform"]
    )
    assert all(
        any("audio_latent" in k for k in c["conditioning"][0][1]["minimax_keyframes"])
        for c in calls
    )
    assert "overrides Studio guide lanes" in result[4]
    # A full Core AUDIO input can cover a long movie. Only this actual job's
    # encoder slice participates in its identity; unrelated earlier audio does not.
    source[..., :1200] += 10000
    repeated = nodes.H3ContinuumTimelineSampler().sample(**args)
    assert len(calls) == 2
    assert repeated[3]["reused_chunks"] == 2
    assert repeated[3]["settings_hash"] == result[3]["settings_hash"]
    source[..., 1201] += 1
    changed = nodes.H3ContinuumTimelineSampler().sample(**args)
    assert changed[3]["reused_chunks"] == 0
    assert changed[3]["settings_hash"] != result[3]["settings_hash"]


@pytest.mark.parametrize(
    "script,mode,expected",
    [
        ('["alpha", "beta", "gamma"]', "List", ["alpha", "beta", "gamma"]),
        (
            "[0-5s]\nalpha\n[5-10s]\nbeta\n[10-15s]\ngamma",
            "Auto",
            ["alpha", "beta", "gamma"],
        ),
        ("[broken unknown text", "Timeline", ["[broken unknown text"] * 3),
    ],
)
def test_connected_prompt_uses_native_permissive_parser(
    runtime, script, mode, expected
):
    args, _, _, _, _ = runtime
    args.update(
        sequence_prompt=script, prompt_mode=mode, managed_prompt_source_json="{invalid"
    )
    result = nodes.H3ContinuumTimelineSampler().sample(**args)
    session = session_io.load_session(prefix=result[3]["session_prefix"], slot=1)
    assert [e["prompt"].strip() for e in session["chunks"]] == expected
    assert not nodes.stale_take(args["timeline"]["project"], result[3], [result[3]])


def test_timeline_lookahead_preserves_conditioning_and_encode_order(
    runtime, monkeypatch
):
    args, calls, events, _, _ = runtime
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "1")
    args.update(run_storage="Off", conditioning_lookahead=True)
    original = args["clip"].encode_from_tokens_scheduled

    def encode(tokens):
        events.append(("encode", tokens))
        return original(tokens)

    args["clip"].encode_from_tokens_scheduled = encode
    first = nodes.H3ContinuumTimelineSampler().sample(**args)
    order = [e[0] for e in events if e[0] in {"sample", "encode"}]
    assert order == ["encode", "encode", "sample", "sample", "encode", "sample"]
    planned = copy.deepcopy([c["conditioning"] for c in calls])
    events.clear()
    calls.clear()
    args["conditioning_lookahead"] = False
    second = nodes.H3ContinuumTimelineSampler().sample(**args)
    assert [e[0] for e in events if e[0] in {"sample", "encode"}] == [
        "encode",
        "sample",
    ] * 3
    assert [i["descriptor"] for i in first[3]["model_inputs"]] == [
        i["descriptor"] for i in second[3]["model_inputs"]
    ]
    for a, b in zip(planned, [c["conditioning"] for c in calls]):
        assert torch.equal(a[0][0], b[0][0])


def test_connected_list_routes_after_chunk_sixteen(runtime):
    import json

    args, calls, _, _, _ = runtime
    p = args["timeline"]["project"]
    p.update(duration_frames=120 * 18, selection=[120 * 16, 120 * 18])
    args.update(
        timeline=compile_project(p),
        sequence_prompt=json.dumps([f"action {i}" for i in range(18)]),
        prompt_mode="Auto",
    )
    result = nodes.H3ContinuumTimelineSampler().sample(**args)
    session = session_io.load_session(prefix=result[3]["session_prefix"], slot=1)
    assert len(calls) == 2
    assert [e["prompt"].strip() for e in session["chunks"]] == [
        "action 16",
        "action 17",
    ]


def test_persistent_video_is_used_outside_scoped_video_windows(runtime, monkeypatch):
    from ComfyUI_H3_Continuum_Join.reference_video import (
        prepare_reference_video_source,
        encode_reference_video,
    )
    from ComfyUI_H3_Continuum_Join.timeline.runtime import TimelineInputs

    args, calls, _, _, _ = runtime
    original = TimelineInputs.materialize
    scoped = prepare_reference_video_source(
        torch.ones(124, 64, 96, 3),
        target_frames=124,
        output_width=96,
        output_height=64,
        size_mode="Match Output",
    )

    def materialize(self, index, **kwargs):
        refs, video, assets, voice, voice_assets = original(self, index, **kwargs)
        if index == 1:
            video, assets = scoped, encode_reference_video(kwargs["video_vae"], scoped)
        return refs, video, assets, voice, voice_assets

    monkeypatch.setattr(TimelineInputs, "materialize", materialize)
    args.update(
        reference_video_1=torch.zeros(124, 64, 96, 3),
        video_reference_size="Match Output",
    )
    nodes.H3ContinuumTimelineSampler().sample(**args)
    assert len(calls) == 3
    assert [
        float(kwargs["minimax_ref_items"][0]["data"][0, 0, 0, 0])
        for _, kwargs in args["clip"].inputs
    ] == [0, 1, 0]


def test_refine_connection_regenerates_all_chunks_and_aligns_exact_state(
    runtime, monkeypatch
):
    from ComfyUI_H3_Continuum_Join.v2 import sampling
    from ComfyUI_H3_Continuum_Join.v3 import driving_nodes

    args, calls, _, _, _ = runtime
    nodes.H3ContinuumTimelineSampler().sample(**args)
    calls.clear()
    sample = sequence.sample_chunk

    def captured_sample(**kwargs):
        sampling._record_chunk_refine_state(
            model=kwargs["model"], conditioning=kwargs["conditioning"], noise_mask=None
        )
        return sample(**kwargs)

    monkeypatch.setattr(sequence, "sample_chunk", captured_sample)
    monkeypatch.setattr(
        driving_nodes, "_fresh_refine_model", lambda model, **kwargs: model
    )
    args["emit_refine_state"] = True
    result = nodes.H3ContinuumTimelineSampler().sample(**args)
    assert result[3]["reused_chunks"] == 0
    assert len(calls) == len(result[0]) == len(result[1]) == len(result[5]) == 3
    assert all(
        state["positive"] is call["conditioning"]
        for state, call in zip(result[5], calls)
    )

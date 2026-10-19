import copy
import io
from fractions import Fraction
import pytest
from ComfyUI_H3_Continuum_Join.timeline.model import (
    blank_project,
    compile_project,
    compile_window,
    sample_boundary,
    validate,
    with_external_prompt_source,
    reference_slots_needed,
)

A, B = "a" * 64, "b" * 64


def project():
    p = blank_project()
    p["assets"] = [{"id": A, "alias": "Alice"}, {"id": B, "alias": "Bob"}]
    p["tracks"] = [
        {
            "id": "v",
            "role": "video_guide",
            "clips": [
                {
                    "id": "v1",
                    "asset_id": A,
                    "start": 0,
                    "end": 336,
                    "source_in": "0",
                    "speed": "1",
                    "padding": "hold",
                }
            ],
        },
        {
            "id": "refs",
            "role": "image_reference",
            "clips": [
                {"id": "a", "asset_id": A, "start": 0, "end": 336},
                {"id": "b", "asset_id": B, "start": 336, "end": 504},
            ],
        },
    ]
    p["prompts"]["global"] = "@Alice sees @Bob"
    return validate(p)


def test_exact_selections_and_aliases():
    p = project()
    a, b, c = [compile_window(p, i) for i in range(3)]
    assert a["video"][0]["source"] == ["0", "7"]
    assert b["video"][0]["source"] == ["7", "14"]
    assert c["video"] == []
    assert a["aliases"] == {"Alice": "<Picture 1>"}
    assert c["aliases"] == {"Bob": "<Picture 1>"}


def test_causal_metadata_and_audio_identity():
    p = project()
    old = [compile_window(p, i)["generation_hash"] for i in range(3)]
    p["name"] = "Changed title"
    p["ui"] = {"zoom": 3}
    p["tracks"][0]["hidden"] = True
    assert old == [compile_window(p, i)["generation_hash"] for i in range(3)]
    p["tracks"][1]["clips"][1]["asset_id"] = A
    assert old[:2] == [compile_window(p, i)["generation_hash"] for i in range(2)]
    assert old[2] != compile_window(p, 2)["generation_hash"]


def test_sample_boundaries_no_drift():
    for rate in (44100, 48000):
        assert sum(
            sample_boundary(i + 1, rate) - sample_boundary(i, rate) for i in range(1001)
        ) == sample_boundary(1001, rate)


def test_long_project_short_selection():
    p = blank_project()
    p["duration_frames"] = 2 * 3600 * 24
    p["selection"] = [3600 * 24, 3600 * 24 + 168]
    plan = compile_project(p)
    assert len(plan["jobs"]) == 1
    assert len(plan["jobs"][0]["windows"]) == 2
    p["selection"] = [0, 168 * 33]
    assert [len(j["windows"]) for j in compile_project(p)["jobs"]] == [16, 16, 1]


def test_invalid_contracts():
    p = project()
    p["tracks"][0]["clips"][0]["start"] = -1
    with pytest.raises(ValueError):
        validate(p)
    p = project()
    p["project_id"] = "../escape"
    with pytest.raises(ValueError):
        validate(p)


def test_recreate_derives_only_visual_guidance_and_supports_audio_only_spans():
    p = project()
    p["operation"] = "recreate"
    p["tracks"][0]["role"] = "retained"
    a = compile_window(p, 0)
    assert a["video"][0]["source"] == ["0", "7"]
    assert a["guide"] == []
    p["tracks"][0]["clips"][0]["guide_disabled"] = True
    assert compile_window(p, 0)["video"] == []


def test_long_prompt_list_and_exact_timeline_headers():
    p = blank_project()
    p["duration_frames"] = 168 * 33
    p["selection"] = [0, 168 * 33]
    p["prompts"] = {
        "mode": "List",
        "script": __import__("json").dumps([f"chunk {i}" for i in range(33)]),
        "global": "shared",
    }
    assert compile_window(p, 20)["prompt"] == "shared\nchunk 20"
    p["prompts"] = {
        "mode": "Timeline",
        "script": "preamble\n[0-7s]\nONLY_A\n[7-14s]\nONLY_B",
        "global": "global",
    }
    assert "ONLY_B" not in compile_window(p, 0)["prompt"]
    assert "ONLY_A" not in compile_window(p, 1)["prompt"]
    p["prompts"]["script"] = "[broken timeline"
    assert "[broken timeline" in compile_window(p, 0)["prompt"]


def test_selected_internal_chunk_includes_accepted_prefix_for_native_reuse():
    p = blank_project()
    p["selection"] = [168, 336]
    p["accepted_windows"] = [
        {"first_chunk": 0, "stop_chunk": 3, "take_id": "a" * 32, "parent_take": None}
    ]
    jobs = compile_project(p)["jobs"]
    assert jobs[0]["range"] == [0, 504]
    assert len(jobs[0]["windows"]) == 3
    assert jobs[0]["resume_take_id"] == "a" * 32
    assert jobs[0]["requires_previous_job"] is False


def test_take_alternatives_remain_selectable_but_old_causal_suffix_is_stale():
    from ComfyUI_H3_Continuum_Join.timeline.nodes import stale_take

    p = blank_project()
    windows = [compile_window(p, i) for i in range(3)]
    base = {
        "id": "a" * 32,
        "indices": [0, 1, 2],
        "input_hashes": [w["generation_hash"] for w in windows],
        "chunk_hashes": ["prefix", "old-second", "old-third"],
        "chunk_output_ranges": [[0, 175], [175, 328], [328, 515]],
        "parent_take": None,
    }
    alternative = {
        **base,
        "id": "b" * 32,
        "indices": [0, 1],
        "input_hashes": base["input_hashes"][:2],
        "chunk_hashes": ["prefix", "new-second"],
        "chunk_output_ranges": [[0, 175], [175, 345]],
    }
    p["accepted"] = {"0": alternative["id"]}
    records = [base, alternative]
    assert not stale_take(p, base, records)  # valid selectable alternative
    assert not stale_take(p, base, records, end_frame=168, check_selection=True)
    assert stale_take(p, base, records, end_frame=504, check_selection=True)


def test_new_batch_can_follow_its_immutable_parent_before_review_selection():
    from ComfyUI_H3_Continuum_Join.timeline.nodes import (
        stale_take,
        H3ContinuumTimelineSampler,
    )
    import math

    p = blank_project()
    p["duration_frames"] = 168 * 17
    p["selection"] = [0, p["duration_frames"]]
    hashes = [compile_window(p, i)["generation_hash"] for i in range(17)]
    a = {
        "id": "a" * 32,
        "indices": list(range(16)),
        "input_hashes": hashes[:16],
        "chunk_hashes": ["old"] * 16,
        "parent_take": None,
    }
    b = {**a, "id": "b" * 32, "chunk_hashes": ["new"] * 16}
    c = {
        "id": "c" * 32,
        "indices": [16],
        "input_hashes": hashes[16:],
        "chunk_hashes": ["child"],
        "parent_take": b["id"],
    }
    p["accepted"] = {"0": a["id"]}
    assert stale_take(p, c, [a, b, c])
    assert not stale_take(p, c, [a, b, c], ignore_selection=True)
    assert math.isnan(H3ContinuumTimelineSampler.IS_CHANGED(regenerate_from=2))
    assert H3ContinuumTimelineSampler.IS_CHANGED(regenerate_from=0) is False


def test_multiple_semantic_aliases_for_one_image_share_one_picture_slot():
    p = project()
    c = p["tracks"][1]["clips"][0]
    c["alias"] = "Alice"
    p["tracks"][1]["clips"].append({**c, "id": "other-name", "alias": "Lead"})
    p["prompts"]["global"] = "@Alice is the @Lead"
    result = compile_window(p, 0)
    assert len(result["images"]) == 1
    assert result["aliases"] == {"Alice": "<Picture 1>", "Lead": "<Picture 1>"}
    assert result["prompt"] == "<Picture 1> is the <Picture 1>"


@pytest.mark.parametrize(
    "window",
    [
        "bad",
        {"stop_chunk": 1, "take_id": "a" * 32},
        {"first_chunk": -1, "stop_chunk": 1, "take_id": "a" * 32},
        {"first_chunk": 2, "stop_chunk": 2, "take_id": "a" * 32},
        {"first_chunk": 0, "stop_chunk": 1, "take_id": "../x"},
        {"first_chunk": 0, "stop_chunk": 1, "take_id": "a" * 32, "parent_take": 3},
    ],
)
def test_malformed_accepted_windows_are_rejected_as_contract_errors(window):
    p = blank_project()
    p["accepted_windows"] = [window]
    with pytest.raises(ValueError):
        compile_project(p)


def test_guide_audio_edit_outside_a_job_scope_keeps_its_takes_valid():
    from ComfyUI_H3_Continuum_Join.timeline.nodes import stale_take

    p = blank_project()
    p["duration_frames"] = 168 * 20
    p["selection"] = [0, 168 * 20]
    p["assets"] = [{"id": A}]
    guide = {
        "id": "g",
        "asset_id": A,
        "start": 168 * 18,
        "end": 168 * 19,
        "audio_role": "guide",
    }
    p["tracks"] = [{"id": "a", "role": "audio", "clips": [guide]}]
    p = validate(p)
    jobs = compile_project(p)["jobs"]
    first = {
        "id": "a" * 32,
        "indices": [w["index"] for w in jobs[0]["windows"]],
        "input_hashes": [w["generation_hash"] for w in jobs[0]["windows"]],
        "guide_scope": jobs[0]["guide_scope"],
        "parent_take": None,
    }
    later = copy.deepcopy(p)
    later["tracks"][0]["clips"][0]["gain"] = "1/2"  # edit inside job 2 only
    assert not stale_take(validate(later), first, [first])
    near = copy.deepcopy(p)
    near["tracks"][0]["clips"][0].update(start=168 * 16, end=168 * 17)
    assert stale_take(validate(near), first, [first])  # within the encoder margin
    legacy = {k: v for k, v in first.items() if k != "guide_scope"}
    legacy["input_hashes"] = [
        compile_window(p, i)["generation_hash"] for i in legacy["indices"]
    ]
    assert stale_take(
        validate(later), legacy, [legacy]
    )  # unscoped records stay conservative


def test_external_sequence_prompt_preserves_explicit_chunk_prompts_only():
    import json

    p = project()
    p["prompts"]["script"] = "inherited project script"
    p["tracks"].append({
        "id": "chunk-editor-prompts", "role": "prompt", "clips": [
            {
                "id": "legacy", "start": 0, "end": 168,
                "text": "old free-form region", "override": True,
            },
            {
                "id": "authored-chunk-2", "start": 168, "end": 336,
                "text": "precise second chunk", "override": True,
                "chunk_prompt": True,
            },
        ],
    })
    p = validate(p)
    source = {
        "global": "", "mode": "List",
        "script": json.dumps(["external one", "external two", "external three"]),
    }
    merged = with_external_prompt_source(p, source)
    assert compile_window(merged, 0)["prompt"] == "external one"
    assert compile_window(merged, 1)["prompt"] == "precise second chunk"
    assert compile_window(merged, 2)["prompt"] == "external three"
    assert p["prompts"]["script"] == "inherited project script"
    assert len(p["tracks"][-1]["clips"]) == 2

    # A take sampled with the combined prompt must not immediately become
    # falsely stale when its inputs are rechecked against the saved project.
    from ComfyUI_H3_Continuum_Join.timeline.nodes import stale_take
    record = {
        "id": "c" * 32, "indices": [1],
        "input_hashes": [compile_window(merged, 1)["generation_hash"]],
        "prompt_override": source,
    }
    assert not stale_take(p, record, [record])
    p["tracks"][-1]["clips"][1]["text"] = "new second chunk prompt"
    assert stale_take(p, record, [record])


def test_conveyor_reference_selection_is_chunk_causal_and_backward_compatible():
    p = blank_project()
    first = [compile_window(p, i)["generation_hash"] for i in range(3)]
    assert "conveyor_reference_slots" not in compile_window(p, 0)
    p["conveyor_reference_slots"] = {"0": [1, 3], "1": []}
    p = validate(p)
    assert compile_window(p, 0)["conveyor_reference_slots"] == [1, 3]
    assert compile_window(p, 1)["conveyor_reference_slots"] == []
    assert "conveyor_reference_slots" not in compile_window(p, 2)
    hashes = [compile_window(p, i)["generation_hash"] for i in range(3)]
    assert hashes[0] != first[0] and hashes[1] != first[1]
    assert hashes[2] == first[2]
    assert reference_slots_needed(p, [0, 1]) == {1, 3}
    assert reference_slots_needed(p, [1]) == set()
    assert reference_slots_needed(p, [0, 2]) == set(range(1, 9))
    for invalid in (
        {"0": [2, 1]}, {"0": [1, 1]}, {"0": [0]}, {"0": [9]},
        {"0": [True]}, {"0": "1,2"}, {"00": [1]}, {"3": [1]},
    ):
        test = blank_project()
        test["conveyor_reference_slots"] = invalid
        with pytest.raises(ValueError, match="reference selection"):
            validate(test)


def test_muted_chunk_media_removed_from_effective_native_conditioning():
    """Clip activation applies to actual backend guidance and audio."""
    p = project()
    guide = p["tracks"][0]["clips"][0]
    assert len(compile_window(p, 0)["video"]) == 1
    assert len(compile_window(p, 1)["video"]) == 1

    # Two edits of one spanning clip isolate one middle chunk without
    # modifying the source PTS or conditional identity of other chunks.
    p["tracks"][0]["clips"] = [
        {**guide, "id": "before", "start": 0, "end": 168},
        {**guide, "id": "off", "start": 168, "end": 336,
         "source_in": "7", "muted": True},
    ]
    validate(p)
    assert compile_window(p, 0)["video"][0]["source"] == ["0", "7"]
    assert compile_window(p, 1)["video"] == []
    p["tracks"][0]["clips"][1]["muted"] = False
    assert compile_window(p, 1)["video"][0]["source"] == ["7", "14"]

    p["tracks"].append({
        "id": "sound",
        "role": "audio",
        "clips": [{
            "id": "music", "asset_id": B, "start": 168, "end": 336,
            "source_in": "100", "speed": "1", "audio_role": "final",
            "padding": "silence", "muted": True,
        }],
    })
    validate(p)
    assert compile_window(p, 1)["final"] == []
    p["tracks"][-1]["clips"][0]["muted"] = False
    assert len(compile_window(p, 1)["final"]) == 1

def test_scene_anchor_compiles_virtual_chunks_at_exact_movie_frame():
    p = blank_project()
    p.update(duration_frames=24 * 3600, chunk_frames=168,
             chunk_origin_frame=14955, selection=[14955, 14955 + 336])
    plan = compile_project(p)
    assert [w["range"] for w in plan["jobs"][0]["windows"]] == [
        [14955, 15123], [15123, 15291]]
    assert [w["index"] for w in plan["jobs"][0]["windows"]] == [0, 1]
    assert plan["jobs"][0]["range"] == [14955, 15291]
    assert plan["jobs"][0]["guide_scope"] == [14955, 15291 + 34]
    assert compile_window(p, 0)["range"] == [14955, 15123]


def test_scene_anchor_rejects_render_before_start_and_invalid_masks():
    p = blank_project()
    p.update(chunk_origin_frame=170, duration_frames=600,
             selection=[170, 338])
    validate(p)
    p["selection"] = [0, 168]
    with pytest.raises(ValueError, match="precede"):
        validate(p)
    p["selection"] = [170, 338]
    p["conveyor_reference_slots"] = {"3": [1]}
    with pytest.raises(ValueError, match="reference selection"):
        validate(p)


def test_legacy_project_keeps_zero_based_chunk_boundaries():
    p = blank_project()
    del p["chunk_origin_frame"]
    p["selection"] = [100, 450]
    plan = compile_project(p)
    assert [w["range"] for w in plan["jobs"][0]["windows"]] == [
        [0, 168], [168, 336], [336, 504]]

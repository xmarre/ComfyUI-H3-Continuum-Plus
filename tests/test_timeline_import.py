"""Unified import contracts: formats, selective application and scene clocks."""

import copy
import json
import zipfile

import pytest
from ComfyUI_H3_Continuum_Join.timeline.import_project import apply_components
from ComfyUI_H3_Continuum_Join.timeline.import_source import ImportSources
from ComfyUI_H3_Continuum_Join.timeline.media import ProjectStore
from ComfyUI_H3_Continuum_Join.timeline.model import (
    blank_project,
    compile_window,
    validate,
    with_external_prompt_source,
)
from ComfyUI_H3_Continuum_Join.timeline.prompt_import import parse_prompt_source

from tests.test_timeline_bundle import Assets

FORMATS = [
    "Identity\n[0-7s]\nA\n[7-14s]\nB",
    "Identity\n[0-7 sec]\nA\n[7-14 sec]\nB",
    "Identity\n[0.0-7.0s]\nA\n[7.0-14.0s]\nB",
    "Identity\n[00:00-00:07]\nA\n[00:07 to 00:14]\nB",
    "Identity\n[0s to 7s]\nA\n[7s to 14s]\nB",
    "Identity\n0-7s:\nA\n7-14s:\nB",
    "Identity\n[Chunk 1]\nA\n[Chunk 2]\nB",
    "Identity\n[Clip 1]\nA\n[Clip 2]\nB",
    "Identity\nChunk 1:\nA\nChunk 2:\nB",
    "Identity\n## Chunk 1\nA\n## Chunk 2\nB",
    '["A","B"]',
    "A\n---\nB",
    {"global": "Identity", "chunks": ["A", "B"]},
    {"global": "Identity", "chunks": {"1": "A", "2": "B"}},
    {
        "global_prefix": "Identity",
        "segments": [
            {"start_seconds": 0, "end_seconds": 7, "prompt": "A"},
            {"start_seconds": 7, "end_seconds": 14, "prompt": "B"},
        ],
    },
]


@pytest.mark.parametrize("source", FORMATS)
def test_all_formats_compile_the_same_global_and_chunks(source):
    p = blank_project()
    p.update(chunk_origin_frame=2400, duration_frames=2736, selection=[2400, 2736])
    p["prompts"]["global"] = "Identity"
    doc = parse_prompt_source(source)
    result, mapping = apply_components(
        p, doc, [], {"global": doc["global_present"], "chunks": True}
    )
    assert [compile_window(result, i)["prompt"] for i in range(2)] == [
        "Identity\nA",
        "Identity\nB",
    ]
    assert not mapping["surplus"]
    assert p["tracks"] == result["tracks"]


def test_fractional_clock_and_mismatch_become_inner_sections_with_surplus():
    p = blank_project()
    p.update(chunk_origin_frame=2400, duration_frames=2904, selection=[2400, 2904])
    doc = parse_prompt_source(
        "Identity\n[0-5s]\nA\n[5s to 7.5s]\nB\n[0:07.5 to 0:25]\nC"
    )
    out, m = apply_components(p, doc, [], {"global": True, "chunks": True})
    assert [(s["range"], s["text"]) for s in compile_window(out, 0)["segments"]] == [
        ([2400, 2520], "Identity\nA"),
        ([2520, 2568], "Identity\nB"),
    ]
    assert m["notes"] and m["surplus"][-1]["range"] == [2580, 3000]
    assert m["surplus"][-1]["applied_range"] == [2580, 2904]
    assert "[0-5s]" not in compile_window(out, 0)["prompt"]


def test_merge_leaves_uncovered_legacy_chunks_and_all_media_and_takes_unchanged():
    p = blank_project()
    p["tracks"] = [
        {
            "id": "prompts",
            "role": "prompt",
            "clips": [
                {"id": "a", "start": 0, "end": 168, "text": "old A", "override": True},
                {
                    "id": "b",
                    "start": 168,
                    "end": 336,
                    "text": "old B",
                    "override": True,
                },
            ],
        }
    ]
    p["takes"] = [{"id": "history"}]
    p["prompts"]["global"] = "Identity"
    p = validate(p)
    before = copy.deepcopy(p)
    out, m = apply_components(
        p, parse_prompt_source("Chunk 2:\nnew B"), [], {"chunks": True}
    )
    assert out["tracks"] == before["tracks"]
    assert out["assets"] == before["assets"]
    assert out["takes"] == before["takes"]
    assert compile_window(out, 0)["prompt"] == "Identity\nold A"
    assert compile_window(out, 1)["prompt"] == "Identity\nnew B"
    assert m["uncovered"] == [1, 3]
    assert p == before


@pytest.mark.parametrize(
    "source",
    [
        "Identity\n[00:xx-00:07]\nA",
        "Identity\n[7s to nope]\nA",
        "Identity\n[7s to 3s]\nA",
        "Identity\n[00:00-00:99]\nA",
    ],
)
def test_malformed_timed_header_is_visible_and_never_fixed(source):
    with pytest.raises(ValueError, match="Line 2:"):
        parse_prompt_source(source)


def fixture_zip(tmp_path, missing=False, media_only=False):
    f = tmp_path / "source.zip"
    with zipfile.ZipFile(f, "w") as z:
        if not media_only:
            z.writestr(
                "prompts.json",
                json.dumps(
                    {
                        "global_prefix": "Identity",
                        "segments": [
                            {
                                "filename": "chunk_01.mp4",
                                "start_seconds": 0,
                                "end_seconds": 7,
                                "prompt": "A",
                            },
                            {
                                "filename": "chunk_02.mp4",
                                "start_seconds": 7,
                                "end_seconds": 11.96,
                                "prompt": "B",
                            },
                        ],
                    }
                ),
            )
        if not missing:
            z.writestr("chunk_01.mp4", b"picture one")
            z.writestr("chunk_02.mp4", b"picture two")
            z.writestr("original_audio.m4a", b"audio")
    return f


def sources(tmp_path):
    store = ProjectStore(tmp_path / "store")
    fake = Assets(tmp_path)
    store.assets = fake
    return ImportSources(store), fake


@pytest.mark.parametrize("missing", [False, True])
def test_prompts_only_from_full_or_missing_media_zip_registers_no_media(
    tmp_path, missing
):
    importer, assets = sources(tmp_path)
    source = importer.register_source(
        fixture_zip(tmp_path, missing=missing), "source.zip"
    )
    p = blank_project()
    p["takes"] = [{"id": "old"}]
    before = copy.deepcopy(p)
    preview = importer.preview(
        p, token=source["token"], selected={"global": True, "chunks": True}
    )
    assert not assets.calls
    for key in ["tracks", "assets", "takes"]:
        assert preview["project"][key] == before[key]
    assert compile_window(preview["project"], 0)["prompt"] == "Identity\nA"
    assert p == before
    if missing:
        assert any("Missing media" in n for n in preview["notes"])


def test_media_only_keeps_prompts_and_original_take_records_in_merge(tmp_path):
    importer, assets = sources(tmp_path)
    token = importer.register_source(
        fixture_zip(tmp_path, media_only=True), "media.zip"
    )["token"]
    p = blank_project()
    p["prompts"] = {
        "global": "Keep identity",
        "mode": "List",
        "script": '["Keep action"]',
    }
    p["takes"] = [{"id": "existing"}]
    out = importer.preview(
        p, token=token, selected={"video": True, "soundtrack": True}
    )["project"]
    assert out["prompts"] == p["prompts"]
    assert out["takes"] == p["takes"]
    assert len(out["assets"]) == 3
    assert out["tracks"][-1]["clips"][0]["audio_role"] == "both"
    assert all(c["audio_role"] == "neither" for c in out["tracks"][0]["clips"])


def test_soundtrack_only_from_full_pack_keeps_videos_and_prompts(tmp_path):
    importer, assets = sources(tmp_path)
    token = importer.register_source(fixture_zip(tmp_path), "full.zip")["token"]
    p = blank_project()
    out = importer.preview(p, token=token, selected={"soundtrack": True})["project"]
    assert assets.calls == ["original_audio.m4a"]
    assert out["prompts"] == p["prompts"]
    assert len(out["tracks"]) == 1 and out["tracks"][0]["role"] == "audio"


def test_state_manager_adoption_ignores_connected_text_even_after_identity_edit():
    p = blank_project()
    out, _ = apply_components(
        p,
        parse_prompt_source("Identity\n[0-7s]\nA"),
        [],
        {"global": True, "chunks": True},
        provenance={"kind": "state-manager-text-box"},
    )
    out["prompts"]["global"] = "Edited identity"
    compiled = compile_window(
        with_external_prompt_source(
            out, {"global": "", "mode": "Fixed", "script": "Identity"}
        ),
        0,
    )["prompt"]
    assert compiled == "Edited identity\nA"
    out["prompt_source"] = {"mode": "connected"}
    compiled = compile_window(
        with_external_prompt_source(
            out, {"global": "", "mode": "Fixed", "script": "Identity"}
        ),
        0,
    )["prompt"]
    assert compiled == "Identity"


def test_replace_is_not_offered_for_prompt_only_import():
    with pytest.raises(ValueError, match="only when media is selected"):
        apply_components(
            blank_project(),
            parse_prompt_source("Identity"),
            [],
            {"global": True},
            "replace",
        )


def test_global_import_replaces_legacy_fixed_identity_before_later_edits():
    p = blank_project()
    p["prompts"] = {
        "global": "Old field",
        "mode": "Fixed",
        "script": "Imported identity",
    }
    out, _ = apply_components(
        p,
        parse_prompt_source("Imported identity\nChunk 1:\nAction"),
        [],
        {"global": True, "chunks": True},
    )
    out["prompts"]["global"] = "Edited identity"
    assert compile_window(out, 0)["prompt"] == "Edited identity\nAction"


def test_global_import_preserves_uncovered_timeline_action_without_old_preamble():
    p = blank_project()
    p["prompts"] = {
        "global": "",
        "mode": "Timeline",
        "script": "Old identity\n[0-7s]\nOld A\n[7-14s]\nOld B",
    }
    out, _ = apply_components(
        p,
        parse_prompt_source("New identity\nChunk 1:\nNew A"),
        [],
        {"global": True, "chunks": True},
    )
    assert compile_window(out, 0)["prompt"] == "New identity\nNew A"
    assert compile_window(out, 1)["prompt"] == "New identity\nOld B"


def test_prompt_text_archive_combines_numbered_files_and_global(tmp_path):
    importer, assets = sources(tmp_path)
    path = tmp_path / "prompts.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("global_prompt.txt", "Identity")
        z.writestr("chunk_02.txt", "B")
        z.writestr("chunk_01.txt", "A")
    token = importer.register_source(path, "prompts.zip")["token"]
    result = importer.preview(blank_project(), token=token)
    assert [compile_window(result["project"], i)["prompt"] for i in range(2)] == [
        "Identity\nA",
        "Identity\nB",
    ]
    assert not assets.calls


def test_media_selection_does_not_require_a_malformed_unselected_prompt(tmp_path):
    importer, assets = sources(tmp_path)
    path = tmp_path / "mixed.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("prompts.txt", "Identity\n[7s to broken]\nAction")
        z.writestr("original_audio.m4a", b"audio")
    token = importer.register_source(path, "mixed.zip")["token"]
    result = importer.preview(
        blank_project(), token=token, selected={"soundtrack": True}
    )
    assert (
        result["prompt_error"]
        and result["project"]["prompts"] == blank_project()["prompts"]
    )
    assert assets.calls == ["original_audio.m4a"]
    with pytest.raises(ValueError, match="Line 2:"):
        importer.preview(blank_project(), token=token, selected={"chunks": True})


def test_unbalanced_timed_header_is_an_error():
    with pytest.raises(ValueError, match="unbalanced"):
        parse_prompt_source("[0-7s\nA")


@pytest.mark.parametrize(
    "source",
    [
        "Identity\n[0-7s] A\n[7-14s] B",
        "Identity\n0-7s: A\n7-14s: B",
        "Identity\nChunk 1: A\nChunk 2: B",
        "Identity\n[Chunk 1] A\n[Chunk 2] B",
    ],
)
def test_inline_headers(source):
    document = parse_prompt_source(source)
    assert document["global"] == "Identity"
    assert [e["text"] for e in document["entries"]] == ["A", "B"]


@pytest.mark.parametrize(
    "text",
    [
        "[1girl, red dress, dancing]",
        "2 dancers - both in long dresses:",
        "3-4 people walk to the door",
    ],
)
def test_numeric_prose_is_not_a_header(text):
    assert parse_prompt_source(text)["global"] == text


def test_partial_import_migrates_typed_chunk_prompt_without_hidden_text():
    p = blank_project()
    p["tracks"] = [
        {
            "id": "typed",
            "role": "prompt",
            "chunk_workspace": True,
            "clips": [
                {
                    "id": "old",
                    "start": 0,
                    "end": 168,
                    "text": "Typed A",
                    "override": True,
                    "chunk_prompt": True,
                    "chunk_index": 0,
                }
            ],
        }
    ]
    result, _ = apply_components(
        p, parse_prompt_source("[2-5s] Imported B"), [], {"chunks": True}
    )
    assert not result["tracks"]
    assert [
        (r["start"], r["end"], r["text"]) for r in result["prompts"]["regions"]
    ] == [(0, 48, "Typed A"), (48, 120, "Imported B"), (120, 168, "Typed A")]
    assert [s["text"] for s in compile_window(result, 0)["segments"]] == [
        "Typed A",
        "Imported B",
        "Typed A",
    ]


def test_explicit_movie_and_scene_time_origins():
    p = blank_project()
    p.update(chunk_origin_frame=2400, duration_frames=2736, selection=[2400, 2736])
    doc = parse_prompt_source("[100-107s] A")
    result, mapping = apply_components(p, doc, [], {"chunks": True})
    assert mapping["time_origin"] == "movie"
    assert result["prompts"]["regions"][0]["start"] == 2400
    _, mapping = apply_components(p, doc, [], {"chunks": True}, time_origin="scene")
    assert not mapping["regions"] and mapping["surplus"]


def test_unimported_connected_extended_headers_warn_without_blocking():
    from ComfyUI_H3_Continuum_Join.timeline.prompt_import import (
        connected_import_warning,
    )

    assert "Fixed fallback" in connected_import_warning("[00:00-00:07] A")
    assert not connected_import_warning("ordinary prose")


def test_embedded_soundtrack_replace_disables_old_routes_and_does_not_overlap():
    p = blank_project()
    asset = {
        "id": "a" * 64,
        "kind": "video",
        "video": {"start_time": "0"},
        "audio": {"sample_rate": 44100},
    }
    p["assets"] = [asset]
    p["tracks"] = [
        {
            "id": "old",
            "role": "retained",
            "clips": [
                {
                    "id": "oldclip",
                    "asset_id": asset["id"],
                    "start": 0,
                    "end": 336,
                    "audio_role": "both",
                }
            ],
        }
    ]
    media = [
        {"component": "video", "asset": asset, "start": 0, "end": 168, "source_in": "0"}
    ]
    result, _ = apply_components(
        p, parse_prompt_source(""), media, {"video": True, "soundtrack": True}
    )
    assert result["tracks"][0]["clips"][0]["audio_role"] == "neither"
    assert result["tracks"][1]["clips"][0]["audio_role"] == "both"
    sequential = [
        {**media[0], "component": "soundtrack"},
        {**media[0], "component": "soundtrack", "start": 168, "end": 336},
    ]
    result, _ = apply_components(
        p, parse_prompt_source(""), sequential, {"soundtrack": True}
    )
    assert [(c["start"], c["end"]) for c in result["tracks"][-1]["clips"]] == [
        (0, 168),
        (168, 504),
    ]

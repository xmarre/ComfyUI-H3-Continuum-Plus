from __future__ import annotations

from ComfyUI_H3_Continuum_Join.constants import (
    PROMPT_FORMAT_AUTO,
    PROMPT_FORMAT_TIMELINE,
    PROMPT_MODE_FIXED,
    PROMPT_MODE_LIST,
    PROMPT_MODE_TIMELINE,
)
from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan


def test_timeline_source_digest_uses_semantic_ast_not_raw_header_spelling():
    compact = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="persistent\n[0-5s]\none\n[5-10s]\ntwo",
        chunks=2,
        chunk_seconds=5.0,
    )
    reformatted = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="persistent\n[ 0 seconds — 5 seconds ]\none\n[ 5 sec - 10 sec ]\ntwo",
        chunks=2,
        chunk_seconds=5.0,
    )

    assert compact["source"]["original_text"] != reformatted["source"]["original_text"]
    assert compact["source"]["sections"][0]["header"] != reformatted["source"]["sections"][0]["header"]
    assert compact["source"]["source_digest"] == reformatted["source"]["source_digest"]


def test_source_digest_changes_when_timeline_semantics_change():
    original = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-5s]\none\n[5-10s]\ntwo",
        chunks=2,
        chunk_seconds=5.0,
    )
    body_changed = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-5s]\none\n[5-10s]\ntwo changed",
        chunks=2,
        chunk_seconds=5.0,
    )
    boundary_changed = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-4s]\none\n[4-10s]\ntwo",
        chunks=2,
        chunk_seconds=5.0,
    )

    assert original["source"]["source_digest"] != body_changed["source"]["source_digest"]
    assert original["source"]["source_digest"] != boundary_changed["source"]["source_digest"]


def test_source_digest_ignores_requested_mode_when_resolved_semantics_match():
    script = "[0-5s]\none\n[5-10s]\ntwo"
    automatic = make_prompt_plan(
        mode=PROMPT_FORMAT_AUTO,
        script=script,
        chunks=2,
        chunk_seconds=5.0,
    )
    explicit = make_prompt_plan(
        mode=PROMPT_FORMAT_TIMELINE,
        script=script,
        chunks=2,
        chunk_seconds=5.0,
    )

    assert automatic["source"]["requested_mode"] != explicit["source"]["requested_mode"]
    assert automatic["source"]["resolved_mode"] == explicit["source"]["resolved_mode"]
    assert automatic["source"]["source_digest"] == explicit["source"]["source_digest"]


def test_list_source_digest_uses_normalized_entries_not_serialization_style():
    json_list = make_prompt_plan(
        mode=PROMPT_MODE_LIST,
        script='["one", "two"]',
        chunks=2,
        chunk_seconds=5.0,
    )
    separated = make_prompt_plan(
        mode=PROMPT_MODE_LIST,
        script="one\n---\ntwo",
        chunks=2,
        chunk_seconds=5.0,
    )

    assert json_list["source"]["original_text"] != separated["source"]["original_text"]
    assert json_list["source"]["entries"] == separated["source"]["entries"]
    assert json_list["source"]["source_digest"] == separated["source"]["source_digest"]


def test_fixed_source_digest_uses_normalized_prompt_text():
    plain = make_prompt_plan(
        mode=PROMPT_MODE_FIXED,
        script="fixed text",
        chunks=2,
        chunk_seconds=5.0,
    )
    padded = make_prompt_plan(
        mode=PROMPT_MODE_FIXED,
        script="  fixed text  ",
        chunks=2,
        chunk_seconds=5.0,
    )

    assert plain["source"]["original_text"] != padded["source"]["original_text"]
    assert plain["prompts"] == padded["prompts"]
    assert plain["hashes"] == padded["hashes"]
    assert plain["source"]["source_digest"] == padded["source"]["source_digest"]

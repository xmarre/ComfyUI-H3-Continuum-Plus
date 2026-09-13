from __future__ import annotations

import copy

import pytest

from ComfyUI_H3_Continuum_Join.constants import (
    PROMPT_MODE_LIST,
    PROMPT_MODE_TIMELINE,
)
from ComfyUI_H3_Continuum_Join.v2.prompts import (
    PromptPlanError,
    make_prompt_plan,
    validate_prompt_plan,
)


def test_validation_accepts_semantically_equivalent_raw_timeline_reformatting():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="persistent\n[0-5s]\none\n[5-10s]\ntwo",
        chunks=2,
        chunk_seconds=5.0,
    )
    reformatted = copy.deepcopy(plan)
    reformatted["source"]["original_text"] = (
        "persistent\n[ 0 seconds — 5 seconds ]\none\n[ 5 sec - 10 sec ]\ntwo"
    )

    assert validate_prompt_plan(reformatted) is reformatted


def test_validation_rejects_raw_timeline_semantics_diverging_from_stored_ast():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-5s]\none\n[5-10s]\ntwo",
        chunks=2,
        chunk_seconds=5.0,
    )
    tampered = copy.deepcopy(plan)
    tampered["source"]["original_text"] = "[0-5s]\none\n[5-10s]\nchanged"

    with pytest.raises(PromptPlanError, match="original source semantics"):
        validate_prompt_plan(tampered)


def test_validation_rejects_raw_list_semantics_diverging_from_stored_entries():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_LIST,
        script='["one", "two"]',
        chunks=2,
        chunk_seconds=5.0,
    )
    tampered = copy.deepcopy(plan)
    tampered["source"]["original_text"] = '["one", "changed"]'

    with pytest.raises(PromptPlanError, match="original source semantics"):
        validate_prompt_plan(tampered)


def test_validation_rejects_source_duration_diverging_from_plan_duration():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-5s]\none\n[5-10s]\ntwo",
        chunks=2,
        chunk_seconds=5.0,
    )
    tampered = copy.deepcopy(plan)
    tampered["chunk_seconds"] = 6.0

    with pytest.raises(PromptPlanError, match="source chunk duration"):
        validate_prompt_plan(tampered)

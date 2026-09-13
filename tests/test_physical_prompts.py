from __future__ import annotations

import copy

from ComfyUI_H3_Continuum_Join.constants import (
    PROMPT_MODE_FIXED,
    PROMPT_MODE_TIMELINE,
    PROMPT_PLAN_MAGIC,
)
from ComfyUI_H3_Continuum_Join.v2.physical_prompts import (
    LEGACY_COMPILER_VERSION,
    PHYSICAL_COMPILER_VERSION,
    compile_physical_prompt,
    make_physical_sample_descriptor,
    physical_metadata,
    physical_metadata_matches,
    physical_prompt_compiler_enabled,
)
from ComfyUI_H3_Continuum_Join.v2.prompts import (
    apply_prompt_overrides,
    make_prompt_plan,
    prompt_hash,
    validate_prompt_plan,
)


def _descriptor(*, retained=175, context=39, total=209, exact=True, group="2"):
    return make_physical_sample_descriptor(
        group_id=group,
        logical_indices=(1,),
        retained_before=retained,
        context_frames=context,
        total_frames=total,
        target_duration_frames=336,
        continuation_method="Native Masked",
        initial_state_origin="sequence",
        include_first=False,
        include_last=False,
        presentation_contract={"images": [], "audio": [], "video": []},
        exact_protected=exact,
    )


def test_schema2_preserves_timeline_source_separately_from_logical_view():
    script = "global preamble\n" + "\n".join(
        f"[{2 * i}-{2 * i + 2}s]\n<Picture {i + 1}> section {i + 1}"
        for i in range(7)
    )
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script=script,
        chunks=2,
        chunk_seconds=7,
    )
    assert plan["schema_version"] == 2
    assert plan["prompts"] == [
        "global preamble\n\n<Picture 1> section 1",
        "global preamble\n\n<Picture 5> section 5",
    ]
    assert plan["source"]["kind"] == "timeline"
    assert plan["source"]["preamble"] == "global preamble"
    assert [section["body"] for section in plan["source"]["sections"]] == [
        f"<Picture {index}> section {index}" for index in range(1, 8)
    ]
    assert len(plan["source"]["source_digest"]) == 64


def test_7s_39f_compiler_uses_the_physical_window_and_preserves_all_sections():
    script = "\n".join(
        f"[{2 * i}-{2 * i + 2}s]\n<Picture {i + 1}> section {i + 1}"
        for i in range(7)
    )
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script=script,
        chunks=2,
        chunk_seconds=7,
    )
    compiled = compile_physical_prompt(plan, _descriptor())
    assert compiled.compiler_version == PHYSICAL_COMPILER_VERSION
    assert compiled.overrun is True
    assert compiled.fallback_status == "physical_overrun_hold"
    assert compiled.text == (
        "[0-0.333333s]\n<Picture 3> section 3\n\n"
        "[0.333333-2.333333s]\n<Picture 4> section 4\n\n"
        "[2.333333-4.333333s]\n<Picture 5> section 5\n\n"
        "[4.333333-6.333333s]\n<Picture 6> section 6\n\n"
        "[6.333333-8.708333s]\n<Picture 7> section 7"
    )
    assert [item["local_start"] for item in compiled.contributing_intervals] == [
        "0", "1/3", "7/3", "13/3", "19/3"
    ]
    assert [item["local_end"] for item in compiled.contributing_intervals] == [
        "1/3", "7/3", "13/3", "19/3", "209/24"
    ]
    assert compiled.contributing_intervals[-1]["source_ordinals"] == [6]


def test_timeline_override_remains_timeline_provenance_and_has_highest_priority():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-7s]\none\n[7-14s]\ntwo",
        chunks=2,
        chunk_seconds=7,
    )
    updated = apply_prompt_overrides(plan, [None, "override second"])
    assert updated["source"]["kind"] == "timeline"
    assert updated["source"]["overrides"] == {"2": "override second"}
    assert updated["prompts"] == ["one", "override second"]
    compiled = compile_physical_prompt(updated, _descriptor())
    assert "override second" in compiled.text
    assert "two" not in compiled.text


def test_schema1_migration_is_explicit_and_never_claims_recovered_timing():
    legacy = {
        "magic": PROMPT_PLAN_MAGIC,
        "schema_version": 1,
        "mode": PROMPT_MODE_TIMELINE,
        "chunks": 2,
        "chunk_seconds": 7.0,
        "prompts": ["one", "two"],
        "hashes": [prompt_hash("one"), prompt_hash("two")],
        "notes": [],
    }
    migrated = validate_prompt_plan(legacy)
    assert migrated is not legacy
    assert migrated["schema_version"] == 2
    assert migrated["source"]["kind"] == "legacy_logical"
    assert migrated["source"]["original_text"] is None
    assert any(item["code"] == "H3C-P200" for item in migrated["diagnostics"])
    compiled = compile_physical_prompt(migrated, _descriptor())
    assert compiled.compiler_version == LEGACY_COMPILER_VERSION
    assert compiled.text == "two"


def test_fixed_prompt_remains_opaque_even_when_it_contains_timeline_like_prose():
    text = "The character says '[0-5s]' as literal prose."
    plan = make_prompt_plan(
        mode=PROMPT_MODE_FIXED,
        script=text,
        chunks=2,
        chunk_seconds=7,
    )
    compiled = compile_physical_prompt(plan, _descriptor())
    assert compiled.compiler_version == LEGACY_COMPILER_VERSION
    assert compiled.text == text


def test_external_initial_state_negative_window_reports_unknown_prior_semantics():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-7s]\none\n[7-14s]\ntwo",
        chunks=2,
        chunk_seconds=7,
    )
    descriptor = make_physical_sample_descriptor(
        group_id="external-1",
        logical_indices=(0,),
        retained_before=0,
        context_frames=39,
        total_frames=175,
        target_duration_frames=336,
        continuation_method="Native Masked",
        initial_state_origin="external_state_unknown_history",
        include_first=False,
        include_last=False,
        presentation_contract={},
        exact_protected=True,
    )
    compiled = compile_physical_prompt(plan, descriptor)
    assert compiled.fallback_status == "unknown_prior_state"
    assert any(item["code"] == "H3C-PT202" for item in compiled.diagnostics)


def test_physical_identity_validates_descriptor_and_compiled_conditioning_together():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-7s]\none\n[7-14s]\ntwo",
        chunks=2,
        chunk_seconds=7,
    )
    descriptor = _descriptor()
    metadata = physical_metadata(descriptor, compile_physical_prompt(plan, descriptor))
    assert physical_metadata_matches(metadata, copy.deepcopy(metadata))
    changed = copy.deepcopy(metadata)
    changed["physical_conditioning_hash"] = "0" * 64
    assert not physical_metadata_matches(metadata, changed)


def test_experimental_activation_is_explicit_and_reversible(monkeypatch):
    monkeypatch.delenv("H3_CONTINUUM_PHYSICAL_PROMPTS", raising=False)
    assert physical_prompt_compiler_enabled() is False
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "1")
    assert physical_prompt_compiler_enabled() is True
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "0")
    assert physical_prompt_compiler_enabled() is False

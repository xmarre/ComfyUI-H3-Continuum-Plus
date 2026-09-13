from __future__ import annotations

from fractions import Fraction
from types import SimpleNamespace

import pytest

from ComfyUI_H3_Continuum_Join.constants import (
    PROMPT_MODE_FIXED,
    PROMPT_MODE_LIST,
    PROMPT_MODE_TIMELINE,
    PROMPT_PLAN_MAGIC,
)
from ComfyUI_H3_Continuum_Join.masked_continuation import (
    CONTINUATION_GUIDE,
    CONTINUATION_NATIVE_MASKED,
)
from ComfyUI_H3_Continuum_Join.v2 import sequence
from ComfyUI_H3_Continuum_Join.v2.physical_prompts import (
    PHYSICAL_COMPILER_VERSION,
    compile_physical_prompt,
    make_physical_sample_descriptor,
)
from ComfyUI_H3_Continuum_Join.v2.physical_sequence import compile_active_metadata
from ComfyUI_H3_Continuum_Join.v2.prompts import (
    apply_prompt_overrides,
    build_sampler_prompt_plan,
    make_prompt_plan,
    prompt_hash,
)


def _descriptor(
    *,
    start_frame: int = 0,
    total_frames: int = 121,
    context_frames: int = 0,
    retained_before: int | None = None,
    exact: bool = False,
    guided: bool = False,
    logical_indices=(0,),
    target_duration_frames=240,
):
    retained = start_frame + context_frames if retained_before is None else retained_before
    return make_physical_sample_descriptor(
        group_id="matrix",
        logical_indices=logical_indices,
        retained_before=retained,
        context_frames=context_frames,
        total_frames=total_frames,
        target_duration_frames=target_duration_frames,
        continuation_method=(
            CONTINUATION_NATIVE_MASKED if exact else CONTINUATION_GUIDE
        ),
        initial_state_origin="sequence",
        include_first=False,
        include_last=False,
        presentation_contract={"reference_count": 0},
        exact_protected=exact,
        guided_overlap=guided,
    )


def test_opaque_override_keeps_historical_list_logical_mode():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_FIXED,
        script="base",
        chunks=2,
        chunk_seconds=5.0,
    )
    updated = apply_prompt_overrides(plan, [None, "override"])

    assert updated["mode"] == PROMPT_MODE_LIST
    assert updated["prompts"] == ["base", "override"]
    assert updated["source"]["kind"] == "fixed"
    assert updated["source"]["overrides"] == {"2": "override"}


def test_timeline_override_is_the_deliberate_schema2_mode_exception():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-5s]\none\n[5-10s]\ntwo",
        chunks=2,
        chunk_seconds=5.0,
    )
    updated = apply_prompt_overrides(plan, [None, "override"])

    assert updated["mode"] == PROMPT_MODE_TIMELINE
    assert updated["source"]["kind"] == "timeline"
    assert updated["source"]["overrides"] == {"2": "override"}


def test_schema2_timeline_duration_adaptation_rebuilds_from_source_and_overrides():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-5s]\none\n[5-10s]\ntwo\n[10-15s]\nthree",
        chunks=3,
        chunk_seconds=5.0,
    )
    plan = apply_prompt_overrides(plan, [None, "override two", None])

    rebuilt = build_sampler_prompt_plan(
        prompt_mode=PROMPT_MODE_FIXED,
        prompt_script="unused",
        sequence_prompt=None,
        prompt_plan=plan,
        chunks=3,
        chunk_seconds=6.0,
    )

    assert rebuilt["schema_version"] == 2
    assert rebuilt["source"]["kind"] == "timeline"
    assert rebuilt["source"]["chunk_seconds"] == "6"
    assert rebuilt["source"]["overrides"] == {"2": "override two"}
    assert rebuilt["prompts"][1] == "override two"


def test_schema1_connected_plan_adapts_without_claiming_source_timing():
    legacy = {
        "magic": PROMPT_PLAN_MAGIC,
        "schema_version": 1,
        "mode": PROMPT_MODE_LIST,
        "chunks": 2,
        "chunk_seconds": 5.0,
        "prompts": ["one", "two"],
        "hashes": [prompt_hash("one"), prompt_hash("two")],
        "notes": [],
    }

    rebuilt = build_sampler_prompt_plan(
        prompt_mode=PROMPT_MODE_FIXED,
        prompt_script="unused",
        sequence_prompt=None,
        prompt_plan=legacy,
        chunks=3,
        chunk_seconds=6.0,
    )

    assert rebuilt["schema_version"] == 2
    assert rebuilt["source"]["kind"] == "legacy_logical"
    assert rebuilt["source"]["original_text"] is None
    assert rebuilt["prompts"] == ["one", "two", "two"]
    assert any(item["code"] == "H3C-P200" for item in rebuilt["diagnostics"])


def test_json_list_and_repeat_last_remain_opaque_clip_instructions():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_LIST,
        script='["one", "two"]',
        chunks=4,
        chunk_seconds=5.0,
    )
    assert plan["prompts"] == ["one", "two", "two", "two"]
    assert plan["source"]["kind"] == "list"
    assert plan["source"]["entries"] == ["one", "two"]


def test_explicit_chunk_body_has_priority_over_timed_body_in_physical_compiler():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-5s]\ntimed\n[Chunk 1]\nchunk body",
        chunks=1,
        chunk_seconds=5.0,
    )
    compiled = compile_physical_prompt(
        plan,
        _descriptor(total_frames=121, target_duration_frames=120),
    )
    assert compiled.compiler_version == PHYSICAL_COMPILER_VERSION
    assert compiled.text == "chunk body"


def test_overlapping_timed_sections_choose_earliest_source_ordinal_with_diagnostic():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-5s]\nfirst\n[2-5s]\nsecond",
        chunks=1,
        chunk_seconds=5.0,
    )
    compiled = compile_physical_prompt(
        plan,
        _descriptor(total_frames=121, target_duration_frames=120),
    )
    assert "first" in compiled.text
    assert "second" not in compiled.text
    assert any(item["code"] == "H3C-PT201" for item in compiled.diagnostics)


def test_gap_holds_previous_body_and_leading_gap_uses_earliest_body():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[1-2s]\nfirst\n[3-4s]\nsecond",
        chunks=1,
        chunk_seconds=5.0,
    )
    compiled = compile_physical_prompt(
        plan,
        _descriptor(total_frames=121, target_duration_frames=120),
    )
    assert compiled.fallback_status == "leading_gap_earliest"
    assert compiled.text.count("first") >= 1
    assert "second" in compiled.text
    assert any(item["code"] == "H3C-PT203" for item in compiled.diagnostics)


def test_future_out_of_domain_section_is_preserved_in_ast_but_not_used_for_overrun():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-5s]\ninside\n[6-7s]\nfuture",
        chunks=1,
        chunk_seconds=5.0,
    )
    # 141 frames = 5.875 s, so the physical sample overruns the 5 s request.
    compiled = compile_physical_prompt(
        plan,
        _descriptor(total_frames=141, target_duration_frames=120),
    )
    assert compiled.overrun
    assert "inside" in compiled.text
    assert "future" not in compiled.text
    assert len(plan["source"]["sections"]) == 2


def test_preamble_is_rendered_once_for_multi_interval_physical_window():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="persistent guidance\n[0-2s]\none\n[2-5s]\ntwo",
        chunks=1,
        chunk_seconds=5.0,
    )
    compiled = compile_physical_prompt(
        plan,
        _descriptor(total_frames=121, target_duration_frames=120),
    )
    assert compiled.text.count("persistent guidance") == 1
    assert compiled.text.count("one") == 1
    assert compiled.text.count("two") == 1


@pytest.mark.parametrize("context", [5, 22, 39])
def test_descriptor_marks_native_exact_overlap_without_changing_geometry(context):
    retained = 175
    descriptor = _descriptor(
        start_frame=retained - context,
        retained_before=retained,
        context_frames=context,
        total_frames=context + 121,
        exact=True,
    )
    assert descriptor.global_start_frame == retained - context
    assert descriptor.exact_protected_interval == (retained - context, retained)
    assert descriptor.guided_overlap_interval is None
    assert descriptor.retained_suffix_interval == (retained, retained + 121)


@pytest.mark.parametrize("context", [5, 22, 39])
def test_descriptor_marks_guide_overlap_as_regenerated_not_exact(context):
    retained = 175
    descriptor = _descriptor(
        start_frame=retained - context,
        retained_before=retained,
        context_frames=context,
        total_frames=context + 121,
        guided=True,
    )
    assert descriptor.exact_protected_interval is None
    assert descriptor.guided_overlap_interval == (retained - context, retained)


def test_terminal_timeline_descriptor_has_one_shared_physical_conditioning_identity(monkeypatch):
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "1")
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-5s]\nfirst\n[5-10s]\nsecond",
        chunks=2,
        chunk_seconds=5.0,
    )
    contract = sequence._terminal_pair_contract(initial_pair=True, chunk_seconds=5.0)
    assets = SimpleNamespace(
        first_image=None,
        last_image=None,
        first_frame_hash="none",
        last_frame_hash="none",
    )
    descriptor = sequence._terminal_descriptor(
        pair_start=0,
        chunks=2,
        retained_before=0,
        contract=contract,
        continuation_method=CONTINUATION_NATIVE_MASKED,
        terminal_prompt_policy=sequence.TERMINAL_PROMPT_POLICY_PHYSICAL,
        assets=assets,
        initial_pair=True,
        target_duration_frames=240,
    )
    compiled, metadata = compile_active_metadata(
        prompt_plan=plan,
        descriptor=descriptor,
        legacy_text="legacy pair text must not win",
    )

    assert descriptor.logical_indices == (0, 1)
    assert compiled.compiler_version == PHYSICAL_COMPILER_VERSION
    assert metadata["physical_conditioning_hash"] == compiled.physical_conditioning_hash
    assert len(metadata["physical_conditioning_hash"]) == 64
    # Runtime stores this same physical metadata object on both logical split
    # entries; the terminal split does not create a second conditioning identity.
    first_half = {"physical_prompt": metadata}
    second_half = {"physical_prompt": dict(metadata)}
    assert (
        first_half["physical_prompt"]["physical_conditioning_hash"]
        == second_half["physical_prompt"]["physical_conditioning_hash"]
    )


def test_fractional_authored_boundaries_remain_exact_in_metadata():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-1.25s]\none\n[1.25-5s]\ntwo",
        chunks=1,
        chunk_seconds=5.0,
    )
    compiled = compile_physical_prompt(
        plan,
        _descriptor(total_frames=121, target_duration_frames=120),
    )
    assert compiled.contributing_intervals[0]["local_end"] == "5/4"
    assert Fraction(compiled.contributing_intervals[0]["local_end"]) == Fraction(5, 4)

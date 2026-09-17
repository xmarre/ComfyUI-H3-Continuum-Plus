from __future__ import annotations

from fractions import Fraction

from ComfyUI_H3_Continuum_Join.constants import PROMPT_MODE_TIMELINE
from ComfyUI_H3_Continuum_Join.masked_continuation import CONTINUATION_NATIVE_MASKED
from ComfyUI_H3_Continuum_Join.v2.physical_prompts import (
    PHYSICAL_COMPILER_VERSION,
    compile_physical_prompt,
    make_physical_sample_descriptor,
)
from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan


def _continuation_descriptor():
    return make_physical_sample_descriptor(
        group_id="00418-chunk-2",
        logical_indices=(1,),
        retained_before=175,
        context_frames=39,
        total_frames=209,
        target_duration_frames=336,
        continuation_method=CONTINUATION_NATIVE_MASKED,
        initial_state_origin="sequence",
        include_first=False,
        include_last=False,
        presentation_contract={"reference_count": 7},
        exact_protected=True,
    )


def _production_style_plan():
    return make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script=(
            "persistent documentary guidance\n"
            "[0-7s]\n"
            "0-2s:\nfish\n\n"
            "2-4s:\nfrog\n\n"
            "4-6s:\ngharial\n\n"
            "6-7s:\nlizard first half\n"
            "[7-14s]\n"
            "7-8s:\nlizard continuation\n\n"
            "8-10s:\niguana\n\n"
            "10-12s:\nturtle\n\n"
            "12-14s:\nblue jay"
        ),
        chunks=2,
        chunk_seconds=7.0,
    )


def test_00418_style_inner_ranges_compile_on_physical_window_not_outer_chunk_body():
    plan = _production_style_plan()
    compiled = compile_physical_prompt(plan, _continuation_descriptor())

    assert PHYSICAL_COMPILER_VERSION == "physical_timeline_text_v2"
    assert compiled.compiler_version == PHYSICAL_COMPILER_VERSION
    assert any(item["code"] == "H3C-PT205" for item in compiled.diagnostics)

    # Physical chunk 2 is [136,345) at 24 fps = [17/3,115/8) seconds.
    # The generated suffix begins at global frame 175 = 175/24 s, still inside
    # the authored 7-8 second lizard interval. The compiler must therefore keep
    # lizard before iguana instead of presenting the whole 7-14 body as one unit.
    intervals = list(compiled.contributing_intervals)
    assert [(item["global_start"], item["global_end"]) for item in intervals] == [
        ("17/3", "6"),
        ("6", "7"),
        ("7", "8"),
        ("8", "10"),
        ("10", "12"),
        ("12", "115/8"),
    ]
    assert [(item["local_start"], item["local_end"]) for item in intervals] == [
        ("0", "1/3"),
        ("1/3", "4/3"),
        ("4/3", "7/3"),
        ("7/3", "13/3"),
        ("13/3", "19/3"),
        ("19/3", "209/24"),
    ]
    assert compiled.overrun

    text = compiled.text
    assert "persistent documentary guidance" in text
    assert text.count("persistent documentary guidance") == 1
    assert text.index("gharial") < text.index("lizard first half")
    assert text.index("lizard first half") < text.index("lizard continuation")
    assert text.index("lizard continuation") < text.index("iguana")
    assert text.index("iguana") < text.index("turtle") < text.index("blue jay")
    # Global inner headers are consumed as structured source timing; Qwen sees
    # only the physical-local labels produced by the compiler.
    assert "7-8s:" not in text
    assert "8-10s:" not in text
    assert "[2.333333-4.333333s]" in text

    protected_end_local = Fraction(175 - 136, 24)
    iguana_start_local = Fraction(8, 1) - Fraction(136, 24)
    assert protected_end_local == Fraction(13, 8)
    assert iguana_start_local == Fraction(7, 3)
    assert protected_end_local < iguana_start_local


def test_inner_range_refinement_does_not_change_legacy_logical_prompt_view():
    plan = _production_style_plan()
    assert plan["prompts"][0].startswith("persistent documentary guidance")
    assert "0-2s:\nfish" in plan["prompts"][0]
    assert "6-7s:\nlizard first half" in plan["prompts"][0]
    assert "7-8s:\nlizard continuation" in plan["prompts"][1]
    assert "12-14s:\nblue jay" in plan["prompts"][1]


def test_ambiguous_or_incomplete_inner_ranges_remain_opaque():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="[0-7s]\n0-2s:\nfish\n\nthen continue without a complete partition",
        chunks=1,
        chunk_seconds=7.0,
    )
    descriptor = make_physical_sample_descriptor(
        group_id="opaque",
        logical_indices=(0,),
        retained_before=0,
        context_frames=0,
        total_frames=175,
        target_duration_frames=168,
        continuation_method=CONTINUATION_NATIVE_MASKED,
        initial_state_origin="sequence",
        include_first=False,
        include_last=False,
        presentation_contract={"reference_count": 0},
    )
    compiled = compile_physical_prompt(plan, descriptor)

    assert not any(item["code"] == "H3C-PT205" for item in compiled.diagnostics)
    assert "0-2s:" in compiled.text
    assert "then continue without a complete partition" in compiled.text

from __future__ import annotations

from ComfyUI_H3_Continuum_Join.constants import PROMPT_MODE_TIMELINE
from ComfyUI_H3_Continuum_Join.masked_continuation import (
    CONTINUATION_GUIDE,
    CONTINUATION_NATIVE_MASKED,
)
from ComfyUI_H3_Continuum_Join.v2.physical_prompts import make_physical_sample_descriptor
from ComfyUI_H3_Continuum_Join.v2.physical_runtime import compile_invocation_prompt
from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan


def _plan():
    return make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script=(
            "persistent documentary guidance\n"
            "[0-7s]\n"
            "0-2s:\nfish\n\n"
            "2-4s:\nfrog\n\n"
            "4-6s:\ngharial scene\nNarrator: Some endure. Others adapt.\n\n"
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


def _descriptor(*, exact: bool, guided: bool = False):
    return make_physical_sample_descriptor(
        group_id="00421-chunk-2",
        logical_indices=(1,),
        retained_before=175,
        context_frames=39,
        total_frames=209,
        target_duration_frames=336,
        continuation_method=(CONTINUATION_NATIVE_MASKED if exact else CONTINUATION_GUIDE),
        initial_state_origin="sequence",
        include_first=False,
        include_last=False,
        presentation_contract={"reference_count": 7},
        exact_protected=exact,
        guided_overlap=guided,
    )


def test_exact_native_masked_prefix_is_neutral_context_not_replayed_authored_content():
    compiled = compile_invocation_prompt(
        _plan(),
        _descriptor(exact=True),
        legacy_text="unused",
        candidate=True,
    )

    assert compiled.compiler_version == "physical_timeline_text_v3"
    codes = {item["code"] for item in compiled.diagnostics}
    assert {"H3C-PT205", "H3C-PT206"}.issubset(codes)
    assert "H3C-PT207" not in codes

    # 00421's physical chunk 2 begins at frame 136, while Native Masked owns
    # frames [136,175) exactly. The stale 4-6s gharial/dialogue and 6-7s lizard
    # bodies are context-only; presenting them as fresh instructions caused the
    # generated suffix to replay both the gharial shot and "Some endure...".
    assert "gharial scene" not in compiled.text
    assert "Some endure. Others adapt." not in compiled.text
    assert "lizard first half" not in compiled.text
    assert "Immutable carried continuation context" in compiled.text

    # Keep the decoded-media-validated 00422 behavior. New generation starts at
    # local 39/24 = 1.625s, still inside the authored 7-8s lizard continuation.
    # The protected part is neutral context; present only the remaining suffix of
    # that instruction to fresh generation. 00509 showed that preserving the body
    # from its authored 7.0s onset duplicates the lizard and delays the iguana cut.
    assert "[0-1.625s]" in compiled.text
    assert "[1.625-2.333333s]\nlizard continuation" in compiled.text
    assert "[2.333333-4.333333s]\niguana" in compiled.text
    assert compiled.text.index("lizard continuation") < compiled.text.index("iguana")
    assert compiled.text.index("iguana") < compiled.text.index("turtle") < compiled.text.index("blue jay")

    intervals = list(compiled.contributing_intervals)
    assert [(item["global_start"], item["global_end"]) for item in intervals] == [
        ("17/3", "175/24"),
        ("175/24", "8"),
        ("8", "10"),
        ("10", "12"),
        ("12", "115/8"),
    ]


def test_guided_overlap_is_not_treated_as_exact_protected_context():
    compiled = compile_invocation_prompt(
        _plan(),
        _descriptor(exact=False, guided=True),
        legacy_text="unused",
        candidate=True,
    )

    assert compiled.compiler_version == "physical_timeline_text_v2"
    assert not any(item["code"] == "H3C-PT206" for item in compiled.diagnostics)
    assert "gharial scene" in compiled.text
    assert "Some endure. Others adapt." in compiled.text


def test_timeline_without_protected_prefix_keeps_v2_identity_and_full_authored_text():
    descriptor = make_physical_sample_descriptor(
        group_id="chunk:1",
        logical_indices=(0,),
        retained_before=0,
        context_frames=0,
        total_frames=175,
        target_duration_frames=336,
        continuation_method=CONTINUATION_NATIVE_MASKED,
        initial_state_origin="sequence",
        include_first=False,
        include_last=False,
        presentation_contract={"reference_count": 7},
    )
    compiled = compile_invocation_prompt(
        _plan(),
        descriptor,
        legacy_text="unused",
        candidate=True,
    )

    assert compiled.compiler_version == "physical_timeline_text_v2"
    assert not any(item["code"] == "H3C-PT206" for item in compiled.diagnostics)
    assert "Some endure. Others adapt." in compiled.text

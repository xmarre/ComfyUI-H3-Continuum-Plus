from __future__ import annotations

from ComfyUI_H3_Continuum_Join.constants import PROMPT_MODE_TIMELINE
from ComfyUI_H3_Continuum_Join.masked_continuation import (
    CONTINUATION_GUIDE,
    CONTINUATION_NATIVE_MASKED,
)
from ComfyUI_H3_Continuum_Join.v2.physical_prompts import (
    _EXACT_PREFIX_FRESH_GAP_BODY,
    make_physical_sample_descriptor,
    physical_metadata,
    text_sha256,
)
from ComfyUI_H3_Continuum_Join.v2 import physical_runtime
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

    assert compiled.compiler_version == "physical_timeline_text_v7"
    codes = {item["code"] for item in compiled.diagnostics}
    assert {"H3C-PT205", "H3C-PT206", "H3C-PT208", "H3C-PT217", "H3C-PT219"}.issubset(codes)
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
    assert "[8.333333-8.708333s]" in compiled.text
    assert "Terminal speech-free lead-out" in compiled.text
    assert "Terminal latent-grid padding only" in compiled.text
    assert "Terminal audio completion contract" not in compiled.text

    intervals = list(compiled.contributing_intervals)
    assert [(item["global_start"], item["global_end"]) for item in intervals] == [
        ("17/3", "175/24"),
        ("175/24", "8"),
        ("8", "10"),
        ("10", "12"),
        ("12", "109/8"),
        ("109/8", "14"),
        ("14", "115/8"),
    ]
    assert intervals[-2]["terminal_role"] == "speech_free_leadout"
    assert intervals[-1]["terminal_role"] == "discarded_padding"


def test_guided_overlap_is_not_treated_as_exact_protected_context():
    compiled = compile_invocation_prompt(
        _plan(),
        _descriptor(exact=False, guided=True),
        legacy_text="unused",
        candidate=True,
    )

    assert compiled.compiler_version == "physical_timeline_text_v7"
    codes = {item["code"] for item in compiled.diagnostics}
    assert "H3C-PT206" not in codes
    assert "H3C-PT208" in codes
    assert "H3C-PT217" in codes
    assert "H3C-PT219" in codes

    # The outer [7-14s] header is the chunk-routing signal. Guided overlap may
    # alter physical geometry, but it must not import the [0-7s] chunk body.
    assert "persistent documentary guidance" in compiled.text
    assert "gharial scene" not in compiled.text
    assert "Some endure. Others adapt." not in compiled.text
    assert "lizard first half" not in compiled.text
    assert "lizard continuation" in compiled.text
    assert "iguana" in compiled.text
    assert "turtle" in compiled.text
    assert "blue jay" in compiled.text


def test_initial_chunk_signal_does_not_import_next_chunk_body_from_physical_overrun():
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

    codes = {item["code"] for item in compiled.diagnostics}
    assert "H3C-PT208" in codes
    assert "H3C-PT206" not in codes
    assert "persistent documentary guidance" in compiled.text
    assert "fish" in compiled.text
    assert "frog" in compiled.text
    assert "gharial scene" in compiled.text
    assert "lizard first half" in compiled.text
    assert "lizard continuation" not in compiled.text
    assert "iguana" not in compiled.text
    assert "turtle" not in compiled.text
    assert "blue jay" not in compiled.text

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
    codes = {item["code"] for item in compiled.diagnostics}
    assert "H3C-PT206" not in codes
    assert "H3C-PT208" in codes
    assert "Some endure. Others adapt." in compiled.text
    assert "lizard continuation" not in compiled.text
    assert "iguana" not in compiled.text


def test_terminal_native_grid_padding_is_not_authored_speech_in_00586_geometry():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script=(
            "[0-7s]\nfirst chunk\n"
            "[7-14s]\nsecond chunk\n"
            "[14-21s]\nNarrator finishes the final sentence cleanly before the end."
        ),
        chunks=3,
        chunk_seconds=7.0,
    )
    descriptor = make_physical_sample_descriptor(
        group_id="chunk:3",
        logical_indices=(2,),
        retained_before=328,
        context_frames=39,
        total_frames=226,
        target_duration_frames=504,
        continuation_method=CONTINUATION_NATIVE_MASKED,
        initial_state_origin="sequence",
        include_first=False,
        include_last=False,
        presentation_contract={"reference_count": 6},
        exact_protected=True,
    )

    compiled = compile_invocation_prompt(
        plan,
        descriptor,
        legacy_text="unused",
        candidate=True,
    )

    assert descriptor.global_start_frame == 289
    assert descriptor.global_end_frame == 515
    assert descriptor.retained_suffix_interval == (328, 515)
    assert compiled.compiler_version == "physical_timeline_text_v7"
    codes = {item["code"] for item in compiled.diagnostics}
    assert {"H3C-PT206", "H3C-PT208", "H3C-PT217", "H3C-PT219"}.issubset(codes)
    terminal = next(item for item in compiled.diagnostics if item["code"] == "H3C-PT217")
    audio_guard = next(item for item in compiled.diagnostics if item["code"] == "H3C-PT219")
    assert terminal["global_start"] == "21"
    assert terminal["global_end"] == "515/24"
    assert audio_guard["global_start"] == "493/24"
    assert audio_guard["global_end"] == "21"
    assert audio_guard["local_start"] == "17/2"
    assert audio_guard["local_end"] == "215/24"
    assert audio_guard["guard_duration"] == "11/24"
    assert audio_guard["structural"] is True
    assert audio_guard["authored_body_suppressed"] is True

    fresh_gap = next(item for item in compiled.diagnostics if item["code"] == "H3C-PT220")
    assert fresh_gap["fresh_gap"] is True
    assert fresh_gap["global_start"] == "41/3"
    assert fresh_gap["global_end"] == "14"
    assert fresh_gap["local_start"] == "13/8"
    assert fresh_gap["local_end"] == "47/24"
    assert fresh_gap["fresh_start"] == "41/3"
    assert fresh_gap["fallback_type"] == "exact_prefix_fresh_gap_bridge"
    assert fresh_gap["inherited_origin"] == "exact_prefix_context"
    assert fresh_gap["inherited_body_sha256"] == text_sha256(
        physical_runtime._EXACT_PREFIX_CONTEXT_BODY
    )
    assert fresh_gap["body_sha256"] == text_sha256(_EXACT_PREFIX_FRESH_GAP_BODY)
    assert fresh_gap["next_authored_start"] == "14"
    assert fresh_gap["next_authored_body_sha256"] == text_sha256(
        "Narrator finishes the final sentence cleanly before the end."
    )

    assert "[8.5-8.958333s]\nTerminal speech-free lead-out" in compiled.text
    assert "[8.958333-9.416667s]" in compiled.text
    assert "Terminal latent-grid padding only" in compiled.text

    intervals = list(compiled.contributing_intervals)
    assert intervals[0]["global_start"] == "289/24"
    assert intervals[0]["global_end"] == "41/3"
    assert intervals[0]["roles"] == ["exact_prefix_context"]
    assert intervals[0]["generation_classes"] == ["exact_prefix"]
    assert intervals[0]["fallback_roles"] == []

    assert intervals[1]["global_start"] == "41/3"
    assert intervals[1]["global_end"] == "14"
    assert intervals[1]["roles"] == ["fallback"]
    assert intervals[1]["generation_classes"] == ["fresh_generation"]
    assert intervals[1]["fallback_roles"] == ["exact_prefix_fresh_gap_bridge"]
    assert intervals[1]["inherited_origins"] == ["exact_prefix_context"]
    assert intervals[1]["inherited_body_sha256s"] == [
        text_sha256(physical_runtime._EXACT_PREFIX_CONTEXT_BODY)
    ]
    assert intervals[1]["body_sha256"] == text_sha256(_EXACT_PREFIX_FRESH_GAP_BODY)
    assert intervals[1]["next_authored_starts"] == ["14"]

    metadata = physical_metadata(descriptor, compiled)
    manifest = physical_runtime.physical_validation_manifest(metadata, [])
    assert manifest["intervals"][1]["body_sha256"] == intervals[1]["body_sha256"]
    assert manifest["intervals"][1]["source_ordinals"] == []
    assert manifest["intervals"][1]["fallback"] is True
    assert manifest["fresh_gaps"][0]["global_start"] == "41/3"
    assert manifest["fresh_gaps"][0]["global_end"] == "14"
    assert manifest["fresh_gaps"][0]["inherited_origin"] == "exact_prefix_context"
    assert manifest["fresh_gaps"][0]["fallback_type"] == "exact_prefix_fresh_gap_bridge"
    assert manifest["fresh_gaps"][0]["body_sha256"] == text_sha256(
        _EXACT_PREFIX_FRESH_GAP_BODY
    )
    assert "Terminal audio completion contract" not in compiled.text
    assert manifest["prompt_provenance"]["compiled_text_sha256"] == compiled.text_sha256

    assert intervals[-3]["global_start"] == "14"
    assert intervals[-3]["global_end"] == "493/24"
    assert intervals[-3]["terminal_role"] is None
    assert intervals[-2]["global_start"] == "493/24"
    assert intervals[-2]["global_end"] == "21"
    assert intervals[-2]["terminal_role"] == "speech_free_leadout"
    assert intervals[-1]["global_start"] == "21"
    assert intervals[-1]["global_end"] == "515/24"
    assert intervals[-1]["terminal_role"] == "discarded_padding"

"""Preview renders must not forcibly silence dialogue from unfinished source timelines."""

from ComfyUI_H3_Continuum_Join.constants import PROMPT_MODE_TIMELINE
from ComfyUI_H3_Continuum_Join.v2.physical_prompts import make_physical_sample_descriptor
from ComfyUI_H3_Continuum_Join.v2.physical_runtime import (
    _has_authored_timeline_after_output,
    compile_invocation_prompt,
)
from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan


def _plan(script: str):
    return make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script=script,
        chunks=2,
        chunk_seconds=7.0,
    )


def _last_physical_group():
    return make_physical_sample_descriptor(
        group_id="chunk:2",
        logical_indices=(1,),
        retained_before=175,
        context_frames=39,
        total_frames=209,
        target_duration_frames=336,
        continuation_method="Native Masked",
        initial_state_origin="sequence",
        include_first=False,
        include_last=False,
        presentation_contract={},
        exact_protected=True,
    )


def _compile(monkeypatch, plan):
    import ComfyUI_H3_Continuum_Join.v2.physical_runtime as runtime

    monkeypatch.setattr(runtime, "physical_prompt_compiler_enabled", lambda: True)
    return compile_invocation_prompt(
        plan,
        _last_physical_group(),
        legacy_text="",
        candidate=True,
    )


def test_two_chunk_preview_of_four_chunk_story_does_not_silence_future_dialogue(monkeypatch):
    plan = _plan(
        "[0-7s]\nMary says hello.\n"
        "[7-14s]\nClark (S2): <d>[English] I'm just stuck selling furniture...</d>\n"
        "[14-21s]\nClark (S2): <d>[English] ...because someone won't help me!</d>\n"
        "[21-28s]\nMary watches him silently."
    )
    assert _has_authored_timeline_after_output(plan) is True

    compiled = _compile(monkeypatch, plan)
    assert "selling furniture" in compiled.text
    assert "because someone" not in compiled.text
    assert "Terminal speech-free lead-out" not in compiled.text
    assert "Terminal latent-grid padding only" not in compiled.text
    assert any(item["code"] == "H3C-PT230" for item in compiled.diagnostics)
    assert not any(item["code"] in ("H3C-PT217", "H3C-PT219") for item in compiled.diagnostics)


def test_genuine_two_chunk_story_retains_terminal_speech_guard(monkeypatch):
    plan = _plan(
        "[0-7s]\nMary says hello.\n"
        "[7-14s]\nClark finishes his last sentence and remains silent."
    )
    assert _has_authored_timeline_after_output(plan) is False
    compiled = _compile(monkeypatch, plan)
    assert "Terminal speech-free lead-out" in compiled.text
    assert "Terminal latent-grid padding only" in compiled.text
    assert any(item["code"] == "H3C-PT219" for item in compiled.diagnostics)
    assert not any(item["code"] == "H3C-PT230" for item in compiled.diagnostics)


def test_future_section_crossing_horizon_is_treated_as_continuation():
    plan = _plan(
        "[0-7s]\nFirst.\n"
        "[7-16s]\nConversation carries on after the preview ends."
    )
    assert _has_authored_timeline_after_output(plan) is True

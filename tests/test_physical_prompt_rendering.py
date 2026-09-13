from __future__ import annotations

from ComfyUI_H3_Continuum_Join.constants import PROMPT_MODE_TIMELINE
from ComfyUI_H3_Continuum_Join.v2.physical_prompts import (
    compile_physical_prompt,
    make_physical_sample_descriptor,
)
from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan


def _descriptor():
    return make_physical_sample_descriptor(
        group_id="render",
        logical_indices=(0,),
        retained_before=0,
        context_frames=0,
        total_frames=121,
        target_duration_frames=96,
        continuation_method="Guide / Motion Context",
        initial_state_origin="sequence",
        include_first=False,
        include_last=False,
        presentation_contract={},
    )


def test_sub_microsecond_interval_is_not_rendered_as_zero_width():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script=(
            "[0-0.0000004s]\n"
            "tiny body\n"
            "[0.0000004-4s]\n"
            "main body"
        ),
        chunks=1,
        chunk_seconds=4.0,
    )
    compiled = compile_physical_prompt(plan, _descriptor())

    assert "tiny body" in compiled.text
    assert "main body" in compiled.text
    assert "[0-0s]" not in compiled.text
    assert any(item["code"] == "H3C-PT204" for item in compiled.diagnostics)
    assert compiled.contributing_intervals[0]["local_end"] == "1/2500000"


def test_six_decimal_renderer_uses_exact_half_even_rational_rounding():
    plan = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script=(
            "[0-0.0000015s]\n"
            "first\n"
            "[0.0000015-4s]\n"
            "second"
        ),
        chunks=1,
        chunk_seconds=4.0,
    )
    compiled = compile_physical_prompt(plan, _descriptor())

    # Exact 0.0000015 rounds half-even to 0.000002 at six decimal places.
    assert "[0-0.000002s]" in compiled.text
    assert compiled.contributing_intervals[0]["local_end"] == "3/2000000"

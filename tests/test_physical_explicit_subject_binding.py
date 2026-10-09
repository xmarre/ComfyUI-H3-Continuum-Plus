"""01738: explicit reference-identity replacement must survive physical chunk scoping."""

from ComfyUI_H3_Continuum_Join.constants import PROMPT_MODE_TIMELINE
from ComfyUI_H3_Continuum_Join.v2.physical_prompts import (
    _bind_explicit_subject_aliases,
    _explicit_subject_aliases,
    compile_physical_prompt,
    make_physical_sample_descriptor,
)
from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan
from ComfyUI_H3_Continuum_Join.v2.physical_runtime import (
    _timeline_plan_for_logical_signal,
)


PREAMBLE = """subject_definitions:
<Subject 1> is Maekar from <Picture 1>, preserving blond hair. He replaces Clark.
<Subject 2> is Mary from <Picture 2>.
<Picture 3> defines Clark's clothes, seated body position, couch, and room.
Replace Clark's identity with <Subject 1>.

Mary (S1) speaks first. Clark/Maekar (S2) speaks second."""


def _descriptor(index, *, rendered=336, chunks=2):
    if index == 0:
        return make_physical_sample_descriptor(
            group_id="chunk:1", logical_indices=(0,), retained_before=0,
            context_frames=0, total_frames=175, target_duration_frames=rendered,
            continuation_method="Native Masked", initial_state_origin="sequence",
            include_first=False, include_last=False,
            presentation_contract={"reference_count": 3},
        )
    return make_physical_sample_descriptor(
        group_id="chunk:2", logical_indices=(1,), retained_before=175,
        context_frames=39, total_frames=209, target_duration_frames=rendered,
        continuation_method="Native Masked", initial_state_origin="sequence",
        include_first=False, include_last=False,
        presentation_contract={"reference_count": 3}, exact_protected=True,
    )


def _plan(script):
    return make_prompt_plan(mode=PROMPT_MODE_TIMELINE, script=script,
                            chunks=2, chunk_seconds=7.0)


def test_explicit_substitution_rebinds_visual_mention_but_not_dialogue():
    aliases = _explicit_subject_aliases(PREAMBLE)
    assert aliases == {"clark": "<Subject 1>"}
    text, changes = _bind_explicit_subject_aliases(
        "Cut to Clark/Maekar. Clark (S2), angry: "
        "<d>[English] What do you think, Clark?</d> "
        "Clark's eyes close.", aliases
    )
    assert text == (
        "Cut to <Subject 1>. <Subject 1> (S2), angry: "
        "<d>[English] What do you think, Clark?</d> "
        "<Subject 1>'s eyes close."
    )
    assert changes == 3


def test_first_and_second_chunk_keep_subject_binding_when_physically_scoped():
    plan = _plan(
        PREAMBLE + "\n\n"
        "[0-7s]\nCut to Clark/Maekar on couch. "
        "Clark (S2): <d>[English] I'm being honest.</d>\n"
        "[7-14s]\nClark (S2): <d>[English] I am an architect!</d>\n"
        "[14-21s]\nClark (S2) continues his speech."
    )
    for index in (0, 1):
        descriptor = _descriptor(index)
        scoped, _ = _timeline_plan_for_logical_signal(plan, descriptor)
        compiled = compile_physical_prompt(scoped, descriptor, neutral_terminal_padding=False)
        assert "Replace Clark's identity with <Subject 1>" in compiled.text
        assert "<Subject 1> (S2)" in compiled.text
        assert "Clark (S2):" not in compiled.text
        assert any(item.get("code") == "H3C-PT231" for item in compiled.diagnostics)
        if index == 0:
            assert "Cut to <Subject 1> on couch" in compiled.text
            assert "I'm being honest" in compiled.text
            assert "I am an architect!" not in compiled.text
        else:
            assert "I am an architect!" in compiled.text
            assert "I'm being honest" not in compiled.text


def test_no_implicit_name_to_subject_mapping():
    # An ordinary visual mention is never altered without an explicit
    # '<name> identity with <Subject N>' instruction.
    aliases = _explicit_subject_aliases(
        "<Subject 1> is Maekar from <Picture 1>. <Picture 3> contains Clark."
    )
    assert aliases == {}
    unchanged, count = _bind_explicit_subject_aliases(
        "Clark (S2): <d>[English] Clark is my name.</d>", aliases
    )
    assert unchanged == "Clark (S2): <d>[English] Clark is my name.</d>"
    assert count == 0


def test_conflicting_explicit_replacements_are_not_guessed():
    aliases = _explicit_subject_aliases(
        "Replace Clark's identity with <Subject 1>. "
        "Replace Clark's identity with <Subject 2>."
    )
    assert "clark" not in aliases

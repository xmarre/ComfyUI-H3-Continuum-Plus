from ComfyUI_H3_Continuum_Join.v2.physical_prompts import (
    compile_legacy_nominal,
    legacy_entry_can_reuse,
    make_physical_sample_descriptor,
)


def _descriptor(video):
    return make_physical_sample_descriptor(
        group_id="chunk:1",
        logical_indices=(0,),
        retained_before=0,
        context_frames=0,
        total_frames=121,
        target_duration_frames=240,
        continuation_method="Guide",
        initial_state_origin="sequence",
        include_first=False,
        include_last=False,
        presentation_contract={
            "include_first": False,
            "include_last": False,
            "first_frame_hash": "none",
            "last_frame_hash": "none",
            "reference_count": 0,
            "reference_combined_hash": "none",
            "reference_image_hashes": [],
            "picture_offset": 0,
            "public_to_qwen_picture": {},
            "presentation_order": [],
            **({"video": video} if video is not None else {}),
        },
    )


def test_legacy_initial_adapter_is_rejected_for_physical_timeline_video():
    descriptor = _descriptor(
        {
            "kind": "timeline_video",
            "adapter": "physical_window_v1",
            "processed_sha256": "1" * 64,
        }
    )
    compiled = compile_legacy_nominal({"prompts": ["same"]}, descriptor, text="same")
    entry = {"prompt": "same", "plan": {}}

    assert not legacy_entry_can_reuse(
        entry=entry,
        descriptor=descriptor,
        compiled=compiled,
        source_kind="fixed",
    )


def test_legacy_initial_adapter_remains_available_without_physical_timeline_video():
    descriptor = _descriptor(None)
    compiled = compile_legacy_nominal({"prompts": ["same"]}, descriptor, text="same")
    entry = {"prompt": "same", "plan": {}}

    assert legacy_entry_can_reuse(
        entry=entry,
        descriptor=descriptor,
        compiled=compiled,
        source_kind="fixed",
    )

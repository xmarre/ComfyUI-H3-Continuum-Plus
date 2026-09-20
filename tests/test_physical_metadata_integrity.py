from __future__ import annotations

import copy
import json

import pytest
import torch

from ComfyUI_H3_Continuum_Join.v2.physical_runtime import physical_validation_manifest
from ComfyUI_H3_Continuum_Join.v2.physical_prompts import (
    PhysicalPromptError,
    canonical_sha256,
    compile_legacy_nominal,
    make_physical_sample_descriptor,
    physical_metadata,
    validate_physical_metadata,
)


def _metadata(*, presentation: dict, include_first: bool = False, include_last: bool = False, timeline_video=None):
    descriptor = make_physical_sample_descriptor(
        group_id="2",
        logical_indices=(1,),
        retained_before=175,
        context_frames=39,
        total_frames=209,
        target_duration_frames=336,
        continuation_method="Native Masked",
        initial_state_origin="sequence",
        include_first=include_first,
        include_last=include_last,
        presentation_contract=presentation,
        exact_protected=True,
    )
    compiled = compile_legacy_nominal({"prompts": ["first", "second"]}, descriptor)
    return physical_metadata(descriptor, compiled, timeline_video=timeline_video)


def _resign_descriptor(metadata: dict) -> None:
    descriptor = metadata["descriptor"]
    compiled = metadata["compiled"]
    descriptor_digest = canonical_sha256(descriptor)
    conditioning_hash = canonical_sha256(
        {
            "compiler_version": compiled["compiler_version"],
            "descriptor": descriptor,
            "text": compiled["text"],
            "presentation_contract": descriptor["presentation_contract"],
            "terminal_contract": descriptor.get("terminal_contract"),
        }
    )
    metadata["descriptor_digest"] = descriptor_digest
    compiled["descriptor_digest"] = descriptor_digest
    metadata["physical_conditioning_hash"] = conditioning_hash
    compiled["physical_conditioning_hash"] = conditioning_hash


def test_validation_rejects_resigned_descriptor_presentation_flag_conflict():
    metadata = _metadata(
        presentation={
            "include_first": True,
            "include_last": False,
            "first_frame_hash": "a" * 64,
            "last_frame_hash": "none",
        },
        include_first=True,
    )
    tampered = copy.deepcopy(metadata)
    tampered["descriptor"]["include_first"] = False
    _resign_descriptor(tampered)

    with pytest.raises(PhysicalPromptError, match="presentation flags"):
        validate_physical_metadata(tampered)


def test_validation_rejects_timeline_video_duplicate_conflict():
    video = {
        "kind": "timeline_video",
        "adapter": "physical_window_v1",
        "processed_sha256": "b" * 64,
        "selection_contract": {"selection_sha256": "c" * 64},
    }
    metadata = _metadata(
        presentation={
            "include_first": False,
            "include_last": False,
            "video": video,
        },
        timeline_video=video,
    )
    tampered = copy.deepcopy(metadata)
    tampered["timeline_video"]["processed_sha256"] = "d" * 64

    with pytest.raises(PhysicalPromptError, match="Timeline Video metadata is inconsistent"):
        validate_physical_metadata(tampered)


def test_validation_keeps_minimal_legacy_presentation_contract_compatible():
    metadata = _metadata(presentation={"images": [], "audio": [], "video": []})
    assert validate_physical_metadata(metadata) is metadata



def test_physical_validation_manifest_is_bounded_and_records_full_tensor_shape_dtype():
    metadata = _metadata(
        presentation={
            "include_first": False,
            "include_last": False,
            "reference_count": 7,
        }
    )
    metadata["compiled"]["contributing_intervals"][0]["body"] = "must-not-leak"
    conditioning = [
        [
            torch.zeros((1, 17, 4096), dtype=torch.float16),
            {"packed_layout_offset": [3, 7]},
        ],
        [
            torch.zeros((1, 5, 2048), dtype=torch.bfloat16),
            {},
        ],
    ]

    manifest = physical_validation_manifest(metadata, conditioning)

    assert manifest["schema"] == 1
    assert manifest["interval_count"] == 1
    assert len(manifest["intervals_sha256"]) == 64
    assert manifest["conditioning"]["token_count"] == 17
    assert manifest["conditioning"]["tensors"] == [
        {"shape": [1, 17, 4096], "dtype": "torch.float16"},
        {"shape": [1, 5, 2048], "dtype": "torch.bfloat16"},
    ]
    assert manifest["conditioning"]["packed_layout"] == [
        {"packed_layout_offset": [3, 7]}
    ]
    rendered = json.dumps(manifest, sort_keys=True)
    assert "must-not-leak" not in rendered
    assert "body" not in manifest["intervals"][0]

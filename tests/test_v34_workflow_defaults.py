"""Keep public V3.4 defaults and both serialized ComfyUI example graphs in sync."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ComfyUI_H3_Continuum_Join.v3.assembly import (
    H3ContinuumAssembleSeamExperimental,
    VIDEO_SEAM_AUTO,
    VIDEO_SEAM_TONE,
)
from ComfyUI_H3_Continuum_Join.v3.driving_nodes import (
    H3ContinuumAssembleSeamV34,
    H3ContinuumFinalizeDurationV34,
    V34_CONTINUITY_STRONG,
    V34_TIMELINE_SAFE,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
TEMPLATES = (
    "MiniMax_H3_Continuum_V34.json",
    "MiniMax_H3_Continuum_V34_turbo.json",
)


def test_v34_default_is_auto3_without_changing_legacy_node():
    old_modes, old_config = H3ContinuumAssembleSeamExperimental.INPUT_TYPES()["required"]["video_seam"]
    new_modes, new_config = H3ContinuumAssembleSeamV34.INPUT_TYPES()["required"]["video_seam"]
    assert old_modes[0] == VIDEO_SEAM_AUTO
    assert old_config["default"] == VIDEO_SEAM_AUTO
    assert new_modes[0] == VIDEO_SEAM_TONE
    assert new_config["default"] == VIDEO_SEAM_TONE
    assert VIDEO_SEAM_AUTO in new_modes


def test_v34_safe_timeline_defaults_are_consistent():
    assert H3ContinuumAssembleSeamV34.INPUT_TYPES()["required"]["timeline_mode"][1]["default"] == V34_TIMELINE_SAFE
    assert H3ContinuumFinalizeDurationV34.INPUT_TYPES()["optional"]["timeline_mode"][1]["default"] == V34_TIMELINE_SAFE


@pytest.mark.parametrize("filename", TEMPLATES)
def test_v34_serialized_workflow_widget_order_and_named_values(filename):
    workflow = json.loads((EXAMPLES / "workflows" / filename).read_text(encoding="utf-8"))
    nodes = [node for node in workflow["nodes"] if node["type"] == "H3ContinuumAssembleSeamV34"]
    assert len(nodes) == 1
    node = nodes[0]
    schema = H3ContinuumAssembleSeamV34.INPUT_TYPES()["required"]
    names = [name for name in schema if name not in ("images", "audio", "assembly_plan")]
    assert names == [
        "exact_total_duration", "audio_seam", "video_seam",
        "diagnostics", "image_output_device", "timeline_mode",
    ]
    assert len(node["widgets_values"]) == len(names)
    named = node["widgets_values_named"]
    assert set(named) == set(names)
    assert dict(zip(names, node["widgets_values"])) == named
    assert named["exact_total_duration"] is True
    assert named["audio_seam"] == "Auto"
    assert named["video_seam"] == VIDEO_SEAM_TONE
    assert named["image_output_device"] == "Auto"
    assert named["timeline_mode"] == V34_TIMELINE_SAFE
    assert node["properties"]["Node name for S&R"] == node["type"]

    samplers = [other for other in workflow["nodes"] if other["type"] == "H3ContinuumSamplerV34"]
    assert len(samplers) == 1
    sampler = samplers[0]
    assert V34_CONTINUITY_STRONG in sampler["widgets_values"]
    assert "Native Masked — exact continuation (Recommended)" in sampler["widgets_values"]


def test_v34_readable_settings_reference_matches_runtime_schema():
    preset = json.loads((EXAMPLES / "recommended_settings_v34.json").read_text(encoding="utf-8"))
    assert preset["h3_continuum_assemble_seam_v34"]["video_seam"] == VIDEO_SEAM_TONE
    assert preset["h3_continuum_assemble_seam_v34"]["timeline_mode"] == V34_TIMELINE_SAFE
    assert preset["h3_continuum_finalize_duration_v34"]["timeline_mode"] == V34_TIMELINE_SAFE

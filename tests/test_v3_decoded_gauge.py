from __future__ import annotations

import pytest
import torch
from torch.nn import functional as F

from ComfyUI_H3_Continuum_Join.v3.decoded_gauge import (
    apply_decoded_rigid_gauge_in_place,
    plan_decoded_rigid_gauge,
    select_decoded_rigid_gauge_translation,
    translate_decoded_frames,
)
from ComfyUI_H3_Continuum_Join.v3.trajectory_diagnostics import (
    measure_decoded_boundary_trajectory,
)


def _base_frame(height: int = 96, width: int = 128) -> torch.Tensor:
    generator = torch.Generator().manual_seed(225)
    frame = torch.rand((1, height, width, 3), generator=generator)
    nchw = frame.permute(0, 3, 1, 2)
    smooth = F.avg_pool2d(nchw, kernel_size=5, stride=1, padding=2)
    return smooth.permute(0, 2, 3, 1)


def _shifted_sequence(
    base: torch.Tensor,
    offsets: list[tuple[float, float]],
) -> torch.Tensor:
    return torch.cat(
        [
            translate_decoded_frames(base, dx=dx, dy=dy)
            for dx, dy in offsets
        ],
        dim=0,
    )


def _continuous_scene() -> dict[str, object]:
    return {
        "scene_analysis_available": True,
        "scene_cut": False,
        "scene_analysis_classification": "clean_boundary",
    }


def test_decoded_rigid_gauge_removes_constant_chunk_offset_without_changing_internal_motion():
    base = _base_frame()
    previous = _shifted_sequence(
        base,
        [(-3.0, 0.0), (-2.0, 0.0), (-1.0, 0.0), (0.0, 0.0)],
    )
    # Natural motion would continue +1 px/frame in X.  The entire current
    # chunk instead starts with an additional (-6,+4) decoded gauge offset.
    current = _shifted_sequence(
        base,
        [(-5.0, 4.0), (-4.0, 4.0), (-3.0, 4.0), (-2.0, 4.0), (-1.0, 4.0), (0.0, 4.0)],
    )
    current_original = current.clone()
    trajectory = measure_decoded_boundary_trajectory(
        previous,
        current,
        trim_frames=0,
        boundary_global_frame=175,
        forward_frames=6,
        previous_transitions=3,
    )

    plan = plan_decoded_rigid_gauge(
        trajectory,
        affine=None,
        scene=_continuous_scene(),
    )
    assert plan["eligible"] is True
    assert plan["reason"] == "coherent_decoded_rigid_boundary_impulse"
    assert plan["consensus_excess_dx_px"] == pytest.approx(-6.0, abs=0.35)
    assert plan["consensus_excess_dy_px"] == pytest.approx(4.0, abs=0.35)

    selected = select_decoded_rigid_gauge_translation(
        previous,
        current,
        boundary_global_frame=175,
        original_trajectory=trajectory,
        plan=plan,
    )
    assert selected["accepted"] is True
    assert selected["selected_score"]["mean_improvement_ratio"] >= 0.65
    assert torch.equal(current, current_original)

    output = torch.cat((previous, current), dim=0).clone()
    split = int(previous.shape[0])
    apply_decoded_rigid_gauge_in_place(
        output,
        frame_start=split,
        frame_stop=int(output.shape[0]),
        dx=float(selected["selected_dx_px"]),
        dy=float(selected["selected_dy_px"]),
    )
    corrected = measure_decoded_boundary_trajectory(
        output[:split],
        output[split:],
        trim_frames=0,
        boundary_global_frame=175,
        forward_frames=6,
        previous_transitions=3,
    )
    for roi in ("upper45", "full"):
        fields = corrected[roi]
        assert fields["pairwise_dx_px"][0] == pytest.approx(
            fields["pre_median_dx_px"],
            abs=0.75,
        )
        assert fields["pairwise_dy_px"][0] == pytest.approx(
            fields["pre_median_dy_px"],
            abs=0.75,
        )
        assert fields["pairwise_dx_px"][1:4] == pytest.approx(
            trajectory[roi]["pairwise_dx_px"][1:4],
            abs=0.75,
        )
        assert fields["pairwise_dy_px"][1:4] == pytest.approx(
            trajectory[roi]["pairwise_dy_px"][1:4],
            abs=0.75,
        )


def test_decoded_rigid_gauge_rejects_scene_cut():
    trajectory = {
        "upper45": {
            "pairwise_dx_px": [-6.0, 0.0, 0.0, 0.0],
            "pairwise_dy_px": [4.0, 0.0, 0.0, 0.0],
            "pairwise_response": [12.0, 12.0, 12.0, 12.0],
            "pairwise_clipped": [False, False, False, False],
            "pre_median_dx_px": 0.0,
            "pre_median_dy_px": 0.0,
        },
        "full": {
            "pairwise_dx_px": [-6.2, 0.0, 0.0, 0.0],
            "pairwise_dy_px": [4.1, 0.0, 0.0, 0.0],
            "pairwise_response": [11.0, 11.0, 11.0, 11.0],
            "pairwise_clipped": [False, False, False, False],
            "pre_median_dx_px": 0.0,
            "pre_median_dy_px": 0.0,
        },
    }

    plan = plan_decoded_rigid_gauge(
        trajectory,
        affine=None,
        scene={
            "scene_analysis_available": True,
            "scene_cut": True,
        },
    )
    assert plan["eligible"] is False
    assert plan["reason"] == "scene_cut"


def test_decoded_rigid_gauge_rejects_roi_disagreement():
    trajectory = {
        "upper45": {
            "pairwise_dx_px": [-0.2, 0.0, 0.0, 0.0],
            "pairwise_dy_px": [0.1, 0.0, 0.0, 0.0],
            "pairwise_response": [12.0, 12.0, 12.0, 12.0],
            "pairwise_clipped": [False, False, False, False],
            "pre_median_dx_px": 0.0,
            "pre_median_dy_px": 0.0,
        },
        "full": {
            "pairwise_dx_px": [-6.0, 0.0, 0.0, 0.0],
            "pairwise_dy_px": [4.0, 0.0, 0.0, 0.0],
            "pairwise_response": [12.0, 12.0, 12.0, 12.0],
            "pairwise_clipped": [False, False, False, False],
            "pre_median_dx_px": 0.0,
            "pre_median_dy_px": 0.0,
        },
    }

    plan = plan_decoded_rigid_gauge(
        trajectory,
        affine=None,
        scene=_continuous_scene(),
    )
    assert plan["eligible"] is False
    assert plan["reason"] in {
        "boundary_rigid_impulse_below_floor",
        "boundary_rigid_impulse_roi_magnitude_disagreement",
        "boundary_rigid_impulse_roi_vector_disagreement",
    }


def test_decoded_rigid_gauge_rejects_coherent_strong_scale_event():
    trajectory = {
        roi: {
            "pairwise_dx_px": [-6.0, 0.0, 0.0, 0.0],
            "pairwise_dy_px": [4.0, 0.0, 0.0, 0.0],
            "pairwise_response": [12.0, 12.0, 12.0, 12.0],
            "pairwise_clipped": [False, False, False, False],
            "pre_median_dx_px": 0.0,
            "pre_median_dy_px": 0.0,
        }
        for roi in ("upper45", "full")
    }
    affine = {
        roi: {
            "boundary_scale_x": 1.025,
            "boundary_scale_y": 1.0,
            "pre_median_scale_x": 1.0,
            "pre_median_scale_y": 1.0,
            "boundary_scale_confidence_x": 0.5,
            "boundary_scale_confidence_y": 0.1,
        }
        for roi in ("upper45", "full")
    }

    plan = plan_decoded_rigid_gauge(
        trajectory,
        affine=affine,
        scene=_continuous_scene(),
    )
    assert plan["eligible"] is False
    assert plan["reason"] == "coherent_scale_event_requires_nonrigid_model"


def test_00716_decoded_receipts_arm_the_rigid_chunk_gauge():
    trajectory = {
        "upper45": {
            "pairwise_dx_px": [-6.0537686384, -0.0911279024, 0.0040899540, -0.1148625006],
            "pairwise_dy_px": [4.0979930556, 4.3472672438, 0.1739019289, 0.2878627684],
            "pairwise_response": [15.4349988992, 8.9293509844, 13.4047562986, 13.9199461079],
            "pairwise_clipped": [False, False, False, False],
            "pre_median_dx_px": -0.0430,
            "pre_median_dy_px": 0.0078,
        },
        "full": {
            "pairwise_dx_px": [-6.5779786049, -0.0579128237, -0.0248370880, -0.1422202054],
            "pairwise_dy_px": [4.3891757281, 0.0572290120, 0.0902066583, -0.0079092522],
            "pairwise_response": [11.6390969296, 15.0328453819, 16.7649368087, 13.3637027095],
            "pairwise_clipped": [False, False, False, False],
            "pre_median_dx_px": -0.0262,
            "pre_median_dy_px": -0.0435,
        },
    }
    affine = {
        "upper45": {
            "boundary_scale_x": 0.99143921,
            "boundary_scale_y": 1.01012172,
            "pre_median_scale_x": 0.99293739,
            "pre_median_scale_y": 1.00390017,
            "boundary_scale_confidence_x": 0.161401,
            "boundary_scale_confidence_y": 0.106296,
        },
        "full": {
            "boundary_scale_x": 0.99073521,
            "boundary_scale_y": 0.99358166,
            "pre_median_scale_x": 0.99694657,
            "pre_median_scale_y": 0.99811333,
            "boundary_scale_confidence_x": 0.134133,
            "boundary_scale_confidence_y": 0.200954,
        },
    }

    plan = plan_decoded_rigid_gauge(
        trajectory,
        affine=affine,
        scene=_continuous_scene(),
    )

    assert plan["eligible"] is True
    assert plan["reason"] == "coherent_decoded_rigid_boundary_impulse"
    assert plan["consensus_excess_dx_px"] == pytest.approx(-6.2813, abs=0.02)
    assert plan["consensus_excess_dy_px"] == pytest.approx(4.2614, abs=0.02)
    assert plan["proposed_correction_dx_px"] == pytest.approx(6.2813, abs=0.02)
    assert plan["proposed_correction_dy_px"] == pytest.approx(-4.2614, abs=0.02)

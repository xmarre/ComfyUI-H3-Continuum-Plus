import pytest
import torch

from ComfyUI_H3_Continuum_Join.v3.trajectory_diagnostics import (
    measure_decoded_boundary_trajectory,
)


def _pattern(height=32, width=40):
    torch.manual_seed(1234)
    frame = torch.rand((height, width, 3), dtype=torch.float32)
    return frame


def test_decoded_trajectory_recovers_multiframe_vertical_motion():
    anchor = _pattern()
    previous = torch.stack(
        [
            torch.roll(anchor, shifts=(-2, 0), dims=(0, 1)),
            torch.roll(anchor, shifts=(-1, 0), dims=(0, 1)),
            anchor,
        ]
    )
    current = torch.stack(
        [
            torch.roll(anchor, shifts=(1, 0), dims=(0, 1)),
            torch.roll(anchor, shifts=(2, 0), dims=(0, 1)),
            torch.roll(anchor, shifts=(3, 0), dims=(0, 1)),
            torch.roll(anchor, shifts=(4, 0), dims=(0, 1)),
            torch.roll(anchor, shifts=(5, 0), dims=(0, 1)),
            torch.roll(anchor, shifts=(6, 0), dims=(0, 1)),
        ]
    )

    result = measure_decoded_boundary_trajectory(
        previous,
        current,
        trim_frames=0,
        boundary_global_frame=175,
        forward_frames=6,
        previous_transitions=2,
    )

    assert result["trajectory_version"] == 1
    assert result["boundary_global_frame"] == 175
    for roi_name in ("upper45", "full"):
        fields = result[roi_name]
        assert fields["pairwise_dy_px"] == pytest.approx(
            [1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
            abs=0.08,
        )
        assert fields["pairwise_net_dy_px"] == pytest.approx(6.0, abs=0.2)
        assert fields["anchor_final_dy_px"] == pytest.approx(6.0, abs=0.2)
        assert fields["pre_median_dy_px"] == pytest.approx(1.0, abs=0.08)
        assert all(value > 1.0 for value in fields["pairwise_response"])

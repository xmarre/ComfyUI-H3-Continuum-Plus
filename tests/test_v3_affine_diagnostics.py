from __future__ import annotations

import pytest
import torch
from torch.nn import functional as F

from ComfyUI_H3_Continuum_Join.v3.trajectory_diagnostics import (
    measure_decoded_boundary_affine,
)


def _base_frame(height: int = 96, width: int = 128) -> torch.Tensor:
    generator = torch.Generator().manual_seed(224)
    frame = torch.rand(
        (1, 3, height, width),
        generator=generator,
        dtype=torch.float32,
    )
    return F.avg_pool2d(frame, kernel_size=3, stride=1, padding=1)


def _affine_frame(
    frame: torch.Tensor,
    *,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    translation_x: float = 0.0,
    translation_y: float = 0.0,
) -> torch.Tensor:
    _, _, height, width = frame.shape
    y, x = torch.meshgrid(
        torch.arange(height, dtype=torch.float32),
        torch.arange(width, dtype=torch.float32),
        indexing="ij",
    )
    source_x = (x - float(translation_x)) / float(scale_x)
    source_y = (y - float(translation_y)) / float(scale_y)
    grid = torch.stack(
        (
            2.0 * source_x / float(width - 1) - 1.0,
            2.0 * source_y / float(height - 1) - 1.0,
        ),
        dim=-1,
    ).unsqueeze(0)
    return F.grid_sample(
        frame,
        grid,
        mode="bilinear",
        padding_mode="border",
        align_corners=True,
    )


def _image(frame: torch.Tensor) -> torch.Tensor:
    return frame[0].permute(1, 2, 0)


def test_decoded_affine_recovers_scale_and_translation_without_mutation():
    base = _base_frame()
    expanded = _affine_frame(
        base,
        scale_x=1.01,
        scale_y=1.025,
        translation_x=0.5,
        translation_y=-0.75,
    )
    previous = torch.stack((_image(base), _image(base), _image(base)))
    current = torch.stack(
        (_image(expanded), _image(expanded), _image(expanded))
    )
    previous_original = previous.clone()
    current_original = current.clone()

    result = measure_decoded_boundary_affine(
        previous,
        current,
        trim_frames=0,
        boundary_global_frame=175,
        forward_frames=2,
        previous_transitions=2,
    )

    assert torch.equal(previous, previous_original)
    assert torch.equal(current, current_original)
    assert result["affine_version"] == 1
    for roi_name in ("upper45", "full"):
        fields = result[roi_name]
        assert fields["boundary_scale_x"] == pytest.approx(
            1.01, abs=0.015
        )
        assert fields["boundary_scale_y"] == pytest.approx(
            1.025, abs=0.02
        )
        assert fields["pre_median_scale_x"] == pytest.approx(
            1.0, abs=0.005
        )
        assert fields["pre_median_scale_y"] == pytest.approx(
            1.0, abs=0.005
        )

    full = result["full"]
    assert full["boundary_translation_x_px"] == pytest.approx(
        0.5, abs=0.5
    )
    assert full["boundary_translation_y_px"] == pytest.approx(
        -0.75, abs=0.5
    )
    assert full["boundary_scale_confidence_y"] > 0.25


def test_decoded_affine_does_not_turn_translation_into_scale():
    base = _base_frame()
    shifted = _affine_frame(
        base,
        translation_x=2.0,
        translation_y=-1.5,
    )
    previous = torch.stack((_image(base), _image(base), _image(base)))
    current = torch.stack((_image(shifted), _image(shifted)))

    result = measure_decoded_boundary_affine(
        previous,
        current,
        trim_frames=0,
        boundary_global_frame=328,
        forward_frames=1,
        previous_transitions=2,
    )

    full = result["full"]
    assert full["boundary_scale_x"] == pytest.approx(1.0, abs=0.005)
    assert full["boundary_scale_y"] == pytest.approx(1.0, abs=0.005)
    assert full["boundary_translation_x_px"] == pytest.approx(
        2.0, abs=0.25
    )
    assert full["boundary_translation_y_px"] == pytest.approx(
        -1.5, abs=0.25
    )
    assert full["boundary_scale_confidence_x"] < 0.25
    assert full["boundary_scale_confidence_y"] < 0.25

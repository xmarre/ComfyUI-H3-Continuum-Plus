import pytest
import torch

from ComfyUI_H3_Continuum_Join.v3.trajectory_diagnostics import (
    measure_decoded_audio_boundary,
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



def test_decoded_audio_boundary_reports_level_and_high_frequency_change():
    sample_rate = 32000
    samples = sample_rate // 2
    t = torch.arange(samples, dtype=torch.float32) / sample_rate

    previous = torch.sin(2.0 * torch.pi * 440.0 * t).view(1, 1, -1)
    current = (
        2.0 * torch.sin(2.0 * torch.pi * 440.0 * t)
        + 0.5 * torch.sin(2.0 * torch.pi * 6000.0 * t)
    ).view(1, 1, -1)

    result = measure_decoded_audio_boundary(
        previous,
        current,
        sample_rate=sample_rate,
        window_seconds=0.5,
        high_band_hz=4000.0,
    )

    assert result["audio_boundary_version"] == 1
    assert result["window_samples"] == samples
    assert result["current_over_previous_db"] > 5.5
    assert result["current_high_band_fraction"] > result["previous_high_band_fraction"]
    assert result["spectral_centroid_delta_hz"] > 0.0


def test_decoded_audio_boundary_uses_only_bounded_tail_and_head_windows():
    sample_rate = 1000
    previous = torch.zeros(1, 1, 2000)
    current = torch.zeros(1, 1, 2000)
    previous[..., -500:] = 1.0
    current[..., :500] = 2.0

    result = measure_decoded_audio_boundary(
        previous,
        current,
        sample_rate=sample_rate,
        window_seconds=0.5,
        high_band_hz=100.0,
    )

    assert result["window_samples"] == 500
    assert result["previous_rms"] == pytest.approx(1.0)
    assert result["current_rms"] == pytest.approx(2.0)
    assert result["current_over_previous_db"] == pytest.approx(6.020599913, rel=1e-6)

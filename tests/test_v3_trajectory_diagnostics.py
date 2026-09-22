import pytest
import torch

from ComfyUI_H3_Continuum_Join.v3.trajectory_diagnostics import (
    measure_decoded_audio_boundary,
    measure_decoded_audio_overlap_context,
    measure_decoded_boundary_trajectory,
    interpret_decoded_boundary_shot,
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



def test_decoded_audio_overlap_context_identical_carried_prefix_is_exact():
    sample_rate = 32000
    prefix_latents = 65
    overlap_samples = prefix_latents * (sample_rate // 40)
    carried = torch.randn(
        1,
        2,
        overlap_samples,
        generator=torch.Generator().manual_seed(214),
    )
    previous = torch.cat((torch.randn(1, 2, 4000), carried), dim=-1)
    current = torch.cat((carried.clone(), torch.randn(1, 2, 6000)), dim=-1)

    result = measure_decoded_audio_overlap_context(
        previous,
        current,
        sample_rate=sample_rate,
        prefix_latents=prefix_latents,
    )

    assert result["audio_overlap_context_version"] == 2
    assert result["overlap_samples"] == overlap_samples
    assert result["decoder_context_margin_latents"] == 30
    assert result["interior_latents"] == 5
    assert result["interior_samples"] == 4000
    assert result["interior_seconds"] == pytest.approx(0.125)
    for region in result["regions"].values():
        assert region["current_over_previous_db"] == pytest.approx(0.0, abs=1e-7)
        assert region["correlation"] == pytest.approx(1.0, abs=1e-7)
        assert region["least_squares_gain"] == pytest.approx(1.0, abs=1e-7)
        assert region["gain_aligned_residual_rms_ratio"] == pytest.approx(0.0, abs=1e-7)


def test_decoded_audio_overlap_context_identifies_constant_decode_gain():
    sample_rate = 32000
    prefix_latents = 65
    overlap_samples = prefix_latents * (sample_rate // 40)
    carried = torch.randn(
        1,
        2,
        overlap_samples,
        generator=torch.Generator().manual_seed(215),
    )
    previous = torch.cat((torch.randn(1, 2, 4000), carried), dim=-1)
    current = torch.cat((carried * 2.0, torch.randn(1, 2, 6000)), dim=-1)

    result = measure_decoded_audio_overlap_context(
        previous,
        current,
        sample_rate=sample_rate,
        prefix_latents=prefix_latents,
    )

    for region in result["regions"].values():
        assert region["current_over_previous_db"] == pytest.approx(6.020599913, rel=1e-6)
        assert region["correlation"] == pytest.approx(1.0, abs=1e-6)
        assert region["least_squares_gain"] == pytest.approx(2.0, rel=1e-6)
        assert region["gain_aligned_residual_rms_ratio"] == pytest.approx(0.0, abs=1e-6)


def test_decoded_audio_overlap_context_localizes_edge_context_difference():
    sample_rate = 32000
    prefix_latents = 65
    overlap_samples = prefix_latents * (sample_rate // 40)
    carried = torch.randn(
        1,
        2,
        overlap_samples,
        generator=torch.Generator().manual_seed(216),
    )
    changed = carried.clone()
    edge = sample_rate // 4
    changed[..., :edge] *= 1.8
    changed[..., -edge:] *= 0.6
    previous = torch.cat((torch.randn(1, 2, 4000), carried), dim=-1)
    current = torch.cat((changed, torch.randn(1, 2, 6000)), dim=-1)

    result = measure_decoded_audio_overlap_context(
        previous,
        current,
        sample_rate=sample_rate,
        prefix_latents=prefix_latents,
    )

    assert result["regions"]["head"]["current_over_previous_db"] > 4.5
    assert result["regions"]["tail"]["current_over_previous_db"] < -3.5
    assert result["regions"]["interior"]["current_over_previous_db"] == pytest.approx(0.0, abs=1e-6)
    assert result["regions"]["interior"]["correlation"] == pytest.approx(1.0, abs=1e-6)


def test_decoded_audio_overlap_context_requires_context_safe_interior():
    sample_rate = 32000
    prefix_latents = 60
    overlap_samples = prefix_latents * (sample_rate // 40)
    carried = torch.randn(
        1,
        2,
        overlap_samples,
        generator=torch.Generator().manual_seed(217),
    )
    previous = torch.cat((torch.randn(1, 2, 4000), carried), dim=-1)
    current = torch.cat((carried.clone(), torch.randn(1, 2, 6000)), dim=-1)

    with pytest.raises(ValueError, match="too short to expose a decoder-context-safe interior"):
        measure_decoded_audio_overlap_context(
            previous,
            current,
            sample_rate=sample_rate,
            prefix_latents=prefix_latents,
        )


def test_decoded_audio_overlap_context_reports_core_normalizer_inactive_proof():
    sample_rate = 32000
    prefix_latents = 65
    overlap_samples = prefix_latents * (sample_rate // 40)
    carried = torch.randn(
        1,
        2,
        overlap_samples,
        generator=torch.Generator().manual_seed(218),
    ) * 0.05
    previous = torch.cat((torch.zeros(1, 2, 4000), carried), dim=-1)
    current = torch.cat((carried.clone(), torch.zeros(1, 2, 6000)), dim=-1)

    result = measure_decoded_audio_overlap_context(
        previous,
        current,
        sample_rate=sample_rate,
        prefix_latents=prefix_latents,
    )

    assert result["previous_whole_std"] < 0.1995
    assert result["current_whole_std"] < 0.1995
    assert result["previous_core_normalizer_provably_inactive"] is True
    assert result["current_core_normalizer_provably_inactive"] is True



def test_pt212_shot_interpretation_reuses_existing_scene_analysis_without_gating():
    previous = torch.full((6, 8, 8, 3), 0.4, dtype=torch.float32)
    current = torch.full((5, 8, 8, 3), 0.4, dtype=torch.float32)

    continuous = interpret_decoded_boundary_shot(
        previous,
        current,
        trim_frames=2,
        boundary_index=1,
    )
    assert continuous["pt212_interpretation"] == "continuous_shot_candidate"
    assert continuous["scene_analysis_available"] is True
    assert continuous["scene_cut"] is False
    assert continuous["production_gate"] is False

    current[2:] = 0.95
    changed = interpret_decoded_boundary_shot(
        previous,
        current,
        trim_frames=2,
        boundary_index=1,
    )
    assert changed["pt212_interpretation"] == "scene_change_or_unknown"
    assert changed["scene_analysis_available"] is True
    assert changed["scene_cut"] is True
    assert changed["production_gate"] is False


def test_pt212_shot_interpretation_is_unknown_when_scene_analysis_is_unavailable():
    previous = torch.zeros((2, 8, 8, 3), dtype=torch.float32)
    current = torch.zeros((3, 8, 8, 3), dtype=torch.float32)
    receipt = interpret_decoded_boundary_shot(
        previous,
        current,
        trim_frames=0,
        boundary_index=1,
    )
    assert receipt["pt212_interpretation"] == "scene_change_or_unknown"
    assert receipt["scene_analysis_available"] is False
    assert receipt["scene_analysis_classification"] == "unavailable"
    assert receipt["production_gate"] is False

import pytest
import torch
from ComfyUI_H3_Continuum_Join.v3.video_seam import _transient_flash_metrics
from ComfyUI_H3_Continuum_Join.v3.video_tone import measure_decoded_video_tone


def _pattern():
    row = torch.tensor((0.4, 0.6)).repeat(12)
    return row[None, :, None].expand(16, 24, 3).clone()


def test_mean_preserving_contrast_pulse_is_visible_without_a_luma_flash():
    frame = _pattern()
    previous = frame.repeat(30, 1, 1, 1)
    current = frame.repeat(39 + 30, 1, 1, 1)
    for i, gain in enumerate((1.8, 1.6, 1.4, 1.2)):
        current[39 + i] = 0.5 + gain * (frame - 0.5)
    before = current.clone()
    flash_shift, _, _ = _transient_flash_metrics(previous, current[39:])
    assert flash_shift < 1.0e-6

    report = measure_decoded_video_tone(
        previous, current, trim_frames=39, boundary_global_frame=175
    )
    pre_std = report["pre"]["luma_std"][-1]
    assert report["post"]["luma_mean"][:5] == pytest.approx([0.5] * 5)
    assert [v / pre_std for v in report["post"]["luma_std"][:5]] == pytest.approx(
        [1.8, 1.6, 1.4, 1.2, 1.0]
    )
    assert torch.equal(current, before)
    assert report["production_gate"] is False
    assert report["extra_vae_calls"] == 0


def test_plan_aligned_overlap_separates_interior_from_right_context_tail():
    frame = _pattern()
    previous = frame.repeat(80, 1, 1, 1)
    current = frame.repeat(39 + 30, 1, 1, 1)
    current[34:39] = 0.5 + 1.7 * (frame - 0.5)
    report = measure_decoded_video_tone(
        previous, current, trim_frames=39, boundary_global_frame=175
    )
    overlap = report["plan_aligned_overlap"]
    assert overlap["sampled_frames"] == 22
    assert overlap["current_frame_start"] == 17
    assert overlap["requires_identical_carried_latents"] is True
    assert overlap["native_h3_context_interior"]["sampled_rgb_difference_rms"] == 0
    tail = overlap["right_context_tail"]
    assert tail["sampled_channel_gain"] == pytest.approx([1.7] * 3)
    assert tail["sampled_rgb_mean_difference"] == pytest.approx([0] * 3, abs=1.0e-6)
    assert tail["sampled_rgb_difference_rms"] > 0.06


@pytest.mark.parametrize("trim", [0, 5, 22, 39])
def test_flat_images_and_short_native_overlaps_have_finite_profiles(trim):
    previous = torch.full((45, 3, 4, 3), 0.5)
    current = torch.full((trim + 2, 3, 4, 3), 0.5)
    report = measure_decoded_video_tone(
        previous, current, trim_frames=trim, boundary_global_frame=45
    )
    assert report["post_frame_offsets"] == [0, 1]
    assert report["post"]["luma_std"] == [0, 0]
    overlap = report["plan_aligned_overlap"]
    if trim:
        assert overlap["window"]["sampled_channel_gain"] == [None] * 3
        assert overlap["window"]["sampled_rgb_difference_rms"] == 0
    else:
        assert overlap["status"] == "not_evaluated"


def test_only_bounded_frame_windows_are_examined_and_alpha_is_ignored():
    frame = torch.cat((_pattern(), torch.ones(16, 24, 1)), dim=-1)
    previous = frame.repeat(100, 1, 1, 1)
    current = frame.repeat(39 + 100, 1, 1, 1)
    previous[:10] = float("nan")
    current[39 + 24 :] = float("nan")
    current[:5] = float("nan")
    current[..., 3] = float("nan")
    report = measure_decoded_video_tone(
        previous, current, trim_frames=39, boundary_global_frame=100
    )
    assert len(report["pre_frame_offsets"]) == 8
    assert len(report["post_frame_offsets"]) == 24
    assert report["plan_aligned_overlap"]["window"]["sampled_rgb_difference_rms"] == 0


def test_selected_nonfinite_pixels_are_reported_as_unavailable():
    previous = _pattern().repeat(30, 1, 1, 1)
    current = _pattern().repeat(39 + 30, 1, 1, 1)
    current[39, 0, 0, 0] = float("nan")
    with pytest.raises(ValueError, match="NaN or Inf"):
        measure_decoded_video_tone(
            previous, current, trim_frames=39, boundary_global_frame=30
        )

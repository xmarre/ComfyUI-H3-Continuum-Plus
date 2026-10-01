import logging

import pytest
import torch

from ComfyUI_H3_Continuum_Join.constants import DIAGNOSTICS_FULL
from ComfyUI_H3_Continuum_Join.hardening import enrich_assembly_plan
from ComfyUI_H3_Continuum_Join.v2.seam_guard import apply_audio_seam, correct_audio_seam
from ComfyUI_H3_Continuum_Join.v2.seam_types import AudioSeamMetrics
from ComfyUI_H3_Continuum_Join.v3.assembly import assemble_decoded_chunks
from ComfyUI_H3_Continuum_Join.v3.plan import ASSEMBLY_PLAN_MAGIC


@pytest.mark.parametrize("sample_rate", [1000, 32000])
@pytest.mark.parametrize("dtype", [torch.float32, torch.float64])
def test_matching_overlap_preserves_waveform_and_native_boundary(sample_rate, dtype):
    time = torch.arange(round(sample_rate * 0.8), dtype=dtype) / sample_rate
    signal = 0.01 * torch.cos(2.0 * torch.pi * 2.0 * time)
    cut = round(sample_rate * 0.5)
    current = signal.reshape(1, 1, -1).expand(1, 2, -1).clone()
    previous = current[..., :cut].clone()
    previous_before, current_before = previous.clone(), current.clone()

    patch, metrics, fade, gain, dc = correct_audio_seam(
        previous,
        current,
        sample_rate=sample_rate,
        cut_sample=cut,
    )

    assert patch is not None
    assert fade == round(sample_rate * 0.060)
    assert metrics.offset_samples == 0
    assert gain == 1.0 and dc == 0.0
    assert torch.equal(patch, previous[..., -fade:])
    assert metrics.boundary_jump_after == metrics.boundary_jump_before
    assert torch.equal(previous, previous_before)
    assert torch.equal(current, current_before)


@pytest.mark.parametrize("offset", [-7, 0, 7])
@pytest.mark.parametrize("scale,bias", [(0.85, -0.002), (1.15, 0.002)])
@pytest.mark.parametrize("dtype", [torch.float32, torch.float64])
def test_level_dc_and_alignment_corrections_end_at_native_pcm(
    offset, scale, bias, dtype
):
    time = torch.arange(800, dtype=dtype) / 1000
    signal = 0.03 * torch.sin(2.0 * torch.pi * 3.0 * time)
    current = torch.stack((signal, -0.7 * signal)).reshape(1, 2, -1).repeat(2, 1, 1)
    previous = (current[..., :500] * scale + bias).clone()
    previous_before, current_before = previous.clone(), current.clone()
    metrics = AudioSeamMetrics(
        correlation_before=0.8, correlation_after=0.9, offset_samples=offset
    )

    patch, updated, fade, gain, dc = apply_audio_seam(
        previous,
        current,
        metrics,
        sample_rate=1000,
        cut_sample=500,
    )

    assert patch is not None and patch.shape == (2, 2, fade)
    assert gain != 1.0 and dc != 0.0
    assert torch.equal(patch[..., 0], previous[..., -fade])
    assert torch.equal(patch[..., -1], current[..., 499])
    native_jump = float(
        torch.mean(torch.abs(current[..., 499] - current[..., 500])).item()
    )
    assert updated.boundary_jump_after == native_jump
    # Aligned fades must not create the coherent-signal power hump that required
    # scaling the whole patch (and both endpoints) in the old implementation.
    peak_bound = max(
        previous.abs().max().item(),
        current.abs().max().item(),
        (current * gain + dc).abs().max().item(),
    )
    assert patch.abs().max().item() <= peak_bound + 1e-8
    assert torch.equal(previous, previous_before)
    assert torch.equal(current, current_before)


def test_uncorrelated_audio_keeps_the_native_boundary():
    previous = torch.randn(1, 2, 500, generator=torch.Generator().manual_seed(31))
    current = torch.randn(1, 2, 800, generator=torch.Generator().manual_seed(32))
    metrics = AudioSeamMetrics(correlation_after=0.19)
    patch, updated, fade, gain, dc = apply_audio_seam(
        previous,
        current,
        metrics,
        sample_rate=1000,
        cut_sample=500,
    )
    assert patch is None and updated is metrics
    assert (fade, gain, dc) == (0, 1.0, 0.0)


@pytest.mark.parametrize("frequency,fade_ms", [(2, 60), (40, 40), (90, 20), (200, 10)])
def test_matching_overlap_remains_exact_for_every_transient_fade(frequency, fade_ms):
    sample_rate = 1000
    time = torch.arange(800, dtype=torch.float32) / sample_rate
    current = (0.01 * torch.cos(2.0 * torch.pi * frequency * time)).reshape(1, 1, -1)
    previous = current[..., :500].clone()
    patch, metrics, fade, gain, dc = correct_audio_seam(
        previous, current, sample_rate=sample_rate, cut_sample=500
    )

    assert patch is not None and fade == fade_ms
    assert metrics.offset_samples == 0 and gain == 1.0 and dc == 0.0
    assert torch.equal(patch, previous[..., -fade:])
    assert metrics.boundary_jump_after == metrics.boundary_jump_before


@pytest.mark.parametrize("audio_seam", ["Auto", "Off"])
def test_audio_assembly_preserves_a_continuous_waveform_and_fresh_suffix(
    audio_seam, caplog
):
    plan = enrich_assembly_plan(
        {
            "magic": ASSEMBLY_PLAN_MAGIC,
            "schema_version": 1,
            "fps": 24,
            "width": 8,
            "height": 8,
            "chunk_seconds": 5.0,
            "target_frames": 240,
            "preserve_final_frame": True,
            "chunks": [
                {
                    "sequence_index": 1,
                    "chunk_index": 1,
                    "total_frames": 124,
                    "trim_frames": 0,
                    "net_frames": 124,
                    "context_frames": 0,
                    "expected_video_latent_t": 32,
                    "expected_audio_latent_t": 207,
                },
                {
                    "sequence_index": 2,
                    "chunk_index": 2,
                    "total_frames": 141,
                    "trim_frames": 22,
                    "net_frames": 119,
                    "context_frames": 22,
                    "expected_video_latent_t": 36,
                    "expected_audio_latent_t": 235,
                },
            ],
        }
    )
    rate = 32000
    cut = round(124 / 24 * rate)
    trim = round(22 / 24 * rate)
    time = torch.arange(326000, dtype=torch.float32) / rate
    signal = 0.01 * torch.cos(2.0 * torch.pi * 2.0 * time)
    waveform = signal.reshape(1, 1, -1).expand(1, 2, -1).clone()
    audio = [
        {"waveform": waveform[..., :190000].clone(), "sample_rate": rate},
        {"waveform": waveform[..., cut - trim :].clone(), "sample_rate": rate},
    ]
    images = [torch.full((frames, 8, 8, 3), 0.25) for frames in (124, 141)]

    caplog.set_level(logging.INFO, logger="h3_continuum_join")
    output_images, output_audio, report = assemble_decoded_chunks(
        images=images,
        audio=audio,
        assembly_plan=plan,
        exact_total_duration=False,
        audio_seam=audio_seam,
        diagnostics=DIAGNOSTICS_FULL,
        image_output_device="CPU",
    )

    assert output_images.shape[0] == 243
    assert output_audio["sample_rate"] == rate
    if audio_seam == "Auto":
        assert "audio seam 1->2" in report and "fallback to native boundary" not in report
        assert "policy=convex_native_endpoints_v1 applied=True" in caplog.text
        assert "left_endpoint_exact=True right_endpoint_exact=True" in caplog.text
    else:
        assert "H3C-PT226" not in caplog.text
    result = output_audio["waveform"]
    assert torch.equal(result, waveform[..., : result.shape[-1]])
    assert torch.equal(result[..., cut:], waveform[..., cut : result.shape[-1]])

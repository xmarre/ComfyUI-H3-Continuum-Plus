from __future__ import annotations

import pytest
import torch

from ComfyUI_H3_Continuum_Join.hardening import enrich_assembly_plan
from ComfyUI_H3_Continuum_Join.v3.assembly import (
    H3ContinuumAssembleSeamExperimental,
    finalize_assembled_timeline,
)
from ComfyUI_H3_Continuum_Join.v3.driving_nodes import (
    H3ContinuumAssembleSeamV34,
    H3ContinuumFinalizeDurationV34,
    V34_TIMELINE_EXACT,
    V34_TIMELINE_SAFE,
    V34_TIMELINE_STRICT,
    V34_TIMELINE_NATURAL,
)
from ComfyUI_H3_Continuum_Join.v3.plan import ASSEMBLY_PLAN_MAGIC


def _plan():
    return enrich_assembly_plan(
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
                    "expected_video_latent_t": 37,
                    "expected_audio_latent_t": 207,
                },
                {
                    "sequence_index": 2,
                    "chunk_index": 2,
                    "total_frames": 141,
                    "trim_frames": 22,
                    "net_frames": 119,
                    "context_frames": 22,
                    "expected_video_latent_t": 42,
                    "expected_audio_latent_t": 235,
                },
            ],
        }
    )


def _audio(frames, sample_rate=48_000):
    samples = round(frames / 24 * sample_rate)
    return {
        "waveform": torch.linspace(-1.0, 1.0, samples).reshape(1, 1, -1),
        "sample_rate": sample_rate,
    }


def test_v34_timeline_mode_is_explicit_and_defaults_to_safe():
    schema = H3ContinuumAssembleSeamV34.INPUT_TYPES()["required"]["timeline_mode"]
    assert schema[0] == (V34_TIMELINE_SAFE, V34_TIMELINE_EXACT, V34_TIMELINE_STRICT, V34_TIMELINE_NATURAL)
    assert schema[1]["default"] == V34_TIMELINE_SAFE


def test_finalizer_timeline_mode_is_optional_and_defaults_to_safe():
    schema = H3ContinuumFinalizeDurationV34.INPUT_TYPES()
    assert set(schema["required"]) == {"images", "audio", "assembly_plan"}
    mode = schema["optional"]["timeline_mode"]
    assert mode[0] == (V34_TIMELINE_SAFE, V34_TIMELINE_EXACT, V34_TIMELINE_STRICT, V34_TIMELINE_NATURAL)
    assert mode[1]["default"] == V34_TIMELINE_SAFE
    assert mode[1]["display_name"] == "Timeline Output"


def test_v34_natural_mode_drives_underlying_assembler_exact_flag_false(monkeypatch):
    captured = {}

    def fake_assemble(_self, *args, **kwargs):
        captured["exact"] = kwargs["exact_total_duration"]
        return torch.zeros((243, 8, 8, 3)), _audio(243), "base report"

    monkeypatch.setattr(H3ContinuumAssembleSeamExperimental, "assemble", fake_assemble)
    images, _audio_out, _report = H3ContinuumAssembleSeamV34().assemble(
        images=[],
        audio=[],
        assembly_plan=_plan(),
        exact_total_duration=True,
        timeline_mode=V34_TIMELINE_NATURAL,
    )
    assert captured["exact"] is False
    assert images.shape[0] == 243


def test_v34_legacy_exact_mode_preserves_audible_tail(monkeypatch):
    captured = {}

    def fake_assemble(_self, *args, **kwargs):
        captured["exact"] = kwargs["exact_total_duration"]
        return torch.zeros((243, 8, 8, 3)), _audio(243), "base report"

    monkeypatch.setattr(H3ContinuumAssembleSeamExperimental, "assemble", fake_assemble)
    images, _audio_out, _report = H3ContinuumAssembleSeamV34().assemble(
        images=[],
        audio=[],
        assembly_plan=_plan(),
        exact_total_duration=False,
        timeline_mode=V34_TIMELINE_EXACT,
    )
    assert captured["exact"] is False
    assert images.shape[0] == 243


def test_post_stitch_finalizer_reuses_final_anchor_and_audio_policy():
    images = torch.arange(243, dtype=torch.float32).reshape(243, 1, 1, 1).expand(-1, 8, 8, 3)
    audio = _audio(243)
    output_images, output_audio, report = finalize_assembled_timeline(
        images=images,
        audio=audio,
        assembly_plan=_plan(),
    )
    assert output_images.shape[0] == 240
    assert torch.equal(output_images[-1], images[-1])
    assert output_audio["waveform"].shape[-1] == round(240 / 24 * 48_000)
    assert output_audio["sample_rate"] == 48_000
    assert "final anchor preserved" in report


def test_post_stitch_finalizer_leaves_target_length_driving_audio_unchanged():
    images = torch.zeros((243, 8, 8, 3))
    audio = _audio(240)
    output_images, output_audio, _report = finalize_assembled_timeline(
        images=images,
        audio=audio,
        assembly_plan=_plan(),
    )
    assert output_images.shape[0] == 240
    assert torch.equal(output_audio["waveform"], audio["waveform"])
    assert output_audio["sample_rate"] == audio["sample_rate"]


@pytest.mark.parametrize("timeline_mode", [None, V34_TIMELINE_EXACT, [V34_TIMELINE_EXACT]])
def test_finalizer_node_legacy_mode_preserves_audible_tail(timeline_mode):
    kwargs = {} if timeline_mode is None else {"timeline_mode": timeline_mode}
    images = torch.arange(243, dtype=torch.float32).reshape(243, 1, 1, 1)
    output_images, output_audio, report = H3ContinuumFinalizeDurationV34().finalize(
        images, _audio(243), _plan(), **kwargs
    )
    assert output_images.shape[0] == 243
    assert torch.equal(output_images[-1], images[-1])
    assert output_audio["waveform"].shape[-1] == round(243 / 24 * 48_000)
    assert "Audible-tail preservation" in report


@pytest.mark.parametrize("timeline_mode", [V34_TIMELINE_NATURAL, [V34_TIMELINE_NATURAL]])
def test_finalizer_node_natural_mode_preserves_all_frames_and_pcm(timeline_mode, caplog):
    images = torch.arange(243, dtype=torch.float32).reshape(243, 1, 1, 1)
    audio = _audio(243)
    input_images = images.clone()
    input_waveform = audio["waveform"].clone()
    with caplog.at_level("INFO", logger="h3_continuum_join"):
        output_images, output_audio, report = H3ContinuumFinalizeDurationV34().finalize(
            images, audio, _plan(), timeline_mode=timeline_mode
        )
    assert torch.equal(output_images, input_images)
    assert output_images.data_ptr() == images.data_ptr()
    assert torch.equal(output_audio["waveform"], input_waveform)
    assert output_audio["sample_rate"] == audio["sample_rate"]
    assert torch.equal(images, input_images)
    assert torch.equal(audio["waveform"], input_waveform)
    assert "natural=243, target=240, output=243" in report
    assert "Natural retained timeline" in report
    assert "final anchor preserved" not in report
    assert "target_frames=243 trim_frames=0" in caplog.text
    assert "discarded_samples=0 " in caplog.text
    assert "requested_target_frames=240 exact_total_duration=False" in caplog.text


def test_natural_finalizer_aligns_short_driving_audio_without_changing_source():
    images = torch.zeros((243, 8, 8, 3))
    audio = _audio(240)
    original = audio["waveform"].clone()
    output_images, output_audio, _report = finalize_assembled_timeline(
        images=images, audio=audio, assembly_plan=_plan(), exact_total_duration=False
    )
    assert output_images.shape[0] == 243
    waveform = output_audio["waveform"]
    assert waveform.shape[-1] == round(243 / 24 * audio["sample_rate"])
    assert torch.equal(waveform[..., :original.shape[-1]], original)
    assert torch.all(waveform[..., original.shape[-1]:] == original[..., -1:])
    assert torch.equal(audio["waveform"], original)


@pytest.mark.parametrize("timeline_mode", [V34_TIMELINE_SAFE, V34_TIMELINE_EXACT, V34_TIMELINE_STRICT, V34_TIMELINE_NATURAL])
def test_post_stitch_finalizer_rejects_already_compacted_video(timeline_mode):
    with pytest.raises(ValueError, match="natural retained timeline"):
        H3ContinuumFinalizeDurationV34().finalize(
            torch.zeros((240, 8, 8, 3)),
            _audio(240),
            _plan(),
            timeline_mode=timeline_mode,
        )

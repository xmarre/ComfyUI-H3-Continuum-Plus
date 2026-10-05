import json
import logging

import pytest
import torch
from ComfyUI_H3_Continuum_Join.hardening import enrich_assembly_plan
from ComfyUI_H3_Continuum_Join.v3.assembly import assemble_decoded_chunks
from ComfyUI_H3_Continuum_Join.v3.plan import ASSEMBLY_PLAN_MAGIC


@pytest.fixture
def decoded_inputs():
    specs = ((124, 0, 32, 207), (141, 22, 36, 235))
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
                    "sequence_index": index,
                    "chunk_index": index,
                    "total_frames": total,
                    "trim_frames": trim,
                    "net_frames": total - trim,
                    "context_frames": trim,
                    "expected_video_latent_t": video_t,
                    "expected_audio_latent_t": audio_t,
                }
                for index, (total, trim, video_t, audio_t) in enumerate(specs, 1)
            ],
        }
    )
    return {
        "images": [torch.full((total, 8, 8, 3), 0.25) for total, _, _, _ in specs],
        "audio": [
            {"waveform": torch.zeros((1, 2, 190000)), "sample_rate": 32000}
            for _ in specs
        ],
        "assembly_plan": plan,
        "exact_total_duration": False,
        "audio_seam": "Off",
        "diagnostics": "Off",
    }


def test_assembly_tone_receipt_measures_raw_frames_before_video_patch(
    decoded_inputs, caplog
):
    images = decoded_inputs["images"]
    originals = [frames.clone() for frames in images]
    patch = torch.full((1, 8, 8, 3), 0.75)

    with caplog.at_level(logging.INFO, logger="h3_continuum_join"):
        result_images, _, _ = assemble_decoded_chunks(
            **decoded_inputs, video_patches={1: patch}
        )

    line = next(
        record.getMessage()
        for record in caplog.records
        if "H3C-PT227 decoded-video-tone receipt" in record.getMessage()
    )
    tone = json.loads(line.split(" receipt ", 1)[1])
    assert tone["post"]["luma_mean"][0] == pytest.approx(0.25)
    assert tone["production_images_modified"] is False
    assert torch.equal(result_images[124:125], patch)
    assert torch.equal(result_images[:124], originals[0])
    assert torch.equal(result_images[125:], originals[1][23:141])
    assert all(torch.equal(a, b) for a, b in zip(images, originals))


def test_tone_measurement_failure_does_not_change_assembly(
    decoded_inputs, monkeypatch
):
    from ComfyUI_H3_Continuum_Join.v3 import assembly

    def unavailable(*args, **kwargs):
        raise ValueError("test measurement unavailable")

    monkeypatch.setattr(assembly, "measure_decoded_video_tone", unavailable)
    result_images, _, _ = assemble_decoded_chunks(**decoded_inputs)
    images = decoded_inputs["images"]
    assert torch.equal(result_images, torch.cat((images[0], images[1][22:141])))

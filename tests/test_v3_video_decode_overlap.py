from __future__ import annotations

import logging

import torch

from ComfyUI_H3_Continuum_Join.constants import DIAGNOSTICS_FULL
from ComfyUI_H3_Continuum_Join.state import make_plan
from ComfyUI_H3_Continuum_Join.temporal import context_slots
from ComfyUI_H3_Continuum_Join.v3.assembly import assemble_decoded_chunks
from ComfyUI_H3_Continuum_Join.v3.plan import prepare_physical_decode_entries
from ComfyUI_H3_Continuum_Join.v3.video_decode_overlap import (
    VIDEO_DECODE_OVERLAP_CONTRACT,
    VIDEO_DECODE_REUSE_FRAMES,
    annotate_video_decode_overlaps,
    reuse_exact_video_decode_overlap,
)


def _latent_pair(trim_frames: int = 39):
    prefix_t = context_slots(trim_frames)
    generator = torch.Generator().manual_seed(223)
    left = torch.randn(1, 24, 42, 2, 3, generator=generator)
    future = torch.randn(1, 24, 20, 2, 3, generator=generator)
    right = torch.cat((left[:, :, -prefix_t:].clone(), future), dim=2)
    entries = [{"video": left}, {"video": right}]
    groups = [
        {"trim_frames": 0, "net_frames": 175},
        {"trim_frames": trim_frames, "net_frames": 170},
    ]
    return entries, groups, prefix_t


def test_exact_video_overlap_proof_accepts_native_5_22_39_frame_contexts():
    for trim_frames in (5, 22, 39):
        entries, groups, prefix_t = _latent_pair(trim_frames)
        annotated = annotate_video_decode_overlaps(entries, groups)
        second = annotated[1]
        assert second["video_decode_overlap_contract"] == VIDEO_DECODE_OVERLAP_CONTRACT
        assert second["video_decode_overlap_verified"] is True
        assert second["video_decode_overlap_prefix_latents"] == prefix_t
        assert second["video_decode_overlap_reuse_frames"] == VIDEO_DECODE_REUSE_FRAMES
        assert second["video_decode_overlap_reason"] == "exact_carried_prefix"


def test_video_overlap_proof_fails_closed_for_changed_prefix():
    entries, groups, _prefix_t = _latent_pair()
    entries[1] = {"video": entries[1]["video"].clone()}
    entries[1]["video"][:, :, 0] += 1.0

    annotated = annotate_video_decode_overlaps(entries, groups)

    assert annotated[1]["video_decode_overlap_verified"] is False
    assert annotated[1]["video_decode_overlap_reuse_frames"] == 0
    assert annotated[1]["video_decode_overlap_reason"] == "exact_video_prefix_not_bit_identical"


def test_video_overlap_reuse_replaces_only_previous_five_output_frames():
    entries, groups, _prefix_t = _latent_pair()
    group = annotate_video_decode_overlaps(entries, groups)[1]
    frame_cursor = 20
    image_buffer = torch.arange(
        frame_cursor * 2 * 3 * 3,
        dtype=torch.float32,
    ).reshape(frame_cursor, 2, 3, 3)
    before = image_buffer.clone()
    raw_images = torch.zeros((60, 2, 3, 3), dtype=torch.float32)
    raw_images[34:39] = torch.arange(5, dtype=torch.float32).view(5, 1, 1, 1) + 100.0

    receipt = reuse_exact_video_decode_overlap(
        image_buffer,
        raw_images,
        group=group,
        frame_cursor=frame_cursor,
    )

    assert receipt["applied"] is True
    assert receipt["reuse_frames"] == 5
    assert receipt["extra_h3_nfe"] == 0
    assert receipt["extra_sampler_lifetimes"] == 0
    assert receipt["extra_vae_windows"] == 0
    assert torch.equal(image_buffer[: frame_cursor - 5], before[: frame_cursor - 5])
    assert torch.equal(image_buffer[-5:], raw_images[34:39])


def test_unverified_video_overlap_is_bit_exact_noop():
    image_buffer = torch.randn(
        20,
        2,
        3,
        3,
        generator=torch.Generator().manual_seed(224),
    )
    before = image_buffer.clone()
    raw_images = torch.randn(
        60,
        2,
        3,
        3,
        generator=torch.Generator().manual_seed(225),
    )
    receipt = reuse_exact_video_decode_overlap(
        image_buffer,
        raw_images,
        group={"trim_frames": 39},
        frame_cursor=20,
    )

    assert receipt["applied"] is False
    assert torch.equal(image_buffer, before)


def test_prepared_exact_overlap_is_reused_end_to_end_without_extra_compute(caplog):
    generator = torch.Generator().manual_seed(226)
    prefix_t = context_slots(39)
    left_video = torch.randn(1, 24, 52, 2, 3, generator=generator)
    right_future = torch.randn(1, 24, 45, 2, 3, generator=generator)
    right_video = torch.cat((left_video[:, :, -prefix_t:].clone(), right_future), dim=2)

    left_audio = torch.zeros(1, 32, 2, 292)
    right_audio = torch.zeros(1, 32, 2, 320)
    entries = [
        {
            "plan": make_plan(
                continuation=False,
                clip_index=1,
                total_frames=175,
                trim_frames=0,
                width=48,
                height=32,
                context_frames=0,
                state_capacity_frames=39,
                requested_extend_seconds=6.5,
                debug=False,
            ),
            "video": left_video,
            "audio": left_audio,
        },
        {
            "plan": make_plan(
                continuation=True,
                clip_index=2,
                total_frames=192,
                trim_frames=39,
                width=48,
                height=32,
                context_frames=39,
                state_capacity_frames=39,
                requested_extend_seconds=6.5,
                debug=False,
            ),
            "video": right_video,
            "audio": right_audio,
        },
    ]
    decode_entries, plan = prepare_physical_decode_entries(
        entries,
        chunk_seconds=6.5,
        preserve_final_frame=False,
        terminal_merged=False,
    )
    assert decode_entries == entries
    assert plan["chunks"][1]["video_decode_overlap_verified"] is True
    assert plan["chunks"][1]["video_decode_overlap_reuse_frames"] == 5

    first = torch.zeros((175, 2, 3, 3), dtype=torch.float32)
    second = torch.full((192, 2, 3, 3), 20.0, dtype=torch.float32)
    replacement = (
        torch.arange(5, dtype=torch.float32).reshape(5, 1, 1, 1) + 100.0
    ).expand(5, 2, 3, 3)
    second[34:39] = replacement
    audio = [
        {"waveform": torch.zeros((1, 2, 300000)), "sample_rate": 32000},
        {"waveform": torch.zeros((1, 2, 300000)), "sample_rate": 32000},
    ]

    with caplog.at_level(logging.INFO, logger="h3_continuum_join"):
        result_images, _result_audio, _report = assemble_decoded_chunks(
            images=[first, second],
            audio=audio,
            assembly_plan=plan,
            exact_total_duration=False,
            audio_seam="Off",
            diagnostics=DIAGNOSTICS_FULL,
        )

    assert torch.equal(result_images[:170], first[:170])
    assert torch.equal(result_images[170:175], replacement)
    assert torch.equal(result_images[175:], second[39:])

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "H3C-PT223 exact-video-decode-overlap" in joined
    assert "verified=True applied=True reuse_frames=5" in joined
    assert "extra_h3_nfe=0" in joined
    assert "extra_sampler_lifetimes=0" in joined
    assert "extra_vae_windows=0" in joined

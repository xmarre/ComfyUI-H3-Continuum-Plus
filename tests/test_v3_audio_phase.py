from __future__ import annotations

import torch

from ComfyUI_H3_Continuum_Join.v3.audio_phase import (
    AUDIO_PHASE_CONTRACT,
    annotate_audio_phase_origins,
    phase_align_decoded_audio,
)


def _latent_pair():
    generator = torch.Generator().manual_seed(410)
    left_t = 292
    prefix_t = 65
    right_t = 348
    left = torch.randn(1, 32, 2, left_t, generator=generator)
    future = torch.randn(1, 32, 2, right_t - prefix_t, generator=generator)
    right = torch.cat((left[..., -prefix_t:].clone(), future), dim=-1)
    entries = [{"audio": left}, {"audio": right}]
    groups = [
        {
            "trim_frames": 0,
            "net_frames": 175,
        },
        {
            "trim_frames": 39,
            "net_frames": 170,
        },
    ]
    return entries, groups


def test_exact_carried_prefix_proves_global_40hz_origin():
    entries, groups = _latent_pair()
    annotated = annotate_audio_phase_origins(entries, groups)

    assert annotated[0]["audio_phase_contract"] == AUDIO_PHASE_CONTRACT
    assert annotated[0]["audio_phase_verified"] is True
    assert annotated[0]["audio_phase_origin_latent"] == 0
    assert annotated[1]["audio_phase_verified"] is True
    assert annotated[1]["audio_phase_prefix_latents"] == 65
    assert annotated[1]["audio_phase_origin_latent"] == 227
    assert annotated[1]["audio_phase_reason"] == "exact_carried_prefix"
    assert "audio_phase_origin_latent" not in groups[1]


def test_first_group_with_external_prefix_has_no_global_phase_anchor():
    entries, _groups = _latent_pair()
    groups = [
        {"trim_frames": 39, "net_frames": 68},
        {"trim_frames": 39, "net_frames": 170},
    ]
    annotated = annotate_audio_phase_origins(entries, groups)

    assert annotated[0]["audio_phase_verified"] is False
    assert annotated[0]["audio_phase_origin_latent"] is None
    assert annotated[0]["audio_phase_prefix_latents"] == 65
    assert annotated[0]["audio_phase_reason"] == "first_group_has_unanchored_prefix"
    assert annotated[1]["audio_phase_verified"] is False
    assert annotated[1]["audio_phase_origin_latent"] is None
    assert annotated[1]["audio_phase_reason"] == "previous_origin_unverified"

    waveform = torch.randn(
        1,
        2,
        100000,
        generator=torch.Generator().manual_seed(413),
    )
    audio = {"waveform": waveform.clone(), "sample_rate": 32000}
    aligned, report = phase_align_decoded_audio(
        audio,
        group=annotated[0],
        frame_cursor=0,
    )

    assert report["verified"] is False
    assert report["applied"] is False
    assert torch.equal(aligned["waveform"], waveform)


def test_broken_carry_fails_closed_and_does_not_reanchor_later_groups():
    entries, groups = _latent_pair()
    entries[1] = {"audio": entries[1]["audio"].clone()}
    entries[1]["audio"][..., 0] += 1.0
    third_prefix = entries[1]["audio"][..., -65:].clone()
    third_future = torch.randn(
        1,
        32,
        2,
        20,
        generator=torch.Generator().manual_seed(411),
    )
    entries.append({"audio": torch.cat((third_prefix, third_future), dim=-1)})
    groups.append({"trim_frames": 39, "net_frames": 12})

    annotated = annotate_audio_phase_origins(entries, groups)

    assert annotated[1]["audio_phase_verified"] is False
    assert annotated[1]["audio_phase_origin_latent"] is None
    assert annotated[1]["audio_phase_reason"] == "exact_audio_prefix_not_bit_identical"
    assert annotated[2]["audio_phase_verified"] is False
    assert annotated[2]["audio_phase_origin_latent"] is None
    assert annotated[2]["audio_phase_reason"] == "previous_origin_unverified"


def test_00410_geometry_maps_native_52000_trim_to_physical_51733_sample():
    sample_rate = 32000
    samples_per_latent = sample_rate // 40
    right_t = 348
    waveform = torch.arange(
        right_t * samples_per_latent,
        dtype=torch.float32,
    ).reshape(1, 1, -1)
    audio = {"waveform": waveform.clone(), "sample_rate": sample_rate}
    group = {
        "trim_frames": 39,
        "net_frames": 170,
        "audio_phase_contract": AUDIO_PHASE_CONTRACT,
        "audio_phase_verified": True,
        "audio_phase_origin_latent": 227,
        "audio_phase_prefix_latents": 65,
        "audio_phase_reason": "exact_carried_prefix",
    }

    aligned, report = phase_align_decoded_audio(
        audio,
        group=group,
        frame_cursor=175,
    )

    assert report["native_trim_samples"] == 52000
    assert report["phase_trim_samples"] == 51733
    assert report["requested_phase_delta_samples"] == 267
    assert report["phase_delta_samples"] == 267
    assert report["candidate_shortfall_samples"] == 0
    assert report["applied"] is True
    torch.testing.assert_close(
        aligned["waveform"][..., 52000 : 52000 + 1000],
        waveform[..., 51733 : 51733 + 1000],
        rtol=0.0,
        atol=0.0,
    )
    assert aligned["waveform"].shape[-1] == waveform.shape[-1] + 267
    assert torch.equal(audio["waveform"], waveform)


def test_verified_negative_shift_fails_closed_when_decode_tail_is_too_short():
    sample_rate = 32000
    waveform = torch.randn(
        1,
        2,
        178 * (sample_rate // 40),
        generator=torch.Generator().manual_seed(414),
    )
    audio = {"waveform": waveform.clone(), "sample_rate": sample_rate}
    group = {
        "trim_frames": 39,
        "net_frames": 68,
        "audio_phase_contract": AUDIO_PHASE_CONTRACT,
        "audio_phase_verified": True,
        "audio_phase_origin_latent": 113,
        "audio_phase_prefix_latents": 65,
        "audio_phase_reason": "exact_carried_prefix",
    }

    aligned, report = phase_align_decoded_audio(
        audio,
        group=group,
        frame_cursor=107,
    )

    assert report["phase_trim_samples"] == 52267
    assert report["requested_phase_delta_samples"] == -267
    assert report["phase_delta_samples"] == 0
    assert report["candidate_shortfall_samples"] == 534
    assert report["reason"] == "insufficient_decoded_tail_for_phase_alignment"
    assert report["applied"] is False
    assert torch.equal(aligned["waveform"], waveform)


def test_legacy_plan_without_phase_proof_is_bit_exact_noop():
    waveform = torch.randn(1, 2, 4096, generator=torch.Generator().manual_seed(412))
    audio = {"waveform": waveform.clone(), "sample_rate": 32000}

    aligned, report = phase_align_decoded_audio(
        audio,
        group={"trim_frames": 39, "net_frames": 68},
        frame_cursor=175,
    )

    assert report["verified"] is False
    assert report["applied"] is False
    assert report["phase_delta_samples"] == 0
    assert torch.equal(aligned["waveform"], waveform)

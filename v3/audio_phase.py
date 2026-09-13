"""Physical-timeline audio phase transport for externally decoded H3 chunks.

MiniMax H3 audio latents advance at 40 Hz while Continuum's video timeline is
24 fps. An exact carried latent prefix proves the physical audio origin of a
continuation decode group. That origin can differ from the rounded 24-fps trim
position by a fraction of one audio latent tick. This module records only origins
that are proven by bit-identical carried audio and shifts decoded waveforms so
Continuum's existing frame/sample trim lands on the same global physical time.

No resampling, crossfade, latent mutation, VAE work, or H3 evaluation occurs.
"""

from __future__ import annotations

from typing import Any

import torch

from ..media import validate_audio

FPS = 24
AUDIO_LATENT_FPS = 40
AUDIO_PHASE_CONTRACT = "exact_carried_prefix_v1"


def _exact_audio_prefix_ticks(trim_frames: int) -> int | None:
    numerator = int(trim_frames) * AUDIO_LATENT_FPS
    if numerator < 0 or numerator % FPS:
        return None
    return numerator // FPS


def _phase_unavailable(group: dict[str, Any], *, reason: str, prefix_ticks: int | None = None) -> None:
    group["audio_phase_contract"] = AUDIO_PHASE_CONTRACT
    group["audio_phase_verified"] = False
    group["audio_phase_origin_latent"] = None
    group["audio_phase_prefix_latents"] = None if prefix_ticks is None else int(prefix_ticks)
    group["audio_phase_reason"] = str(reason)


def annotate_audio_phase_origins(
    entries: list[dict[str, Any]],
    groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach physical 40-Hz origins proven by exact carried audio prefixes.

    Origin zero is authoritative for the first physical decode group. Every later
    origin is propagated only when the current group's frame trim lands exactly on
    the 40-Hz grid and the corresponding audio prefix is bit-identical to the tail
    of the previous physical group. Once proof is lost, later origins remain
    unavailable rather than being re-anchored heuristically.
    """

    if len(entries) != len(groups):
        raise ValueError(
            "audio phase annotation requires one physical entry per decode group: "
            f"entries={len(entries)}, groups={len(groups)}"
        )
    if not groups:
        return []

    annotated = [dict(group) for group in groups]
    first_audio = entries[0].get("audio") if isinstance(entries[0], dict) else None
    if not torch.is_tensor(first_audio) or first_audio.ndim != 4 or tuple(first_audio.shape[:3]) != (1, 32, 2):
        raise ValueError("audio phase annotation requires native [1,32,2,T] H3 audio latents")

    annotated[0].update(
        {
            "audio_phase_contract": AUDIO_PHASE_CONTRACT,
            "audio_phase_verified": True,
            "audio_phase_origin_latent": 0,
            "audio_phase_prefix_latents": 0,
            "audio_phase_reason": "timeline_origin",
        }
    )
    previous_origin: int | None = 0
    previous_audio = first_audio

    for index in range(1, len(annotated)):
        group = annotated[index]
        current_audio = entries[index].get("audio") if isinstance(entries[index], dict) else None
        prefix_ticks = _exact_audio_prefix_ticks(int(group.get("trim_frames", -1)))

        if previous_origin is None:
            _phase_unavailable(group, reason="previous_origin_unverified", prefix_ticks=prefix_ticks)
            previous_audio = current_audio
            continue
        if prefix_ticks is None:
            _phase_unavailable(group, reason="trim_not_on_40hz_grid")
            previous_origin = None
            previous_audio = current_audio
            continue
        if prefix_ticks <= 0:
            _phase_unavailable(group, reason="continuation_has_no_exact_audio_prefix", prefix_ticks=prefix_ticks)
            previous_origin = None
            previous_audio = current_audio
            continue
        if not torch.is_tensor(current_audio) or current_audio.ndim != 4 or tuple(current_audio.shape[:3]) != (1, 32, 2):
            _phase_unavailable(group, reason="invalid_current_audio_latent", prefix_ticks=prefix_ticks)
            previous_origin = None
            previous_audio = current_audio
            continue
        if (
            not torch.is_tensor(previous_audio)
            or previous_audio.ndim != 4
            or tuple(previous_audio.shape[:3]) != tuple(current_audio.shape[:3])
            or previous_audio.dtype != current_audio.dtype
            or previous_audio.device != current_audio.device
        ):
            _phase_unavailable(group, reason="adjacent_audio_latent_contract_mismatch", prefix_ticks=prefix_ticks)
            previous_origin = None
            previous_audio = current_audio
            continue
        if prefix_ticks > int(previous_audio.shape[-1]) or prefix_ticks > int(current_audio.shape[-1]):
            _phase_unavailable(group, reason="exact_audio_prefix_exceeds_physical_group", prefix_ticks=prefix_ticks)
            previous_origin = None
            previous_audio = current_audio
            continue
        if not torch.equal(previous_audio[..., -prefix_ticks:], current_audio[..., :prefix_ticks]):
            _phase_unavailable(group, reason="exact_audio_prefix_not_bit_identical", prefix_ticks=prefix_ticks)
            previous_origin = None
            previous_audio = current_audio
            continue

        origin = previous_origin + int(previous_audio.shape[-1]) - prefix_ticks
        group.update(
            {
                "audio_phase_contract": AUDIO_PHASE_CONTRACT,
                "audio_phase_verified": True,
                "audio_phase_origin_latent": int(origin),
                "audio_phase_prefix_latents": int(prefix_ticks),
                "audio_phase_reason": "exact_carried_prefix",
            }
        )
        previous_origin = int(origin)
        previous_audio = current_audio

    return annotated


def _frame_sample(frame: int, sample_rate: int) -> int:
    return int(round(int(frame) / FPS * int(sample_rate)))


def _shift_for_native_trim(waveform: torch.Tensor, delta_samples: int) -> torch.Tensor:
    """Shift decode-only samples while keeping Continuum's native trim index fixed."""

    delta = int(delta_samples)
    if delta == 0:
        return waveform
    if delta > 0:
        if int(waveform.shape[-1]) < 1:
            raise ValueError("cannot phase-align an empty decoded waveform")
        pad = waveform[..., :1].expand(*waveform.shape[:-1], delta).clone()
        return torch.cat((pad, waveform), dim=-1)
    drop = -delta
    if drop >= int(waveform.shape[-1]):
        raise ValueError("audio phase correction would discard the entire decoded waveform")
    return waveform[..., drop:].contiguous()


def phase_align_decoded_audio(
    audio: dict[str, Any],
    *,
    group: dict[str, Any],
    frame_cursor: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply a proven physical-origin correction to one decoded audio group.

    Legacy plans without phase metadata and groups whose latent carry could not be
    proven remain byte-for-byte on the native assembly path.
    """

    waveform, sample_rate = validate_audio(audio)
    waveform = waveform.detach().to("cpu")
    rate = int(sample_rate)
    report: dict[str, Any] = {
        "contract": group.get("audio_phase_contract"),
        "verified": bool(group.get("audio_phase_verified", False)),
        "origin_latent": group.get("audio_phase_origin_latent"),
        "prefix_latents": group.get("audio_phase_prefix_latents"),
        "reason": str(group.get("audio_phase_reason", "legacy_plan_no_phase_metadata")),
        "native_trim_samples": _frame_sample(int(group.get("trim_frames", 0)), rate),
        "phase_trim_samples": None,
        "phase_delta_samples": 0,
        "applied": False,
    }

    if group.get("audio_phase_contract") != AUDIO_PHASE_CONTRACT or not report["verified"]:
        return {"waveform": waveform, "sample_rate": rate}, report

    origin = group.get("audio_phase_origin_latent")
    if type(origin) is not int or origin < 0:
        raise ValueError("verified audio phase metadata requires a non-negative integer latent origin")
    if rate <= 0 or rate % AUDIO_LATENT_FPS:
        raise ValueError(
            f"decoded audio sample rate {rate} is not divisible by the H3 {AUDIO_LATENT_FPS}-Hz audio latent grid"
        )

    samples_per_latent = rate // AUDIO_LATENT_FPS
    desired_global_start = _frame_sample(int(frame_cursor), rate)
    phase_trim = desired_global_start - origin * samples_per_latent
    if phase_trim < 0 or phase_trim > int(waveform.shape[-1]):
        raise ValueError(
            "proven audio phase origin maps outside the decoded waveform: "
            f"origin={origin}, frame_cursor={int(frame_cursor)}, phase_trim={phase_trim}, "
            f"waveform={int(waveform.shape[-1])}"
        )

    native_trim = int(report["native_trim_samples"])
    delta = native_trim - phase_trim
    shifted = _shift_for_native_trim(waveform, delta)
    report.update(
        {
            "phase_trim_samples": int(phase_trim),
            "phase_delta_samples": int(delta),
            "applied": bool(delta),
        }
    )
    return {"waveform": shifted, "sample_rate": rate}, report

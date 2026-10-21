"""Exact source PCM windows for experimental H3 target-audio preservation.

The original waveform remains an independent assembly input. Only this model
input is quantized to H3's 40 Hz target grid, anchored at each physical sample's
24 fps start. No latent interpolation or prior generated audio is used.
"""
from __future__ import annotations

from fractions import Fraction
import torch

from .driving_audio import prepare_driving_audio_source, _canonical_sha256
from .state import extract_av_streams

AUDIO_GENERATION_MODES = ("guided", "source_locked", "generated")
SOURCE_AUDIO_LOCK_CONTRACT = "pcm_window_target_mask_v1"


def prepare_locked_source(audio, audio_vae, *, start_frame, end_frame, required_end_frame):
    """Snapshot a bounded source interval; only native overhang may be padded.

    ``audio`` uses the movie clock, including for a connected Driving Audio
    input. Timeline guide mixes supply explicit silence in uncovered intervals.
    """
    if audio is None or audio_vae is None:
        raise ValueError("Source Audio Locked requires source audio and the H3 Audio VAE")
    waveform = audio.get("waveform")
    rate = audio.get("sample_rate")
    if (not torch.is_tensor(waveform) or waveform.ndim != 3
            or tuple(waveform.shape[:2]) != (1, 2)
            or not waveform.is_floating_point()
            or type(rate) is not int or rate <= 0):
        raise ValueError("Source Audio Locked needs finite stereo AUDIO [1,2,samples] and a positive sample rate")
    if not bool(torch.isfinite(waveform).all()):
        raise ValueError("Source Audio Locked source PCM contains NaN or Inf")
    boundary = lambda f: round(Fraction(int(f) * rate, 24))
    a, b, required = map(boundary, (start_frame, end_frame, required_end_frame))
    if not 0 <= a < required <= b or waveform.shape[-1] < required:
        raise ValueError("Source Audio Locked source audio does not cover the authored window; native overhang alone may use silence")
    pcm = waveform[..., a:min(b, waveform.shape[-1])].detach().clone()
    pcm = torch.nn.functional.pad(pcm, (0, b-a-pcm.shape[-1]))
    source = prepare_driving_audio_source(
        {"waveform": pcm, "sample_rate": rate}, audio_vae,
        target_frames=end_frame-start_frame, fps=24,
    )
    # Snapshot even when no resampling was necessary: callers own their inputs.
    from dataclasses import replace
    contract = {**source.contract, "source_audio_lock": SOURCE_AUDIO_LOCK_CONTRACT,
                "movie_start_frame": int(start_frame), "movie_end_frame": int(end_frame),
                "required_end_frame": int(required_end_frame),
                "padding": "encoded_pcm_silence"}
    return replace(source, contract=contract, combined_hash=_canonical_sha256(contract))


def encode_locked_window(source, audio_vae, *, start_frame, total_frames):
    """Encode a physical window at its PCM phase, never slice rounded ticks.

    start_frame is relative to source PCM's first frame. Audio VAE receives an
    integer number of 40 Hz hops, so its ceil padding cannot add a target tick.
    The final tick differs from video duration by at most half an audio tick;
    the original soundtrack keeps its exact media duration during assembly.
    """
    rate = int(source.resolved_vae_sample_rate)
    if rate % 40:
        raise ValueError("Source Audio Locked requires an Audio VAE sample rate divisible by 40 Hz")
    ticks = round(Fraction(int(total_frames)*40, 24))
    start = round(Fraction(int(start_frame)*rate, 24))
    count = ticks*(rate//40)
    waveform = source.resampled_waveform
    if start < 0 or start+count > waveform.shape[-1]:
        raise ValueError("Source Audio Locked physical window exceeds the prepared source PCM interval")
    # The encoder may be implemented by an extension. Never hand it a view of
    # the authoritative waveform or retain a GPU latent beyond this invocation.
    pcm = waveform[..., start:start+count].clone()
    latent = audio_vae.encode(pcm.movedim(1, -1))
    if (not torch.is_tensor(latent) or tuple(latent.shape) != (1, 32, 2, ticks)
            or not latent.is_floating_point() or not bool(torch.isfinite(latent).all())):
        raise ValueError(f"Source Audio Locked Audio VAE must return finite [1,32,2,{ticks}] target latents")
    return latent


def apply_source_audio_lock(latent, source_latent):
    """Copy into a fresh target and compose with the native video prefix mask."""
    from .masked_continuation import require_native_mask_support
    require_native_mask_support()
    import comfy.nested_tensor

    video, audio = extract_av_streams(latent)
    if (not torch.is_tensor(source_latent) or source_latent.shape != audio.shape
            or not source_latent.is_floating_point()
            or not bool(torch.isfinite(source_latent).all())):
        raise ValueError("Source Audio Locked latent must exactly match the complete target audio shape")
    old = latent.get("noise_mask")
    if old is None:
        video_mask = torch.ones((1, 1, *video.shape[2:]), device=video.device, dtype=torch.float32)
    else:
        # This path composes only with the same native NestedTensor contract
        # produced by apply_native_masked_continuation; other masks are invalid.
        parts = old.unbind()
        if len(parts) != 2 or tuple(parts[0].shape) != (1, 1, *video.shape[2:]):
            raise ValueError("Source Audio Locked received incompatible native video/audio mask topology")
        video_mask = parts[0]
    audio_mask = torch.zeros((1, 1, *audio.shape[2:]), device=audio.device, dtype=torch.float32)
    audio.copy_(source_latent)
    return {**latent, "noise_mask": comfy.nested_tensor.NestedTensor((video_mask, audio_mask))}

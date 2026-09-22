"""Bounded decoded-frame trajectory diagnostics for Continuum boundaries."""

from __future__ import annotations

import math
from statistics import median
from typing import Any

import torch
import torch.nn.functional as F

COMPARE_LONG_SIDE = 512
DEFAULT_FORWARD_FRAMES = 6
DEFAULT_PREVIOUS_TRANSITIONS = 4
# Core's current MiniMax-H3 BigVGAN decoder has a finite, non-causal
# receptive field spanning at most 58 audio-latent ticks for one output
# sample. Thirty ticks on each side is therefore a conservative context
# exclusion margin. A 65-tick Continuum carry leaves a 5-tick (125 ms)
# center whose decoder dependency is entirely inside the carried prefix.
MINIMAX_H3_AUDIO_DECODER_CONTEXT_MARGIN_LATENTS = 30
CORE_AUDIO_NORMALIZED_STD = 0.2
CORE_AUDIO_NORMALIZED_STD_TOLERANCE = 5.0e-4
_EPS = 1.0e-12


def _validate_images(frames: Any, label: str) -> None:
    if not torch.is_tensor(frames) or frames.ndim != 4 or int(frames.shape[-1]) < 3:
        raise ValueError(f"{label} must be IMAGE [T,H,W,C]")
    if min(map(int, frames.shape[:3])) < 1:
        raise ValueError(f"{label} must not be empty")


def _prepare_rgb(frames: torch.Tensor) -> tuple[torch.Tensor, float, float]:
    _validate_images(frames, "trajectory frames")
    rgb = frames[..., :3].detach().to(dtype=torch.float32).permute(0, 3, 1, 2)
    height, width = map(int, rgb.shape[-2:])
    scale = min(1.0, COMPARE_LONG_SIDE / float(max(height, width)))
    target_height = max(8, int(round(height * scale)))
    target_width = max(8, int(round(width * scale)))
    if (target_height, target_width) != (height, width):
        rgb = F.interpolate(
            rgb,
            size=(target_height, target_width),
            mode="bilinear",
            align_corners=False,
        )
    rgb = rgb.to(device="cpu").contiguous()
    if not bool(torch.isfinite(rgb).all().item()):
        raise ValueError("decoded trajectory frames contain NaN or Inf")
    return rgb, target_width / float(width), target_height / float(height)


def _parabolic_peak_offset(left: torch.Tensor, center: torch.Tensor, right: torch.Tensor) -> float:
    denominator = float((left - 2.0 * center + right).item())
    if abs(denominator) <= _EPS:
        return 0.0
    offset = 0.5 * float((left - right).item()) / denominator
    if not math.isfinite(offset):
        return 0.0
    return max(-0.5, min(0.5, offset))


def _phase_correlation_shift(
    left: torch.Tensor,
    right: torch.Tensor,
    *,
    roi_fraction: float,
    max_shift_x: int,
    max_shift_y: int,
) -> dict[str, float | bool]:
    if left.shape != right.shape or left.ndim != 3:
        raise ValueError("decoded trajectory phase correlation requires matching CxHxW frames")
    roi_fraction = float(roi_fraction)
    if not math.isfinite(roi_fraction) or not 0.0 < roi_fraction <= 1.0:
        raise ValueError("decoded trajectory roi_fraction must be in (0, 1]")

    height, width = map(int, left.shape[-2:])
    roi_height = max(4, min(height, int(round(height * roi_fraction))))
    left = left[..., :roi_height, :]
    right = right[..., :roi_height, :]
    left = left - left.mean(dim=(-2, -1), keepdim=True)
    right = right - right.mean(dim=(-2, -1), keepdim=True)

    window_y = torch.hann_window(roi_height, periodic=False, dtype=left.dtype)
    window_x = torch.hann_window(width, periodic=False, dtype=left.dtype)
    window = window_y[:, None] * window_x[None, :]
    left_fft = torch.fft.rfft2(left * window)
    right_fft = torch.fft.rfft2(right * window)
    cross = left_fft.conj() * right_fft
    cross = cross / cross.abs().clamp_min(_EPS)
    cross = cross.mean(dim=0)
    correlation = torch.fft.fftshift(
        torch.fft.irfft2(cross, s=(roi_height, width)).real
    )

    center_y, center_x = roi_height // 2, width // 2
    radius_y = min(int(max_shift_y), max(1, center_y - 1))
    radius_x = min(int(max_shift_x), max(1, center_x - 1))
    region = correlation[
        center_y - radius_y : center_y + radius_y + 1,
        center_x - radius_x : center_x + radius_x + 1,
    ]
    flat_index = int(torch.argmax(region).item())
    region_width = int(region.shape[1])
    peak_y = flat_index // region_width + center_y - radius_y
    peak_x = flat_index % region_width + center_x - radius_x
    sub_y = (
        _parabolic_peak_offset(
            correlation[peak_y - 1, peak_x],
            correlation[peak_y, peak_x],
            correlation[peak_y + 1, peak_x],
        )
        if 0 < peak_y < roi_height - 1
        else 0.0
    )
    sub_x = (
        _parabolic_peak_offset(
            correlation[peak_y, peak_x - 1],
            correlation[peak_y, peak_x],
            correlation[peak_y, peak_x + 1],
        )
        if 0 < peak_x < width - 1
        else 0.0
    )
    dx = float(peak_x - center_x) + sub_x
    dy = float(peak_y - center_y) + sub_y
    peak = float(correlation[peak_y, peak_x].item())
    response = peak / max(float(region.abs().mean().item()), _EPS)
    clipped = (
        peak_y in (center_y - radius_y, center_y + radius_y)
        or peak_x in (center_x - radius_x, center_x + radius_x)
    )
    return {"dx": dx, "dy": dy, "response": response, "clipped": bool(clipped)}


def _trajectory_for_roi(
    previous: torch.Tensor,
    current: torch.Tensor,
    *,
    roi_fraction: float,
    scale_x: float,
    scale_y: float,
    forward_frames: int,
    previous_transitions: int,
) -> dict[str, Any]:
    max_pair_shift = max(4, int(round(COMPARE_LONG_SIDE * 0.02)))
    pairwise_dx: list[float] = []
    pairwise_dy: list[float] = []
    pairwise_response: list[float] = []
    pairwise_clipped: list[bool] = []

    sequence = torch.cat((previous[-1:], current[:forward_frames]), dim=0)
    for index in range(int(sequence.shape[0]) - 1):
        shift = _phase_correlation_shift(
            sequence[index],
            sequence[index + 1],
            roi_fraction=roi_fraction,
            max_shift_x=max_pair_shift,
            max_shift_y=max_pair_shift,
        )
        pairwise_dx.append(float(shift["dx"]) / scale_x)
        pairwise_dy.append(float(shift["dy"]) / scale_y)
        pairwise_response.append(float(shift["response"]))
        pairwise_clipped.append(bool(shift["clipped"]))

    anchor_dx: list[float] = []
    anchor_dy: list[float] = []
    anchor_response: list[float] = []
    anchor_clipped: list[bool] = []
    anchor_shift = max_pair_shift * max(1, forward_frames)
    for index in range(min(forward_frames, int(current.shape[0]))):
        shift = _phase_correlation_shift(
            previous[-1],
            current[index],
            roi_fraction=roi_fraction,
            max_shift_x=anchor_shift,
            max_shift_y=anchor_shift,
        )
        anchor_dx.append(float(shift["dx"]) / scale_x)
        anchor_dy.append(float(shift["dy"]) / scale_y)
        anchor_response.append(float(shift["response"]))
        anchor_clipped.append(bool(shift["clipped"]))

    pre_dx: list[float] = []
    pre_dy: list[float] = []
    pre_response: list[float] = []
    start = max(1, int(previous.shape[0]) - int(previous_transitions))
    for index in range(start, int(previous.shape[0])):
        shift = _phase_correlation_shift(
            previous[index - 1],
            previous[index],
            roi_fraction=roi_fraction,
            max_shift_x=max_pair_shift,
            max_shift_y=max_pair_shift,
        )
        pre_dx.append(float(shift["dx"]) / scale_x)
        pre_dy.append(float(shift["dy"]) / scale_y)
        pre_response.append(float(shift["response"]))

    return {
        "pairwise_dx_px": pairwise_dx,
        "pairwise_dy_px": pairwise_dy,
        "pairwise_response": pairwise_response,
        "pairwise_clipped": pairwise_clipped,
        "pairwise_net_dx_px": float(sum(pairwise_dx)),
        "pairwise_net_dy_px": float(sum(pairwise_dy)),
        "anchor_dx_px": anchor_dx,
        "anchor_dy_px": anchor_dy,
        "anchor_response": anchor_response,
        "anchor_clipped": anchor_clipped,
        "anchor_final_dx_px": float(anchor_dx[-1]),
        "anchor_final_dy_px": float(anchor_dy[-1]),
        "pre_pairwise_dx_px": pre_dx,
        "pre_pairwise_dy_px": pre_dy,
        "pre_pairwise_response": pre_response,
        "pre_median_dx_px": float(median(pre_dx)) if pre_dx else 0.0,
        "pre_median_dy_px": float(median(pre_dy)) if pre_dy else 0.0,
        "post_first3_median_dx_px": float(median(pairwise_dx[:3])),
        "post_first3_median_dy_px": float(median(pairwise_dy[:3])),
    }


def interpret_decoded_boundary_shot(
    previous_images: torch.Tensor,
    current_raw_images: torch.Tensor,
    *,
    trim_frames: int,
    boundary_index: int,
) -> dict[str, Any]:
    """Interpret PT212 against the existing read-only Video Seam scene analysis.

    This is evidence annotation only. It introduces no trajectory threshold and
    never changes assembly behavior or suppresses the raw PT212 displacement data.
    """

    try:
        from .video_seam import analyze_video_boundary

        analysis = analyze_video_boundary(
            previous_images,
            current_raw_images,
            trim_frames=int(trim_frames),
            boundary_index=int(boundary_index),
        )
    except Exception as exc:
        return {
            "pt212_interpretation": "scene_change_or_unknown",
            "scene_analysis_available": False,
            "scene_analysis_classification": "unavailable",
            "scene_cut": None,
            "scene_cut_score": None,
            "scene_analysis_error": type(exc).__name__,
            "production_gate": False,
        }

    return {
        "pt212_interpretation": (
            "scene_change_or_unknown"
            if analysis.scene_cut
            else "continuous_shot_candidate"
        ),
        "scene_analysis_available": True,
        "scene_analysis_classification": str(analysis.classification),
        "scene_cut": bool(analysis.scene_cut),
        "scene_cut_score": float(analysis.scene_cut_score),
        "scene_analysis_error": None,
        "production_gate": False,
    }


def _decoded_audio_window_metrics(
    waveform: torch.Tensor,
    *,
    sample_rate: int,
    high_band_hz: float,
) -> dict[str, float]:
    if not torch.is_tensor(waveform) or waveform.ndim < 2 or int(waveform.shape[-1]) < 8:
        raise ValueError("decoded audio diagnostic requires [...,samples] waveform data")
    rate = int(sample_rate)
    if rate <= 0:
        raise ValueError("decoded audio diagnostic requires positive sample rate")
    samples = waveform.detach().to(device="cpu", dtype=torch.float32)
    if not bool(torch.isfinite(samples).all().item()):
        raise ValueError("decoded audio diagnostic waveform contains NaN or Inf")

    rms = float(torch.sqrt(torch.mean(samples.square())).item())
    peak = float(samples.abs().amax().item())
    flat = samples.reshape(-1, int(samples.shape[-1]))
    centered = flat - flat.mean(dim=-1, keepdim=True)
    window = torch.hann_window(
        int(centered.shape[-1]),
        periodic=False,
        dtype=centered.dtype,
        device=centered.device,
    )
    spectrum = torch.fft.rfft(centered * window, dim=-1)
    power = spectrum.abs().square().mean(dim=0)
    freqs = torch.fft.rfftfreq(
        int(centered.shape[-1]),
        d=1.0 / float(rate),
        device=power.device,
    )
    total_power = float(power.sum().item())
    if total_power <= _EPS:
        centroid = 0.0
        high_fraction = 0.0
    else:
        centroid = float((power * freqs).sum().item() / total_power)
        high_fraction = float(power[freqs >= float(high_band_hz)].sum().item() / total_power)
    return {
        "rms": rms,
        "peak": peak,
        "spectral_centroid_hz": centroid,
        "high_band_fraction": high_fraction,
    }


def measure_decoded_audio_boundary(
    previous_waveform: torch.Tensor,
    current_waveform: torch.Tensor,
    *,
    sample_rate: int,
    window_seconds: float = 0.5,
    high_band_hz: float = 4000.0,
) -> dict[str, Any]:
    """Measure bounded decoded-audio level and spectral change at one chunk boundary."""

    rate = int(sample_rate)
    window_seconds = float(window_seconds)
    high_band_hz = float(high_band_hz)
    if rate <= 0 or not math.isfinite(window_seconds) or window_seconds <= 0.0:
        raise ValueError("decoded audio diagnostic requires a positive finite window")
    if not math.isfinite(high_band_hz) or not 0.0 < high_band_hz < rate / 2.0:
        raise ValueError("decoded audio diagnostic high-band threshold must lie below Nyquist")
    if not torch.is_tensor(previous_waveform) or not torch.is_tensor(current_waveform):
        raise ValueError("decoded audio diagnostic requires tensor waveforms")
    if tuple(previous_waveform.shape[:-1]) != tuple(current_waveform.shape[:-1]):
        raise ValueError("decoded audio channel structure changed across boundary")

    requested = max(8, int(round(rate * window_seconds)))
    count = min(
        requested,
        int(previous_waveform.shape[-1]),
        int(current_waveform.shape[-1]),
    )
    if count < 8:
        raise ValueError("decoded audio boundary has insufficient samples for measurement")

    previous = _decoded_audio_window_metrics(
        previous_waveform[..., -count:],
        sample_rate=rate,
        high_band_hz=high_band_hz,
    )
    current = _decoded_audio_window_metrics(
        current_waveform[..., :count],
        sample_rate=rate,
        high_band_hz=high_band_hz,
    )
    ratio = (current["rms"] + _EPS) / (previous["rms"] + _EPS)
    return {
        "audio_boundary_version": 1,
        "sample_rate": rate,
        "window_samples": count,
        "window_seconds": count / float(rate),
        "high_band_hz": high_band_hz,
        "previous_rms": previous["rms"],
        "current_rms": current["rms"],
        "current_over_previous_rms_ratio": ratio,
        "current_over_previous_db": 20.0 * math.log10(ratio),
        "previous_peak": previous["peak"],
        "current_peak": current["peak"],
        "previous_spectral_centroid_hz": previous["spectral_centroid_hz"],
        "current_spectral_centroid_hz": current["spectral_centroid_hz"],
        "spectral_centroid_delta_hz": (
            current["spectral_centroid_hz"] - previous["spectral_centroid_hz"]
        ),
        "previous_high_band_fraction": previous["high_band_fraction"],
        "current_high_band_fraction": current["high_band_fraction"],
        "high_band_fraction_delta": (
            current["high_band_fraction"] - previous["high_band_fraction"]
        ),
    }



def _decoded_audio_overlap_pair_metrics(
    previous: torch.Tensor,
    current: torch.Tensor,
) -> dict[str, float]:
    if previous.shape != current.shape or previous.ndim < 2:
        raise ValueError("decoded audio overlap regions must have matching [...,samples] shapes")
    left = previous.detach().to(device="cpu", dtype=torch.float32)
    right = current.detach().to(device="cpu", dtype=torch.float32)
    if not bool(torch.isfinite(left).all().item()) or not bool(torch.isfinite(right).all().item()):
        raise ValueError("decoded audio overlap contains NaN or Inf")

    left_rms = float(torch.sqrt(torch.mean(left.square())).item())
    right_rms = float(torch.sqrt(torch.mean(right.square())).item())
    ratio = (right_rms + _EPS) / (left_rms + _EPS)

    # Scalar correlation/gain receipts use FP64 accumulation so long exact
    # carried-prefix regions do not report impossible |corr| > 1 or a spurious
    # sub-ppm mismatch solely from FP32 reduction order.
    left_flat = left.reshape(-1).to(dtype=torch.float64)
    right_flat = right.reshape(-1).to(dtype=torch.float64)
    left_centered = left_flat - left_flat.mean()
    right_centered = right_flat - right_flat.mean()
    correlation_denominator = torch.linalg.vector_norm(left_centered) * torch.linalg.vector_norm(
        right_centered
    )
    if float(correlation_denominator.item()) <= _EPS:
        correlation = 1.0 if torch.equal(left, right) else 0.0
    else:
        correlation = float(
            torch.dot(left_centered, right_centered).item()
            / float(correlation_denominator.item())
        )
        correlation = max(-1.0, min(1.0, correlation))

    gain_denominator = float(torch.dot(left_flat, left_flat).item())
    if gain_denominator <= _EPS:
        gain = 1.0 if right_rms <= _EPS else 0.0
    else:
        gain = float(torch.dot(left_flat, right_flat).item() / gain_denominator)
    residual = right_flat - left_flat * gain
    residual_rms = float(torch.sqrt(torch.mean(residual.square())).item())
    residual_ratio = residual_rms / max(right_rms, _EPS)

    return {
        "previous_rms": left_rms,
        "current_rms": right_rms,
        "current_over_previous_db": 20.0 * math.log10(ratio),
        "correlation": correlation,
        "least_squares_gain": gain,
        "least_squares_gain_db": (
            20.0 * math.log10(abs(gain)) if abs(gain) > _EPS else float("-inf")
        ),
        "gain_aligned_residual_rms_ratio": residual_ratio,
    }


def measure_decoded_audio_overlap_context(
    previous_waveform: torch.Tensor,
    current_waveform: torch.Tensor,
    *,
    sample_rate: int,
    prefix_latents: int,
    latent_hz: int = 40,
    edge_seconds: float = 0.25,
    decoder_context_margin_latents: int = MINIMAX_H3_AUDIO_DECODER_CONTEXT_MARGIN_LATENTS,
) -> dict[str, Any]:
    """Compare the waveform decoded twice from a bit-identical carried audio prefix.

    Continuum's exact audio carry duplicates the same latent prefix at the tail of
    the previous physical group and the head of the continuation group. Core VAE
    Decode Audio evaluates those groups independently. This diagnostic compares
    the decoded copies before phase alignment, seam processing, or timeline trim.

    The Core MiniMax-H3 audio decoder is non-causal and has a wide finite
    receptive field, so an arbitrary center window is not a valid context-free
    control. The interior region excludes a conservative 30 latent ticks from
    both ends of the carried prefix. For the production 65-tick carry this leaves
    five ticks / 125 ms whose complete decoder dependency is still inside the
    bit-identical carried prefix.

    A nearly constant gain with high correlation across the complete overlap and
    the context-safe interior is evidence for decode-call-level scaling. Edge-only
    differences with a matching interior implicate decoder context/padding. If the
    context-safe interior and overlap remain close while the retained boundary is
    loud, the newly generated suffix is the first demonstrated source.
    """

    rate = int(sample_rate)
    ticks = int(prefix_latents)
    latent_hz = int(latent_hz)
    if rate <= 0 or latent_hz <= 0 or rate % latent_hz:
        raise ValueError("decoded audio overlap requires sample rate divisible by latent_hz")
    if ticks <= 0:
        raise ValueError("decoded audio overlap requires a positive exact prefix length")
    if not torch.is_tensor(previous_waveform) or not torch.is_tensor(current_waveform):
        raise ValueError("decoded audio overlap requires tensor waveforms")
    if tuple(previous_waveform.shape[:-1]) != tuple(current_waveform.shape[:-1]):
        raise ValueError("decoded audio channel structure changed across carried overlap")

    samples_per_latent = rate // latent_hz
    context_margin_latents = int(decoder_context_margin_latents)
    if context_margin_latents < 0:
        raise ValueError("decoded audio overlap context margin must be non-negative")
    interior_latents = ticks - 2 * context_margin_latents
    if interior_latents <= 0:
        raise ValueError(
            "decoded audio carried prefix is too short to expose a decoder-context-safe interior: "
            f"prefix={ticks}, margin={context_margin_latents}"
        )
    overlap_samples = ticks * samples_per_latent
    if (
        overlap_samples > int(previous_waveform.shape[-1])
        or overlap_samples > int(current_waveform.shape[-1])
    ):
        raise ValueError(
            "decoded audio carried-prefix overlap exceeds one of the decoded waveforms"
        )

    previous = previous_waveform[..., -overlap_samples:]
    current = current_waveform[..., :overlap_samples]
    edge = min(
        max(8, int(round(float(edge_seconds) * rate))),
        max(8, overlap_samples // 3),
    )
    interior_start = context_margin_latents * samples_per_latent
    interior_stop = (ticks - context_margin_latents) * samples_per_latent
    interior_samples = interior_stop - interior_start

    regions = {
        "full": (0, overlap_samples),
        "head": (0, edge),
        "interior": (interior_start, interior_stop),
        "tail": (overlap_samples - edge, overlap_samples),
    }
    measured = {
        name: _decoded_audio_overlap_pair_metrics(
            previous[..., start:stop],
            current[..., start:stop],
        )
        for name, (start, stop) in regions.items()
    }

    previous_whole_std = float(
        torch.std(
            previous_waveform.detach().to(device="cpu", dtype=torch.float32),
            dim=tuple(range(previous_waveform.ndim)),
        ).item()
    )
    current_whole_std = float(
        torch.std(
            current_waveform.detach().to(device="cpu", dtype=torch.float32),
            dim=tuple(range(current_waveform.ndim)),
        ).item()
    )
    return {
        "audio_overlap_context_version": 2,
        "sample_rate": rate,
        "latent_hz": latent_hz,
        "prefix_latents": ticks,
        "samples_per_latent": samples_per_latent,
        "overlap_samples": overlap_samples,
        "overlap_seconds": overlap_samples / float(rate),
        "decoder_context_margin_latents": context_margin_latents,
        "decoder_context_margin_seconds": context_margin_latents / float(latent_hz),
        "interior_latents": interior_latents,
        "interior_samples": interior_samples,
        "interior_seconds": interior_samples / float(rate),
        "previous_whole_std": previous_whole_std,
        "current_whole_std": current_whole_std,
        "previous_core_normalizer_provably_inactive": (
            previous_whole_std < CORE_AUDIO_NORMALIZED_STD - CORE_AUDIO_NORMALIZED_STD_TOLERANCE
        ),
        "current_core_normalizer_provably_inactive": (
            current_whole_std < CORE_AUDIO_NORMALIZED_STD - CORE_AUDIO_NORMALIZED_STD_TOLERANCE
        ),
        "regions": measured,
    }


def measure_decoded_boundary_trajectory(
    previous_images: torch.Tensor,
    current_raw_images: torch.Tensor,
    *,
    trim_frames: int,
    boundary_global_frame: int,
    forward_frames: int = DEFAULT_FORWARD_FRAMES,
    previous_transitions: int = DEFAULT_PREVIOUS_TRANSITIONS,
) -> dict[str, Any]:
    """Measure decoded VAE framing motion around one retained chunk boundary."""

    _validate_images(previous_images, "previous decoded trajectory frames")
    _validate_images(current_raw_images, "current decoded trajectory frames")
    trim_frames = int(trim_frames)
    forward_frames = int(forward_frames)
    if int(previous_images.shape[0]) < 2:
        raise ValueError("decoded trajectory requires at least two previous frames")
    if trim_frames < 0 or trim_frames >= int(current_raw_images.shape[0]):
        raise ValueError("decoded trajectory trim position is outside current frames")
    available = int(current_raw_images.shape[0]) - trim_frames
    forward_frames = min(max(2, forward_frames), available)

    previous_start = max(
        0,
        int(previous_images.shape[0]) - max(2, int(previous_transitions) + 1),
    )
    previous_rgb, scale_x, scale_y = _prepare_rgb(previous_images[previous_start:])
    current_rgb, current_scale_x, current_scale_y = _prepare_rgb(
        current_raw_images[trim_frames : trim_frames + forward_frames]
    )
    if not math.isclose(scale_x, current_scale_x, rel_tol=0.0, abs_tol=1.0e-9) or not math.isclose(
        scale_y, current_scale_y, rel_tol=0.0, abs_tol=1.0e-9
    ):
        raise ValueError("decoded trajectory geometry changed across boundary")

    return {
        "trajectory_version": 1,
        "boundary_global_frame": int(boundary_global_frame),
        "current_trim_frame": trim_frames,
        "forward_frames": forward_frames,
        "comparison_long_side": COMPARE_LONG_SIDE,
        "upper45": _trajectory_for_roi(
            previous_rgb,
            current_rgb,
            roi_fraction=0.45,
            scale_x=scale_x,
            scale_y=scale_y,
            forward_frames=forward_frames,
            previous_transitions=previous_transitions,
        ),
        "full": _trajectory_for_roi(
            previous_rgb,
            current_rgb,
            roi_fraction=1.0,
            scale_x=scale_x,
            scale_y=scale_y,
            forward_frames=forward_frames,
            previous_transitions=previous_transitions,
        ),
    }

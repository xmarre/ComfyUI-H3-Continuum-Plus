"""Decoded chunk assembly for latent-first H3 Continuum V3."""

from __future__ import annotations

import logging
import math
import time
from typing import Any

import torch

from ..media import validate_audio
from ..v2.decoder import _slice_audio_for_timeline, enforce_total_frames
from ..constants import (
    DIAGNOSTICS_FULL,
    DIAGNOSTICS_OFF,
    DIAGNOSTICS_OPTIONS,
    normalize_diagnostics_mode,
)
from ..v2.seam_guard import correct_audio_seam
from .audio_phase import phase_align_decoded_audio
from .plan import FPS, validate_assembly_plan
from .trajectory_diagnostics import (
    measure_decoded_audio_boundary,
    measure_decoded_audio_overlap_context,
    measure_decoded_boundary_trajectory,
    interpret_decoded_boundary_shot,
)

LOG = logging.getLogger("h3_continuum_join")

AUDIO_SEAM_OFF = "Off"
AUDIO_SEAM_AUTO = "Auto"
AUDIO_SEAM_OPTIONS = (AUDIO_SEAM_OFF, AUDIO_SEAM_AUTO)
VIDEO_SEAM_OFF = "Off"
VIDEO_SEAM_ANALYZE = "Analyze Only"
VIDEO_SEAM_AUTO = "Auto"
VIDEO_SEAM_AUTO_2 = "Auto 2"
VIDEO_SEAM_ANALYSIS_OPTIONS = (
    VIDEO_SEAM_AUTO,
    VIDEO_SEAM_AUTO_2,
    VIDEO_SEAM_ANALYZE,
    VIDEO_SEAM_OFF,
)
IMAGE_OUTPUT_AUTO = "Auto"
IMAGE_OUTPUT_CUDA = "CUDA"
IMAGE_OUTPUT_CPU = "CPU"
IMAGE_OUTPUT_DEVICE_OPTIONS = (
    IMAGE_OUTPUT_AUTO,
    IMAGE_OUTPUT_CUDA,
    IMAGE_OUTPUT_CPU,
)
_GIB = 1024**3
_AUTO_CUDA_MIN_HEADROOM = 2 * _GIB
_AUTO_CUDA_HEADROOM_FRACTION = 0.10


def _singleton(value: Any, name: str) -> Any:
    while isinstance(value, list):
        if len(value) != 1:
            raise ValueError(f"{name} must contain exactly one value")
        value = value[0]
    return value


def _chunk_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        if len(value) == 1 and isinstance(value[0], list):
            return list(value[0])
        return list(value)
    return [value]


def _format_elapsed(seconds: float) -> str:
    total = max(0.0, float(seconds))
    hours, remainder = divmod(total, 3600.0)
    minutes, seconds = divmod(remainder, 60.0)
    return f"{int(hours):02d}:{int(minutes):02d}:{seconds:05.2f}"


def _tensor_nbytes(shape: tuple[int, ...], dtype: torch.dtype) -> int:
    elements = 1
    for dimension in shape:
        elements *= int(dimension)
    return elements * torch.empty((), dtype=dtype).element_size()


def _resolve_image_output_device(
    images: list[Any],
    output_shape: tuple[int, int, int, int],
    preference: str,
) -> torch.device:
    preference = str(preference)
    if preference not in IMAGE_OUTPUT_DEVICE_OPTIONS:
        raise ValueError(f"unknown Image Output Device: {preference!r}")
    if preference == IMAGE_OUTPUT_CPU:
        return torch.device("cpu")

    if not torch.cuda.is_available():
        if preference == IMAGE_OUTPUT_CUDA:
            raise RuntimeError("Image Output Device CUDA requires CUDA")
        return torch.device("cpu")

    first_tensor = next((item for item in images if torch.is_tensor(item)), None)
    if first_tensor is None:
        raise ValueError("decoded images contain no IMAGE tensors")
    cuda_device = (
        first_tensor.device
        if first_tensor.device.type == "cuda"
        else torch.device("cuda")
    )
    if preference == IMAGE_OUTPUT_CUDA:
        return cuda_device

    # Auto always checks the new output allocation against currently free VRAM,
    # including when decoded inputs themselves are already resident on CUDA.
    try:
        free_bytes, total_bytes = torch.cuda.mem_get_info(cuda_device)
    except Exception:
        return torch.device("cpu")

    output_bytes = _tensor_nbytes(output_shape, first_tensor.dtype)
    frame_bytes = _tensor_nbytes(output_shape[1:], first_tensor.dtype)
    headroom = max(
        _AUTO_CUDA_MIN_HEADROOM,
        int(total_bytes * _AUTO_CUDA_HEADROOM_FRACTION),
    )
    if free_bytes >= output_bytes + frame_bytes + headroom:
        return cuda_device
    return torch.device("cpu")


def _apply_video_patch(
    image_buffer: torch.Tensor,
    *,
    frame_start: int,
    net_frames: int,
    patch: torch.Tensor | None,
) -> None:
    if patch is None:
        return
    if not torch.is_tensor(patch) or patch.ndim != 4:
        raise ValueError("video seam patch must be an IMAGE tensor [frames,H,W,C]")
    patch_frames = int(patch.shape[0])
    if patch_frames < 1 or patch_frames > int(net_frames):
        raise ValueError("video seam patch frame count is outside the retained segment")
    if tuple(patch.shape[1:]) != tuple(image_buffer.shape[1:]):
        raise ValueError("video seam patch geometry changed at assembly")
    image_buffer[frame_start : frame_start + patch_frames].copy_(
        patch.to(device=image_buffer.device, dtype=image_buffer.dtype)
    )


def _enforce_image_duration_in_place(
    image_buffer: torch.Tensor,
    *,
    current_frames: int,
    target_frames: int,
    preserve_final_frame: bool,
) -> torch.Tensor:
    """Apply exact-duration image trim/pad inside the already-owned output buffer."""
    current_frames = int(current_frames)
    target_frames = int(target_frames)
    if target_frames < 1:
        raise ValueError("target_frames must be positive")
    if current_frames < 1:
        raise ValueError("cannot adjust an empty image sequence")
    if int(image_buffer.shape[0]) < max(current_frames, target_frames):
        raise ValueError("image output buffer has insufficient exact-duration capacity")

    adjustment = target_frames - current_frames
    if adjustment < 0:
        if preserve_final_frame and target_frames >= 2:
            image_buffer[target_frames - 1].copy_(image_buffer[current_frames - 1])
    elif adjustment > 0:
        image_buffer[current_frames:target_frames].copy_(
            image_buffer[current_frames - 1 : current_frames].expand(
                adjustment, *image_buffer.shape[1:]
            )
        )
    return image_buffer[:target_frames]


def assemble_decoded_chunks(
    *,
    images: list[Any],
    audio: list[Any],
    assembly_plan: dict[str, Any],
    exact_total_duration: bool,
    audio_seam: str,
    diagnostics: str,
    image_output_device: str = IMAGE_OUTPUT_AUTO,
    video_patches: dict[int, torch.Tensor] | None = None,
):
    plan = validate_assembly_plan(assembly_plan)
    diagnostics_mode = normalize_diagnostics_mode(diagnostics)
    if audio_seam not in AUDIO_SEAM_OPTIONS:
        raise ValueError(f"unknown Audio Seam mode: {audio_seam!r}")

    chunk_plans = list(
        plan["decode_groups"] if "decode_groups" in plan else plan["chunks"]
    )
    if len(images) != len(chunk_plans) or len(audio) != len(chunk_plans):
        raise ValueError(
            "decoded chunk count does not match the V3 assembly plan: "
            f"images={len(images)}, audio={len(audio)}, plan={len(chunk_plans)}"
        )

    total_retained_frames = sum(int(item["net_frames"]) for item in chunk_plans)
    target_frames = int(plan["target_frames"])
    image_capacity_frames = (
        max(total_retained_frames, target_frames)
        if exact_total_duration
        else total_retained_frames
    )
    image_buffer = None
    audio_buffer = None
    audio_rate = None
    previous_decoded_audio_cpu = None
    frame_cursor = 0
    reports = [
        "H3 Continuum Assemble V3",
        f"Audio Seam: {audio_seam}. Video seam correction is disabled.",
    ]
    video_patches = video_patches or {}

    for index, (raw_images, raw_audio, chunk) in enumerate(
        zip(images, audio, chunk_plans), start=1
    ):
        if not torch.is_tensor(raw_images) or raw_images.ndim != 4:
            raise ValueError(f"decoded image chunk {index} must be [frames,H,W,C]")
        raw_images = raw_images.detach()
        total_frames = int(chunk["total_frames"])
        trim_frames = int(chunk["trim_frames"])
        net_frames = int(chunk["net_frames"])
        if int(raw_images.shape[0]) < total_frames:
            raise ValueError(
                f"decoded image chunk {index} has {raw_images.shape[0]} frames; "
                f"expected at least {total_frames}"
            )
        segment_images = raw_images[:total_frames][trim_frames:]
        if int(segment_images.shape[0]) != net_frames:
            raise ValueError(
                f"decoded image chunk {index} retained {segment_images.shape[0]} frames; "
                f"plan expects {net_frames}"
            )

        waveform, sample_rate = validate_audio(raw_audio)
        decoded_audio_cpu = {
            "waveform": waveform.detach().to("cpu"),
            "sample_rate": int(sample_rate),
        }
        if index > 1 and previous_decoded_audio_cpu is not None:
            prefix_latents = chunk.get("audio_phase_prefix_latents")
            phase_verified = bool(chunk.get("audio_phase_verified", False))
            if phase_verified and type(prefix_latents) is int and prefix_latents > 0:
                try:
                    overlap = measure_decoded_audio_overlap_context(
                        previous_decoded_audio_cpu["waveform"],
                        decoded_audio_cpu["waveform"],
                        sample_rate=int(sample_rate),
                        prefix_latents=prefix_latents,
                    )
                    full = overlap["regions"]["full"]
                    head = overlap["regions"]["head"]
                    interior = overlap["regions"]["interior"]
                    tail = overlap["regions"]["tail"]
                    LOG.info(
                        "H3C-PT214 decoded-audio-carried-overlap receipt "
                        "version=%d boundary_global_frame=%d prefix_latents=%d overlap_samples=%d "
                        "overlap_seconds=%.6f decoder_context_margin_latents=%d "
                        "interior_latents=%d interior_samples=%d interior_seconds=%.6f "
                        "previous_whole_std=%.9f current_whole_std=%.9f "
                        "previous_core_normalizer_provably_inactive=%s "
                        "current_core_normalizer_provably_inactive=%s "
                        "full_db=%+.4f full_corr=%.6f full_gain=%+.6f full_gain_db=%+.4f "
                        "full_residual_ratio=%.6f "
                        "head_db=%+.4f head_corr=%.6f "
                        "interior_db=%+.4f interior_corr=%.6f interior_gain=%+.6f "
                        "interior_gain_db=%+.4f interior_residual_ratio=%.6f "
                        "tail_db=%+.4f tail_corr=%.6f",
                        int(overlap["audio_overlap_context_version"]),
                        frame_cursor,
                        int(overlap["prefix_latents"]),
                        int(overlap["overlap_samples"]),
                        float(overlap["overlap_seconds"]),
                        int(overlap["decoder_context_margin_latents"]),
                        int(overlap["interior_latents"]),
                        int(overlap["interior_samples"]),
                        float(overlap["interior_seconds"]),
                        float(overlap["previous_whole_std"]),
                        float(overlap["current_whole_std"]),
                        bool(overlap["previous_core_normalizer_provably_inactive"]),
                        bool(overlap["current_core_normalizer_provably_inactive"]),
                        float(full["current_over_previous_db"]),
                        float(full["correlation"]),
                        float(full["least_squares_gain"]),
                        float(full["least_squares_gain_db"]),
                        float(full["gain_aligned_residual_rms_ratio"]),
                        float(head["current_over_previous_db"]),
                        float(head["correlation"]),
                        float(interior["current_over_previous_db"]),
                        float(interior["correlation"]),
                        float(interior["least_squares_gain"]),
                        float(interior["least_squares_gain_db"]),
                        float(interior["gain_aligned_residual_rms_ratio"]),
                        float(tail["current_over_previous_db"]),
                        float(tail["correlation"]),
                    )
                    if diagnostics_mode == DIAGNOSTICS_FULL:
                        reports.append(
                            "decoded carried-audio overlap "
                            f"{index-1}->{index}: full={full['current_over_previous_db']:+.3f} dB "
                            f"corr={full['correlation']:.5f}, "
                            f"head/interior/tail={head['current_over_previous_db']:+.3f}/"
                            f"{interior['current_over_previous_db']:+.3f}/"
                            f"{tail['current_over_previous_db']:+.3f} dB, "
                            f"interior={overlap['interior_latents']} ticks "
                            f"({overlap['interior_seconds']:.3f}s)"
                        )
                except Exception as exc:
                    LOG.warning(
                        "H3C-PT214 decoded-audio-carried-overlap unavailable "
                        "boundary_global_frame=%d reason=%s: %s",
                        frame_cursor,
                        type(exc).__name__,
                        exc,
                    )
        previous_decoded_audio_cpu = decoded_audio_cpu
        raw_audio_cpu, phase_report = phase_align_decoded_audio(
            decoded_audio_cpu,
            group=chunk,
            frame_cursor=frame_cursor,
        )
        waveform = raw_audio_cpu["waveform"]
        sample_rate = int(raw_audio_cpu["sample_rate"])
        if audio_rate is None:
            audio_rate = sample_rate
        elif sample_rate != audio_rate:
            raise ValueError(
                f"audio sample rate changed between chunks: {audio_rate} -> {sample_rate}"
            )

        if phase_report["applied"] or diagnostics_mode == DIAGNOSTICS_FULL:
            reports.append(
                f"audio phase group {index}: verified={phase_report['verified']}, "
                f"origin_latent={phase_report['origin_latent']}, "
                f"native_trim={phase_report['native_trim_samples']}, "
                f"phase_trim={phase_report['phase_trim_samples']}, "
                f"delta={int(phase_report['phase_delta_samples']):+d} samples, "
                f"reason={phase_report['reason']}"
            )

        frame_stop = frame_cursor + net_frames
        segment_waveform = _slice_audio_for_timeline(
            raw_audio_cpu,
            trim_frames=trim_frames,
            frame_start=frame_cursor,
            frame_stop=frame_stop,
        )

        if image_buffer is None:
            output_shape = (
                image_capacity_frames,
                int(segment_images.shape[1]),
                int(segment_images.shape[2]),
                int(segment_images.shape[3]),
            )
            output_device = _resolve_image_output_device(
                images,
                output_shape,
                image_output_device,
            )
            image_buffer = torch.empty(
                output_shape,
                dtype=segment_images.dtype,
                device=output_device,
            )
            if diagnostics_mode == DIAGNOSTICS_FULL:
                actual_height = int(segment_images.shape[1])
                actual_width = int(segment_images.shape[2])
                plan_width = int(plan["width"])
                plan_height = int(plan["height"])
                scale_x = actual_width / float(plan_width)
                scale_y = actual_height / float(plan_height)
                output_mib = _tensor_nbytes(output_shape, segment_images.dtype) / (1024**2)
                reports.append(
                    "Decoded geometry: "
                    f"{actual_width}x{actual_height} vs plan {plan_width}x{plan_height} "
                    f"({scale_x:.3f}x/{scale_y:.3f}x); image buffer "
                    f"{image_capacity_frames} frames, {output_mib:.1f} MiB on {output_device}."
                )
        elif tuple(segment_images.shape[1:]) != tuple(image_buffer.shape[1:]):
            raise ValueError(f"decoded image geometry changed at chunk {index}")

        pt212_recorded = False
        if index > 1 and image_buffer is not None and frame_cursor > 1:
            try:
                previous_window = image_buffer[max(0, frame_cursor - 8) : frame_cursor]
                shot = interpret_decoded_boundary_shot(
                    previous_window,
                    raw_images[:total_frames],
                    trim_frames=trim_frames,
                    boundary_index=index - 1,
                )
                trajectory = measure_decoded_boundary_trajectory(
                    previous_window,
                    raw_images[:total_frames],
                    trim_frames=trim_frames,
                    boundary_global_frame=frame_cursor,
                    forward_frames=6,
                    previous_transitions=4,
                )
                for roi_name in ("upper45", "full"):
                    fields = trajectory[roi_name]
                    LOG.info(
                        "H3C-PT212 decoded-trajectory receipt "
                        "boundary_global_frame=%d trim_frame=%d roi=%s "
                        "pairwise_dx_px=%s pairwise_dy_px=%s "
                        "pairwise_net_dx_px=%.4f pairwise_net_dy_px=%.4f "
                        "anchor_dx_px=%s anchor_dy_px=%s "
                        "anchor_final_dx_px=%.4f anchor_final_dy_px=%.4f "
                        "pre_median_dx_px=%.4f pre_median_dy_px=%.4f "
                        "post_first3_median_dx_px=%.4f post_first3_median_dy_px=%.4f "
                        "response=%s clipped=%s "
                        "pt212_interpretation=%s scene_analysis_available=%s "
                        "scene_analysis_classification=%s scene_cut=%s scene_cut_score=%s "
                        "production_gate=%s",
                        int(trajectory["boundary_global_frame"]),
                        int(trajectory["current_trim_frame"]),
                        roi_name,
                        fields["pairwise_dx_px"],
                        fields["pairwise_dy_px"],
                        fields["pairwise_net_dx_px"],
                        fields["pairwise_net_dy_px"],
                        fields["anchor_dx_px"],
                        fields["anchor_dy_px"],
                        fields["anchor_final_dx_px"],
                        fields["anchor_final_dy_px"],
                        fields["pre_median_dx_px"],
                        fields["pre_median_dy_px"],
                        fields["post_first3_median_dx_px"],
                        fields["post_first3_median_dy_px"],
                        fields["pairwise_response"],
                        fields["pairwise_clipped"],
                        shot["pt212_interpretation"],
                        shot["scene_analysis_available"],
                        shot["scene_analysis_classification"],
                        shot["scene_cut"],
                        shot["scene_cut_score"],
                        shot["production_gate"],
                    )
                    pt212_recorded = True
                    if diagnostics_mode == DIAGNOSTICS_FULL:
                        reports.append(
                            "decoded trajectory "
                            f"{index-1}->{index} {roi_name}: "
                            f"dy={fields['pairwise_dy_px']}, "
                            f"net={fields['pairwise_net_dy_px']:+.3f}px, "
                            f"pre_median={fields['pre_median_dy_px']:+.3f}px, "
                            f"shot={shot['pt212_interpretation']}"
                        )
            except Exception as exc:
                LOG.warning(
                    "H3C-PT212 decoded-trajectory unavailable boundary_global_frame=%d "
                    "trim_frame=%d reason=%s: %s",
                    frame_cursor,
                    trim_frames,
                    type(exc).__name__,
                    exc,
                )

        # copy_ supports CPU<->CUDA directly, so do not first materialize a full
        # retained-chunk CUDA temporary with segment_images.to(device=...).
        image_buffer[frame_cursor:frame_stop].copy_(segment_images)
        video_patch = video_patches.get(index - 1)
        _apply_video_patch(
            image_buffer,
            frame_start=frame_cursor,
            net_frames=net_frames,
            patch=video_patch,
        )
        if index > 1:
            patch_frames = int(video_patch.shape[0]) if torch.is_tensor(video_patch) else 0
            LOG.info(
                "H3C-PT216 video-assembly-patch receipt "
                "boundary_global_frame=%d boundary_index=%d applied=%s patch_frames=%d "
                "pre_patch_pt212_recorded=%s source=pre_patch_raw_decode",
                frame_cursor,
                index - 1,
                video_patch is not None,
                patch_frames,
                pt212_recorded,
            )

        if audio_buffer is None:
            audio_buffer = torch.empty(
                (
                    *waveform.shape[:-1],
                    int(round(total_retained_frames / FPS * sample_rate)),
                ),
                dtype=waveform.dtype,
                device="cpu",
            )
        elif tuple(segment_waveform.shape[:-1]) != tuple(audio_buffer.shape[:-1]):
            raise ValueError(
                "decoded audio batch/channel structure changed between chunks"
            )

        sample_start = int(round(frame_cursor / FPS * sample_rate))
        sample_stop = int(round(frame_stop / FPS * sample_rate))
        if index > 1 and sample_start > 0:
            try:
                audio_boundary = measure_decoded_audio_boundary(
                    audio_buffer[..., :sample_start],
                    segment_waveform,
                    sample_rate=sample_rate,
                )
                LOG.info(
                    "H3C-PT213 decoded-audio-boundary receipt "
                    "stage=pre_seam boundary_global_frame=%d "
                    "window_samples=%d window_seconds=%.6f sample_rate=%d "
                    "previous_rms=%.9f current_rms=%.9f "
                    "current_over_previous_rms_ratio=%.6f current_over_previous_db=%+.4f "
                    "previous_peak=%.9f current_peak=%.9f "
                    "previous_spectral_centroid_hz=%.3f current_spectral_centroid_hz=%.3f "
                    "spectral_centroid_delta_hz=%+.3f high_band_hz=%.1f "
                    "previous_high_band_fraction=%.9f current_high_band_fraction=%.9f "
                    "high_band_fraction_delta=%+.9f phase_delta_samples=%+d",
                    frame_cursor,
                    int(audio_boundary["window_samples"]),
                    float(audio_boundary["window_seconds"]),
                    int(audio_boundary["sample_rate"]),
                    float(audio_boundary["previous_rms"]),
                    float(audio_boundary["current_rms"]),
                    float(audio_boundary["current_over_previous_rms_ratio"]),
                    float(audio_boundary["current_over_previous_db"]),
                    float(audio_boundary["previous_peak"]),
                    float(audio_boundary["current_peak"]),
                    float(audio_boundary["previous_spectral_centroid_hz"]),
                    float(audio_boundary["current_spectral_centroid_hz"]),
                    float(audio_boundary["spectral_centroid_delta_hz"]),
                    float(audio_boundary["high_band_hz"]),
                    float(audio_boundary["previous_high_band_fraction"]),
                    float(audio_boundary["current_high_band_fraction"]),
                    float(audio_boundary["high_band_fraction_delta"]),
                    int(phase_report.get("phase_delta_samples", 0)),
                )
                if diagnostics_mode == DIAGNOSTICS_FULL:
                    reports.append(
                        "decoded audio boundary "
                        f"{index-1}->{index} pre-seam: "
                        f"level={audio_boundary['current_over_previous_db']:+.3f} dB, "
                        f"centroid_delta={audio_boundary['spectral_centroid_delta_hz']:+.1f} Hz, "
                        f"high_band_delta={audio_boundary['high_band_fraction_delta']:+.6f}"
                    )
            except Exception as exc:
                LOG.warning(
                    "H3C-PT213 decoded-audio-boundary unavailable "
                    "stage=pre_seam boundary_global_frame=%d reason=%s: %s",
                    frame_cursor,
                    type(exc).__name__,
                    exc,
                )

        seam_report = None
        if index > 1 and audio_seam == AUDIO_SEAM_AUTO:
            try:
                cut_sample = int(round(trim_frames / FPS * sample_rate))
                patch, metrics, fade_samples, level_gain, dc_bias = correct_audio_seam(
                    audio_buffer[..., :sample_start],
                    waveform,
                    sample_rate=sample_rate,
                    cut_sample=cut_sample,
                )
                if patch is not None and int(patch.shape[-1]) > 0:
                    patch_start = sample_start - int(patch.shape[-1])
                    if patch_start < 0:
                        raise ValueError("audio seam patch starts before the output")
                    audio_buffer[..., patch_start:sample_start].copy_(patch)
                seam_report = (
                    f"audio seam {index-1}->{index}: "
                    f"corr={metrics.correlation_before:.4f}->{metrics.correlation_after:.4f}, "
                    f"jump={metrics.boundary_jump_before:.6f}->{metrics.boundary_jump_after:.6f}, "
                    f"fade={fade_samples} samples"
                )
                if diagnostics_mode == DIAGNOSTICS_FULL:
                    seam_report += (
                        f", offset={metrics.offset_samples:+d}, "
                        f"gain={level_gain:.4f}, dc={dc_bias:+.6f}"
                    )
            except Exception as exc:
                seam_report = (
                    f"audio seam {index-1}->{index}: fallback to native boundary "
                    f"({type(exc).__name__}: {exc})"
                )

        audio_buffer[..., sample_start:sample_stop].copy_(segment_waveform)
        if index > 1 and sample_start > 0:
            try:
                audio_boundary = measure_decoded_audio_boundary(
                    audio_buffer[..., :sample_start],
                    audio_buffer[..., sample_start:sample_stop],
                    sample_rate=sample_rate,
                )
                LOG.info(
                    "H3C-PT213 decoded-audio-boundary receipt "
                    "stage=post_seam boundary_global_frame=%d "
                    "window_samples=%d window_seconds=%.6f sample_rate=%d "
                    "previous_rms=%.9f current_rms=%.9f "
                    "current_over_previous_rms_ratio=%.6f current_over_previous_db=%+.4f "
                    "previous_peak=%.9f current_peak=%.9f "
                    "previous_spectral_centroid_hz=%.3f current_spectral_centroid_hz=%.3f "
                    "spectral_centroid_delta_hz=%+.3f high_band_hz=%.1f "
                    "previous_high_band_fraction=%.9f current_high_band_fraction=%.9f "
                    "high_band_fraction_delta=%+.9f audio_seam=%s",
                    frame_cursor,
                    int(audio_boundary["window_samples"]),
                    float(audio_boundary["window_seconds"]),
                    int(audio_boundary["sample_rate"]),
                    float(audio_boundary["previous_rms"]),
                    float(audio_boundary["current_rms"]),
                    float(audio_boundary["current_over_previous_rms_ratio"]),
                    float(audio_boundary["current_over_previous_db"]),
                    float(audio_boundary["previous_peak"]),
                    float(audio_boundary["current_peak"]),
                    float(audio_boundary["previous_spectral_centroid_hz"]),
                    float(audio_boundary["current_spectral_centroid_hz"]),
                    float(audio_boundary["spectral_centroid_delta_hz"]),
                    float(audio_boundary["high_band_hz"]),
                    float(audio_boundary["previous_high_band_fraction"]),
                    float(audio_boundary["current_high_band_fraction"]),
                    float(audio_boundary["high_band_fraction_delta"]),
                    audio_seam,
                )
                if diagnostics_mode == DIAGNOSTICS_FULL:
                    reports.append(
                        "decoded audio boundary "
                        f"{index-1}->{index} post-seam: "
                        f"level={audio_boundary['current_over_previous_db']:+.3f} dB, "
                        f"centroid_delta={audio_boundary['spectral_centroid_delta_hz']:+.1f} Hz, "
                        f"high_band_delta={audio_boundary['high_band_fraction_delta']:+.6f}"
                    )
            except Exception as exc:
                LOG.warning(
                    "H3C-PT213 decoded-audio-boundary unavailable "
                    "stage=post_seam boundary_global_frame=%d reason=%s: %s",
                    frame_cursor,
                    type(exc).__name__,
                    exc,
                )

        if diagnostics_mode != DIAGNOSTICS_OFF:
            reports.append(
                f"assembled decoded chunk {index}: {net_frames} retained frames, "
                f"cumulative {frame_stop}/{total_retained_frames}"
            )
            if seam_report:
                reports.append(seam_report)
        frame_cursor = frame_stop

    if image_buffer is None or audio_buffer is None or audio_rate is None:
        raise RuntimeError("V3 assembler failed to allocate output buffers")
    if frame_cursor != total_retained_frames:
        raise RuntimeError(
            f"V3 assembly cursor mismatch: {frame_cursor} != {total_retained_frames}"
        )

    result_images = image_buffer[:total_retained_frames]
    result_audio = {
        "waveform": audio_buffer.contiguous(),
        "sample_rate": audio_rate,
    }
    duration_report = ""
    if exact_total_duration:
        preserve_final_frame = bool(plan.get("preserve_final_frame"))
        result_images = _enforce_image_duration_in_place(
            image_buffer,
            current_frames=total_retained_frames,
            target_frames=target_frames,
            preserve_final_frame=preserve_final_frame,
        )
        # Reuse the existing, validated audio exact-duration policy with a tiny
        # placeholder image tensor. This keeps identical audio trim/pad semantics
        # without making enforce_total_frames allocate another full video tensor.
        duration_probe = torch.empty(
            (total_retained_frames, 1, 1, 1),
            dtype=torch.uint8,
            device="cpu",
        )
        _, result_audio, duration_report = enforce_total_frames(
            duration_probe,
            result_audio,
            target_frames=target_frames,
            preserve_final_frame=preserve_final_frame,
        )
        del duration_probe

    reports.append(
        f"Assembled {len(chunk_plans)} externally decoded chunks into "
        f"{result_images.shape[0]} frames with cumulative sample-boundary alignment."
    )
    if duration_report:
        reports.append(duration_report)
    runtime_started_at = plan.get("_runtime_started_at")
    if isinstance(runtime_started_at, (int, float)):
        elapsed = time.perf_counter() - float(runtime_started_at)
        if math.isfinite(elapsed) and elapsed >= 0.0:
            reports.append(f"Total workflow elapsed: {_format_elapsed(elapsed)}")
    return result_images.contiguous(), result_audio, "\n".join(reports)


def finalize_assembled_timeline(
    *,
    images: torch.Tensor,
    audio: dict[str, Any],
    assembly_plan: dict[str, Any],
):
    """Apply Continuum's exact-duration policy after downstream image processing.

    The input video must still be the plan's natural retained timeline. This is
    the public post-processing counterpart to ``exact_total_duration=False`` on
    the assembler and reuses the same final-frame/audio policy as normal assembly.
    """

    plan = validate_assembly_plan(assembly_plan)
    if not torch.is_tensor(images) or images.ndim != 4:
        raise ValueError("finalizer images must be IMAGE [frames,H,W,C]")
    groups = plan.get("decode_groups", plan["chunks"])
    natural_frames = sum(int(item["net_frames"]) for item in groups)
    if int(images.shape[0]) != natural_frames:
        raise ValueError(
            "H3 Continuum exact-duration finalizer requires the natural retained "
            f"timeline: images={int(images.shape[0])}, plan={natural_frames}"
        )

    waveform, sample_rate = validate_audio(audio)
    normalized_audio = {
        "waveform": waveform.detach().to("cpu").contiguous(),
        "sample_rate": int(sample_rate),
    }
    result_images, result_audio, duration_report = enforce_total_frames(
        images,
        normalized_audio,
        target_frames=int(plan["target_frames"]),
        preserve_final_frame=bool(plan.get("preserve_final_frame", False)),
    )
    report = (
        "H3 Continuum Finalize Duration V3.4: "
        f"natural={natural_frames}, target={int(plan['target_frames'])}.\n"
        f"{duration_report}"
    )
    return result_images, result_audio, report


class H3ContinuumAssembleV3:
    DESCRIPTION = (
        "Assemble full AV chunks decoded by ComfyUI Core. Trims decoded context, "
        "aligns proven exact-continuation audio to the physical 24-fps/40-Hz timeline, "
        "and optionally applies Audio Seam Auto."
    )
    SEARCH_ALIASES = ["H3 latent assemble", "H3 external VAE decode"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "audio": ("AUDIO",),
                "assembly_plan": ("H3_CONTINUUM_ASSEMBLY_PLAN",),
                "exact_total_duration": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "Adjust the final output to the exact requested total duration.",
                    },
                ),
                "audio_seam": (
                    AUDIO_SEAM_OPTIONS,
                    {
                        "default": AUDIO_SEAM_AUTO,
                        "display_name": "Audio Seam",
                        "tooltip": (
                            "Auto corrects only decoded audio boundaries; "
                            "video frames are never altered."
                        ),
                    },
                ),
                "diagnostics": (
                    DIAGNOSTICS_OPTIONS,
                    {
                        "default": DIAGNOSTICS_OPTIONS[0],
                        "display_name": "Report Detail",
                    },
                ),
                "image_output_device": (
                    IMAGE_OUTPUT_DEVICE_OPTIONS,
                    {
                        "default": IMAGE_OUTPUT_AUTO,
                        "display_name": "Image Output Device",
                        "tooltip": (
                            "Auto keeps the assembled IMAGE in VRAM when the full output plus "
                            "conservative headroom fits. CUDA is useful after high-resolution "
                            "latent upscaling because it avoids a second full-video CPU buffer."
                        ),
                    },
                ),
            }
        }

    INPUT_IS_LIST = True
    RETURN_TYPES = ("IMAGE", "AUDIO", "STRING")
    RETURN_NAMES = ("images", "audio", "report")
    FUNCTION = "assemble"
    CATEGORY = "MiniMax H3/Continuum"

    def assemble(
        self,
        images,
        audio,
        assembly_plan,
        exact_total_duration,
        audio_seam,
        diagnostics,
        image_output_device=IMAGE_OUTPUT_AUTO,
    ):
        return assemble_decoded_chunks(
            images=_chunk_list(images),
            audio=_chunk_list(audio),
            assembly_plan=_singleton(assembly_plan, "assembly_plan"),
            exact_total_duration=bool(
                _singleton(exact_total_duration, "exact_total_duration")
            ),
            audio_seam=str(_singleton(audio_seam, "audio_seam")),
            diagnostics=str(_singleton(diagnostics, "diagnostics")),
            image_output_device=str(
                _singleton(image_output_device, "image_output_device")
            ),
        )


# V3.0.1 hardening integration: preflight before allocation; Detailed Report only.
from ..hardening import assemble_with_hardening as _assemble_with_hardening

_assemble_decoded_chunks_v300 = assemble_decoded_chunks


def assemble_decoded_chunks(*args, **kwargs):
    return _assemble_with_hardening(_assemble_decoded_chunks_v300, args, kwargs)


class H3ContinuumAssembleSeamExperimental(H3ContinuumAssembleV3):
    DEPRECATED = False
    CATEGORY = "MiniMax H3/Continuum"
    DESCRIPTION = (
        "Analyze decoded chunk boundaries and apply guarded video seam correction."
    )
    SEARCH_ALIASES = ["H3 seam analysis", "H3 boundary flicker analysis"]

    @classmethod
    def INPUT_TYPES(cls):
        schema = super().INPUT_TYPES()
        required = dict(schema["required"])
        diagnostics = required.pop("diagnostics")
        image_output_device = required.pop("image_output_device")
        required["video_seam"] = (
            VIDEO_SEAM_ANALYSIS_OPTIONS,
            {
                "default": VIDEO_SEAM_AUTO,
                "display_name": "Video Seam",
                "tooltip": (
                    "Analyze Only reports decoded video boundaries without changing them. "
                    "Auto applies the validated transient and micro-flash correction. "
                    "Auto 2 experimentally adds qualified exposure-ramp smoothing."
                ),
            },
        )
        required["diagnostics"] = diagnostics
        required["image_output_device"] = image_output_device
        schema["required"] = required
        return schema

    def assemble(
        self,
        images,
        audio,
        assembly_plan,
        exact_total_duration,
        audio_seam,
        video_seam,
        diagnostics,
        image_output_device=IMAGE_OUTPUT_AUTO,
    ):
        mode = str(_singleton(video_seam, "video_seam"))
        if mode not in VIDEO_SEAM_ANALYSIS_OPTIONS:
            raise ValueError(f"unknown Video Seam mode: {mode!r}")
        if mode == VIDEO_SEAM_OFF:
            return super().assemble(
                images,
                audio,
                assembly_plan,
                exact_total_duration,
                audio_seam,
                diagnostics,
                image_output_device=image_output_device,
            )

        image_chunks = _chunk_list(images)
        plan = _singleton(assembly_plan, "assembly_plan")
        analyses = []
        analysis_error = None
        actions = {}
        video_patches = {}
        try:
            from .video_seam import (
                analyze_decoded_boundaries,
                build_decoded_boundary_patches,
                format_video_boundary_analysis,
            )

            analyses = analyze_decoded_boundaries(
                images=image_chunks,
                assembly_plan=plan,
            )
            if mode in (VIDEO_SEAM_AUTO, VIDEO_SEAM_AUTO_2):
                video_patches, actions = build_decoded_boundary_patches(
                    images=image_chunks,
                    assembly_plan=plan,
                    analyses=analyses,
                    enable_exposure_ramp=mode == VIDEO_SEAM_AUTO_2,
                )
        except Exception as exc:
            analysis_error = exc
            video_patches = {}

        result_images, result_audio, report = assemble_decoded_chunks(
            images=image_chunks,
            audio=_chunk_list(audio),
            assembly_plan=plan,
            exact_total_duration=bool(
                _singleton(exact_total_duration, "exact_total_duration")
            ),
            audio_seam=str(_singleton(audio_seam, "audio_seam")),
            diagnostics=str(_singleton(diagnostics, "diagnostics")),
            image_output_device=str(
                _singleton(image_output_device, "image_output_device")
            ),
            video_patches=video_patches,
        )
        if mode == VIDEO_SEAM_ANALYZE:
            status = "Video Seam: Analyze Only; decoded frames and audio are unchanged."
        elif mode == VIDEO_SEAM_AUTO:
            status = (
                "Video Seam: Auto; guarded transient and micro-flash correction enabled."
            )
        else:
            status = (
                "Video Seam: Auto 2 (Experimental); guarded transient, micro-flash, "
                "and exposure-ramp correction enabled."
            )
        report = report.replace("Video seam correction is disabled.", status, 1)
        if analysis_error is None:
            lines = [
                format_video_boundary_analysis(
                    item,
                    action=(
                        "analysis only"
                        if mode == VIDEO_SEAM_ANALYZE
                        else actions.get(item.boundary_index, "kept native boundary")
                    ),
                )
                for item in analyses
            ]
            if not lines:
                lines = [f"Video Seam {mode}: no decoded chunk boundary to analyze."]
        else:
            lines = [
                f"Video Seam {mode}: native output preserved; analysis unavailable "
                f"({type(analysis_error).__name__}: {analysis_error})"
            ]
        return result_images, result_audio, report.rstrip() + "\n" + "\n".join(lines)

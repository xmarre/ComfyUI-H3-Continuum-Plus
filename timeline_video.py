"""MiniMax H3 timeline-video reference conditioning.

Legacy nominal-chunk extraction remains the production default. The physical
window adapter is a separately gated experimental path because changing sampled
reference-video content requires its own matched decoded-media validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Any

import torch

from .constants import FPS
from .temporal import align_frame_count_up


TIMELINE_VIDEO_CONTRACT_VERSION = 1
TIMELINE_VIDEO_PREPROCESS_VERSION = 1
TIMELINE_VIDEO_PHYSICAL_SELECTION_VERSION = 1
TIMELINE_VIDEO_SIZE_EFFICIENT = "Efficient - 0.4 MP"
TIMELINE_VIDEO_SIZE_BALANCED = "Balanced - 0.6 MP"
TIMELINE_VIDEO_SIZE_MATCH_OUTPUT = "Match Output"
TIMELINE_VIDEO_SIZE_OPTIONS = (
    TIMELINE_VIDEO_SIZE_EFFICIENT,
    TIMELINE_VIDEO_SIZE_MATCH_OUTPUT,
)
_EFFICIENT_AREA = 400_000
_BALANCED_AREA = 600_000
_CANVAS_MULTIPLE = 32


class TimelineVideoError(ValueError):
    pass


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _fraction_string(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _tensor_hash(value: torch.Tensor) -> str:
    tensor = value.detach().to(device="cpu").contiguous()
    digest = hashlib.sha256()
    digest.update(str(tuple(int(v) for v in tensor.shape)).encode("ascii"))
    digest.update(str(tensor.dtype).encode("ascii"))
    digest.update(tensor.numpy().tobytes(order="C"))
    return digest.hexdigest()


def _source_hash(video: Any) -> tuple[str, int]:
    try:
        source = video.get_stream_source()
    except Exception as exc:
        raise TimelineVideoError(
            "Timeline Video must expose a stable ComfyUI VIDEO stream source"
        ) from exc
    digest = hashlib.sha256()
    size = 0
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.is_file():
            raise TimelineVideoError("Timeline Video source file is unavailable")
        with path.open("rb") as handle:
            while True:
                block = handle.read(4 * 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                size += len(block)
    elif isinstance(source, io.BytesIO):
        view = source.getbuffer()
        try:
            digest.update(view)
            size = len(view)
        finally:
            view.release()
    else:
        raise TimelineVideoError(
            "Timeline Video currently supports file-backed or in-memory Core VIDEO inputs"
        )
    return digest.hexdigest(), int(size)


def _round_canvas(value: float) -> int:
    return max(
        _CANVAS_MULTIPLE,
        int(round(float(value) / _CANVAS_MULTIPLE)) * _CANVAS_MULTIPLE,
    )


def _resolved_size(
    source_width: int,
    source_height: int,
    *,
    output_width: int,
    output_height: int,
    size_mode: str,
) -> tuple[int, int]:
    if size_mode == TIMELINE_VIDEO_SIZE_MATCH_OUTPUT:
        target_area = int(output_width) * int(output_height)
    elif size_mode == TIMELINE_VIDEO_SIZE_EFFICIENT:
        target_area = _EFFICIENT_AREA
    elif size_mode == TIMELINE_VIDEO_SIZE_BALANCED:
        target_area = _BALANCED_AREA
    else:
        raise TimelineVideoError(f"unknown Timeline Video Size: {size_mode!r}")
    source_area = int(source_width) * int(source_height)
    scale = min(1.0, math.sqrt(float(target_area) / float(source_area)))
    return (
        _round_canvas(int(source_width) * scale),
        _round_canvas(int(source_height) * scale),
    )


@dataclass(frozen=True)
class TimelineVideoSource:
    video: Any
    duration: float
    source_width: int
    source_height: int
    source_sha256: str
    source_bytes: int
    target_width: int
    target_height: int
    size_mode: str
    chunks: int
    chunk_seconds: float
    chunk_contracts: tuple[dict[str, Any], ...]
    combined_hash: str

    @property
    def contract(self) -> dict[str, Any]:
        return {
            "timeline_video_contract_version": TIMELINE_VIDEO_CONTRACT_VERSION,
            "source_sha256": self.source_sha256,
            "source_bytes": self.source_bytes,
            "source_duration": round(self.duration, 6),
            "source_width": self.source_width,
            "source_height": self.source_height,
            "size_mode": self.size_mode,
            "target_width": self.target_width,
            "target_height": self.target_height,
            "target_fps": FPS,
            "preprocess_version": TIMELINE_VIDEO_PREPROCESS_VERSION,
            "combined_hash": self.combined_hash,
            "chunk_slices": [dict(item) for item in self.chunk_contracts],
        }


@dataclass(frozen=True)
class TimelineVideoAssets:
    item: dict[str, Any]
    block: dict[str, Any]
    processed_sha256: str
    frame_count: int
    selection_contract: dict[str, Any] | None = None


def prepare_timeline_video_source(
    video: Any,
    *,
    chunks: int,
    chunk_seconds: float,
    output_width: int,
    output_height: int,
    size_mode: str,
) -> TimelineVideoSource:
    if video is None:
        raise TimelineVideoError("Timeline Video is required")
    for method in ("get_duration", "get_dimensions", "get_stream_source", "as_trimmed"):
        if not callable(getattr(video, method, None)):
            raise TimelineVideoError(
                f"Timeline Video is not a compatible ComfyUI VIDEO input: missing {method}()"
            )
    chunks = int(chunks)
    chunk_seconds = float(chunk_seconds)
    duration = float(video.get_duration())
    required_duration = chunks * chunk_seconds
    if duration + (1.0 / FPS) < required_duration:
        raise TimelineVideoError(
            f"Timeline Video is {duration:.3f}s, but {required_duration:.3f}s is required "
            f"for {chunks} chunk(s); looping and padding are not enabled"
        )
    source_width, source_height = (int(value) for value in video.get_dimensions())
    if source_width < 1 or source_height < 1:
        raise TimelineVideoError("Timeline Video dimensions are invalid")
    source_sha256, source_bytes = _source_hash(video)
    target_width, target_height = _resolved_size(
        source_width,
        source_height,
        output_width=int(output_width),
        output_height=int(output_height),
        size_mode=str(size_mode),
    )
    chunk_contracts = []
    for index in range(chunks):
        item = {
            "chunk_number": index + 1,
            "start_seconds": round(index * chunk_seconds, 6),
            "duration_seconds": round(chunk_seconds, 6),
            "target_width": target_width,
            "target_height": target_height,
            "target_fps": FPS,
            "preprocess_version": TIMELINE_VIDEO_PREPROCESS_VERSION,
        }
        item["slice_sha256"] = _canonical_hash(
            {"source_sha256": source_sha256, **item}
        )
        chunk_contracts.append(item)
    combined_hash = _canonical_hash(
        {
            "timeline_video_contract_version": TIMELINE_VIDEO_CONTRACT_VERSION,
            "source_sha256": source_sha256,
            "size_mode": str(size_mode),
            "target_width": target_width,
            "target_height": target_height,
            "chunk_slices": chunk_contracts,
        }
    )
    return TimelineVideoSource(
        video=video,
        duration=duration,
        source_width=source_width,
        source_height=source_height,
        source_sha256=source_sha256,
        source_bytes=source_bytes,
        target_width=target_width,
        target_height=target_height,
        size_mode=str(size_mode),
        chunks=chunks,
        chunk_seconds=chunk_seconds,
        chunk_contracts=tuple(chunk_contracts),
        combined_hash=combined_hash,
    )


def combine_timeline_video_identity(
    visual_identity_hash: str,
    source: TimelineVideoSource | None,
) -> str:
    if source is None:
        return str(visual_identity_hash)
    return _canonical_hash(
        {
            "timeline_video_identity_version": 1,
            "visual_identity_hash": str(visual_identity_hash),
            "timeline_video_hash": source.combined_hash,
        }
    )


def validate_timeline_video_prompts(
    prompts: list[str], source: TimelineVideoSource | None
) -> str:
    if source is None:
        return ""
    if any("<Video 1>" in str(prompt) for prompt in prompts):
        return ""
    return (
        "H3C-P103 Warning: Timeline Video is connected but the prompt contains no "
        "<Video 1> tag; video still conditions generation, but an explicit motion "
        "reference is recommended."
    )


def _resize_frames(frames: torch.Tensor, source: TimelineVideoSource) -> torch.Tensor:
    if (
        int(frames.shape[2]) == source.target_width
        and int(frames.shape[1]) == source.target_height
    ):
        return frames.contiguous()
    try:
        import comfy.utils
    except Exception as exc:
        raise TimelineVideoError(
            "ComfyUI Core image resize support is unavailable"
        ) from exc
    return comfy.utils.common_upscale(
        frames.movedim(-1, 1),
        source.target_width,
        source.target_height,
        "lanczos",
        "disabled",
    ).movedim(1, -1).contiguous()


def _encode_assets(
    video_vae: Any,
    source: TimelineVideoSource,
    frames: torch.Tensor,
    *,
    timestamps: list[float] | None = None,
    selection_contract: dict[str, Any] | None = None,
) -> TimelineVideoAssets:
    frames = _resize_frames(frames, source)
    processed_sha256 = _tensor_hash(frames)
    latent = video_vae.encode(frames)
    qwen_step = max(1, int(round(float(FPS) / 2.0)))
    qwen_indices = list(range(0, int(frames.shape[0]), qwen_step))
    qwen_frames = frames[qwen_indices].contiguous()
    if timestamps is None:
        qwen_timestamps = [index / float(FPS) for index in qwen_indices]
    else:
        qwen_timestamps = [float(timestamps[index]) for index in qwen_indices]
    item = {
        "type": "video",
        "data": qwen_frames,
        "timestamps": qwen_timestamps,
    }
    block = {
        "kind": "video",
        "latent_t": int(latent.shape[2]),
        "latent_h": source.target_height // 16,
        "latent_w": source.target_width // 16,
        "ref_audio_t": 0,
        "latent": latent,
        "audio_latent": None,
    }
    return TimelineVideoAssets(
        item=item,
        block=block,
        processed_sha256=processed_sha256,
        frame_count=int(frames.shape[0]),
        selection_contract=dict(selection_contract) if selection_contract is not None else None,
    )


def encode_timeline_video_chunk(
    video_vae: Any,
    source: TimelineVideoSource,
    chunk_index: int,
) -> TimelineVideoAssets:
    """Legacy nominal extraction. Keep this path available unchanged in meaning."""

    chunk_index = int(chunk_index)
    if chunk_index < 0 or chunk_index >= source.chunks:
        raise TimelineVideoError("Timeline Video chunk index is out of range")
    start = chunk_index * source.chunk_seconds
    trimmed = source.video.as_trimmed(
        start_time=start,
        duration=source.chunk_seconds,
        strict_duration=False,
    )
    if trimmed is None:
        raise TimelineVideoError(
            f"Timeline Video could not provide chunk {chunk_index + 1}"
        )
    components = trimmed.get_components()
    frames = getattr(components, "images", None)
    if not torch.is_tensor(frames) or frames.ndim != 4:
        raise TimelineVideoError("Timeline Video produced invalid IMAGE frames")
    if int(frames.shape[0]) < 5 or int(frames.shape[-1]) < 3:
        raise TimelineVideoError(
            f"Timeline Video chunk {chunk_index + 1} needs at least 5 RGB frames"
        )
    frames = frames[:, :, :, :3].detach().to(device="cpu", dtype=torch.float32)
    if not bool(torch.isfinite(frames).all()):
        raise TimelineVideoError("Timeline Video contains NaN or Inf pixels")
    target_frames = align_frame_count_up(int(round(source.chunk_seconds * FPS)))
    indices = torch.linspace(
        0, int(frames.shape[0]) - 1, target_frames, dtype=torch.float64
    ).round().to(dtype=torch.long)
    frames = frames.index_select(0, indices).contiguous()
    return _encode_assets(video_vae, source, frames)


def timeline_video_physical_selection_contract(
    source: TimelineVideoSource,
    descriptor: Any,
) -> dict[str, Any]:
    """Pure physical frame-edge selection identity; no media decode is performed."""

    fps = Fraction(int(descriptor.fps_numerator), int(descriptor.fps_denominator))
    start_frame = int(descriptor.global_start_frame)
    frame_count = int(descriptor.total_frames)
    requested = [Fraction(start_frame + index, 1) / fps for index in range(frame_count)]
    # Core VIDEO exposes duration but not a stable source-frame timestamp index.
    # The adapter therefore samples the requested 24-fps grid from a trimmed
    # decoded window and clamps source times at its available endpoints.
    upper = max(0.0, float(source.duration) - 1e-9)
    clamped = [min(max(float(value), 0.0), upper) for value in requested]
    contract = {
        "selection_version": TIMELINE_VIDEO_PHYSICAL_SELECTION_VERSION,
        "source_sha256": source.source_sha256,
        "global_start_frame": start_frame,
        "global_end_frame": start_frame + frame_count,
        "frame_count": frame_count,
        "fps": [int(descriptor.fps_numerator), int(descriptor.fps_denominator)],
        "requested_first": _fraction_string(requested[0]) if requested else "0",
        "requested_last": _fraction_string(requested[-1]) if requested else "0",
        "leading_clamped_frames": sum(1 for value in requested if value < 0),
        "trailing_clamped_frames": sum(1 for value in requested if float(value) > upper),
        "target_width": source.target_width,
        "target_height": source.target_height,
        "preprocess_version": TIMELINE_VIDEO_PREPROCESS_VERSION,
    }
    contract["selection_sha256"] = _canonical_hash(contract)
    return contract


def encode_timeline_video_physical(
    video_vae: Any,
    source: TimelineVideoSource,
    descriptor: Any,
) -> TimelineVideoAssets:
    """Experimental descriptor-aware Timeline Video extraction.

    This function is intentionally separate from ``encode_timeline_video_chunk``;
    callers must explicitly select it after the Timeline Video matched gate.
    """

    contract = timeline_video_physical_selection_contract(source, descriptor)
    fps = Fraction(int(descriptor.fps_numerator), int(descriptor.fps_denominator))
    start_frame = int(descriptor.global_start_frame)
    frame_count = int(descriptor.total_frames)
    requested = [float(Fraction(start_frame + index, 1) / fps) for index in range(frame_count)]
    upper = max(0.0, float(source.duration) - 1e-9)
    clipped = [min(max(value, 0.0), upper) for value in requested]
    if not clipped:
        raise TimelineVideoError("physical Timeline Video selection is empty")
    decode_start = min(clipped)
    decode_end = max(clipped)
    decode_duration = max(1.0 / float(FPS), decode_end - decode_start + 1.0 / float(FPS))
    trimmed = source.video.as_trimmed(
        start_time=decode_start,
        duration=decode_duration,
        strict_duration=False,
    )
    if trimmed is None:
        raise TimelineVideoError("Timeline Video could not provide the physical source window")
    components = trimmed.get_components()
    decoded = getattr(components, "images", None)
    if not torch.is_tensor(decoded) or decoded.ndim != 4 or int(decoded.shape[0]) < 1 or int(decoded.shape[-1]) < 3:
        raise TimelineVideoError("Timeline Video produced invalid IMAGE frames for the physical source window")
    decoded = decoded[:, :, :, :3].detach().to(device="cpu", dtype=torch.float32)
    if not bool(torch.isfinite(decoded).all()):
        raise TimelineVideoError("Timeline Video contains NaN or Inf pixels")
    if int(decoded.shape[0]) == 1 or decode_end <= decode_start:
        indices = torch.zeros(frame_count, dtype=torch.long)
    else:
        positions = [
            (value - decode_start) / (decode_end - decode_start) * (int(decoded.shape[0]) - 1)
            for value in clipped
        ]
        indices = torch.tensor(positions, dtype=torch.float64).round().to(dtype=torch.long)
        indices.clamp_(0, int(decoded.shape[0]) - 1)
    frames = decoded.index_select(0, indices).contiguous()
    local_timestamps = [index / float(FPS) for index in range(frame_count)]
    return _encode_assets(
        video_vae,
        source,
        frames,
        timestamps=local_timestamps,
        selection_contract=contract,
    )

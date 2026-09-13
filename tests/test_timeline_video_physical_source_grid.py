from __future__ import annotations

import io
import math
from types import SimpleNamespace

import torch

from ComfyUI_H3_Continuum_Join.timeline_video import (
    TIMELINE_VIDEO_SIZE_MATCH_OUTPUT,
    encode_timeline_video_physical,
    prepare_timeline_video_source,
    timeline_video_physical_selection_contract,
)
from ComfyUI_H3_Continuum_Join.v2.physical_sequence import (
    ResolvedInvocationGeometry,
    make_normal_descriptor,
)


class _Trimmed:
    def __init__(self, frames, frame_rate):
        self.frames = frames
        self.frame_rate = frame_rate

    def get_components(self):
        return SimpleNamespace(
            images=self.frames,
            audio=None,
            frame_rate=self.frame_rate,
        )


class _CoreLikeVideo:
    def __init__(self, *, frame_rate=30.0, duration=10.0, active_start=0.0):
        self.frame_rate = float(frame_rate)
        self.duration = float(duration)
        self.active_start = float(active_start)
        self.width = 32
        self.height = 32
        frame_count = int(math.ceil(self.duration * self.frame_rate))
        values = torch.arange(frame_count, dtype=torch.float32).view(-1, 1, 1, 1)
        self.frames = values.expand(-1, self.height, self.width, 3).contiguous()
        self.calls = []
        self.stream = io.BytesIO(b"timeline-video-physical-source-grid")

    def get_duration(self):
        return self.duration

    def get_dimensions(self):
        return self.width, self.height

    def get_stream_source(self):
        return self.stream

    def get_frame_rate(self):
        return self.frame_rate

    def get_active_trim_window(self):
        return self.active_start, self.duration

    def as_trimmed(self, start_time=0, duration=0, strict_duration=True):
        self.calls.append((float(start_time), float(duration), bool(strict_duration)))
        start_index = int(round(float(start_time) * self.frame_rate))
        count = max(1, int(round(float(duration) * self.frame_rate)))
        frames = self.frames[start_index : start_index + count]
        if int(frames.shape[0]) < 1:
            frames = self.frames[-1:]
        return _Trimmed(frames, self.frame_rate)


class _VAE:
    def __init__(self):
        self.calls = []

    def encode(self, frames):
        self.calls.append(frames.detach().clone())
        return torch.zeros((1, 24, 8, 2, 2))


def _source(video):
    return prepare_timeline_video_source(
        video,
        chunks=2,
        chunk_seconds=5.0,
        output_width=32,
        output_height=32,
        size_mode=TIMELINE_VIDEO_SIZE_MATCH_OUTPUT,
    )


def _geometry():
    return ResolvedInvocationGeometry(
        sequence_index=1,
        is_final=True,
        continuation=True,
        clip_index=2,
        context_frames=22,
        total_frames=143,
        net_frames=121,
        motion_score=0.0,
        reason="test",
        plan={},
    )


def _assets():
    return SimpleNamespace(
        first_image=None,
        last_image=None,
        first_frame_hash="none",
        last_frame_hash="none",
    )


def test_descriptor_to_encode_consumes_one_source_scoped_prepared_window(monkeypatch):
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "1")
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO", "1")
    video = _CoreLikeVideo(frame_rate=30.0)
    source = _source(video)

    descriptor = make_normal_descriptor(
        geometry=_geometry(),
        retained_before=120,
        target_duration_frames=240,
        continuation_method="Guide",
        initial_state_external=False,
        assets=_assets(),
        include_first=False,
        include_last=False,
        timeline_video_source=source,
    )
    assert len(video.calls) == 1

    vae = _VAE()
    encoded = encode_timeline_video_physical(vae, source, descriptor)

    assert len(video.calls) == 1
    assert len(vae.calls) == 1
    presentation = descriptor.presentation_contract["video"]
    assert encoded.processed_sha256 == presentation["processed_sha256"]
    assert encoded.selection_contract == presentation["selection_contract"]
    assert source._physical_prepared_cache == {}


def test_core_frame_rate_aligns_physical_trim_to_source_grid(monkeypatch):
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "1")
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO", "1")
    video = _CoreLikeVideo(frame_rate=30.0)
    source = _source(video)

    descriptor = make_normal_descriptor(
        geometry=_geometry(),
        retained_before=120,
        target_duration_frames=240,
        continuation_method="Guide",
        initial_state_external=False,
        assets=_assets(),
        include_first=False,
        include_last=False,
        timeline_video_source=source,
    )

    start_time = video.calls[0][0]
    assert abs(start_time * 30.0 - round(start_time * 30.0)) < 1e-9
    selection = descriptor.presentation_contract["video"]["selection_contract"]
    assert selection["source_frame_rate"] == [30, 1]
    assert selection["decoded_frame_rate"] == [30, 1]
    assert len(selection["sampled_source_indices_sha256"]) == 64
    assert len(selection["resolved_selection_sha256"]) == 64


def test_active_trim_provenance_changes_only_physical_selection_identity():
    first = _source(_CoreLikeVideo(active_start=0.0))
    second = _source(_CoreLikeVideo(active_start=2.0))

    # Legacy source identity intentionally remains based on the same source bytes
    # and nominal chunk contract. Physical selection adds the active trim.
    assert first.combined_hash == second.combined_hash
    first_contract = timeline_video_physical_selection_contract(
        first, global_start_frame=98, total_frames=143
    )
    second_contract = timeline_video_physical_selection_contract(
        second, global_start_frame=98, total_frames=143
    )
    assert first_contract["source_active_trim"] == ["0", "10"]
    assert second_contract["source_active_trim"] == ["2", "10"]
    assert first_contract["selection_sha256"] != second_contract["selection_sha256"]


def test_physical_descriptor_persists_trailing_clamp_diagnostic(monkeypatch):
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "1")
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO", "1")
    source = _source(_CoreLikeVideo(frame_rate=30.0, duration=10.0))

    descriptor = make_normal_descriptor(
        geometry=_geometry(),
        retained_before=120,
        target_duration_frames=240,
        continuation_method="Guide",
        initial_state_external=False,
        assets=_assets(),
        include_first=False,
        include_last=False,
        timeline_video_source=source,
    )

    video_presentation = descriptor.presentation_contract["video"]
    assert video_presentation["selection_contract"]["trailing_clamped_frames"] == 1
    assert any(
        item["code"] == "H3C-TV202"
        for item in video_presentation.get("diagnostics", [])
    )

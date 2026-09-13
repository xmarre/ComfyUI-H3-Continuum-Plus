from __future__ import annotations

import io
import math
from types import SimpleNamespace

import torch

from ComfyUI_H3_Continuum_Join.timeline_video import (
    TIMELINE_VIDEO_SIZE_MATCH_OUTPUT,
    prepare_timeline_video_physical_frames,
    prepare_timeline_video_source,
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


class _TrimmedCoreVideo:
    def __init__(self, *, active_start=0.02, frame_rate=30.0, duration=10.0):
        self.active_start = float(active_start)
        self.frame_rate = float(frame_rate)
        self.duration = float(duration)
        self.stream = io.BytesIO(b"trimmed-core-video-phase")
        self.calls = []
        count = int(math.ceil(self.duration * self.frame_rate))
        self.frames = torch.zeros((count, 32, 32, 3), dtype=torch.float32)

    def get_duration(self):
        return self.duration

    def get_dimensions(self):
        return 32, 32

    def get_stream_source(self):
        return self.stream

    def get_frame_rate(self):
        return self.frame_rate

    def get_active_trim_window(self):
        return self.active_start, self.duration

    def as_trimmed(self, start_time=0, duration=0, strict_duration=True):
        self.calls.append((float(start_time), float(duration), bool(strict_duration)))
        start_index = max(0, int(round(float(start_time) * self.frame_rate)))
        count = max(1, int(round(float(duration) * self.frame_rate)))
        frames = self.frames[start_index : start_index + count]
        if int(frames.shape[0]) < 1:
            frames = self.frames[-1:]
        return _Trimmed(frames, self.frame_rate)


class _OpenEndedTrimmedCoreVideo(_TrimmedCoreVideo):
    def get_active_trim_window(self):
        # Match Core VideoFromFile: duration 0 means "until end" and does not
        # erase a non-zero upstream trim start.
        return self.active_start, 0.0


def _prepare(video):
    return prepare_timeline_video_source(
        video,
        chunks=2,
        chunk_seconds=5.0,
        output_width=32,
        output_height=32,
        size_mode=TIMELINE_VIDEO_SIZE_MATCH_OUTPUT,
    )


def test_physical_trim_preserves_upstream_active_trim_source_grid_phase():
    video = _TrimmedCoreVideo(active_start=0.02, frame_rate=30.0)
    source = _prepare(video)

    prepared = prepare_timeline_video_physical_frames(
        source,
        global_start_frame=98,
        total_frames=143,
    )

    relative_start = video.calls[0][0]
    absolute_grid_position = (video.active_start + relative_start) * video.frame_rate
    assert abs(absolute_grid_position - round(absolute_grid_position)) < 1e-9
    assert prepared.selection_contract["source_active_trim"] == ["1/50", "10"]
    assert prepared.selection_contract["source_grid_start"] == "1/50"


def test_open_ended_core_trim_keeps_nonzero_source_grid_phase():
    video = _OpenEndedTrimmedCoreVideo(active_start=0.02, frame_rate=30.0)
    source = _prepare(video)

    prepared = prepare_timeline_video_physical_frames(
        source,
        global_start_frame=98,
        total_frames=143,
    )

    relative_start = video.calls[0][0]
    absolute_grid_position = (video.active_start + relative_start) * video.frame_rate
    assert abs(absolute_grid_position - round(absolute_grid_position)) < 1e-9
    assert prepared.selection_contract["source_active_trim"] == ["1/50", "0"]
    assert prepared.selection_contract["source_grid_start"] == "1/50"

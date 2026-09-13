from __future__ import annotations

from dataclasses import replace
import io
from types import SimpleNamespace

import pytest
import torch

from ComfyUI_H3_Continuum_Join.timeline_video import (
    TIMELINE_VIDEO_SIZE_BALANCED,
    TIMELINE_VIDEO_SIZE_EFFICIENT,
    TIMELINE_VIDEO_SIZE_MATCH_OUTPUT,
    TimelineVideoAssets,
    combine_timeline_video_identity,
    encode_timeline_video_chunk,
    encode_timeline_video_prepared,
    prepare_timeline_video_physical_frames,
    prepare_timeline_video_source,
    validate_timeline_video_prompts,
)
from ComfyUI_H3_Continuum_Join.v2.physical_prompts import (
    PhysicalPromptError,
    compile_legacy_nominal,
    physical_metadata,
    physical_metadata_matches,
)
from ComfyUI_H3_Continuum_Join.v2.physical_runtime import (
    _validate_physical_timeline_video_assets,
)
from ComfyUI_H3_Continuum_Join.v2.physical_sequence import (
    ResolvedInvocationGeometry,
    make_normal_descriptor,
)
from ComfyUI_H3_Continuum_Join.v3.nodes import (
    H3ContinuumSamplerProduction,
    H3ContinuumSamplerTimelineVideo,
)


class _Trimmed:
    def __init__(self, frames):
        self.frames = frames

    def get_components(self):
        return SimpleNamespace(images=self.frames, audio=None, frame_rate=24.0)


class _Video:
    def __init__(self, frames, duration=10.0, width=32, height=32):
        self.frames = frames
        self.duration = duration
        self.width = width
        self.height = height
        self.calls = []
        self.stream = io.BytesIO(b"timeline-video-test")

    def get_duration(self):
        return self.duration

    def get_dimensions(self):
        return self.width, self.height

    def get_stream_source(self):
        return self.stream

    def as_trimmed(self, start_time=0, duration=0, strict_duration=True):
        self.calls.append((start_time, duration, strict_duration))
        return _Trimmed(self.frames)


class _VAE:
    def __init__(self):
        self.calls = []

    def encode(self, frames):
        self.calls.append(frames)
        return torch.zeros((1, 24, 8, 2, 2))


def _source(size_mode=TIMELINE_VIDEO_SIZE_MATCH_OUTPUT):
    video = _Video(torch.zeros((120, 32, 32, 3)))
    source = prepare_timeline_video_source(
        video,
        chunks=2,
        chunk_seconds=5.0,
        output_width=32,
        output_height=32,
        size_mode=size_mode,
    )
    return video, source


def _continuation_geometry(*, total_frames=143, context_frames=22):
    return ResolvedInvocationGeometry(
        sequence_index=1,
        is_final=True,
        continuation=True,
        clip_index=2,
        context_frames=context_frames,
        total_frames=total_frames,
        net_frames=total_frames - context_frames,
        motion_score=0.0,
        reason="test",
        plan={},
    )


def _descriptor_assets():
    return SimpleNamespace(
        first_image=None,
        last_image=None,
        first_frame_hash="none",
        last_frame_hash="none",
    )


def test_v33_unifies_optional_timeline_video_and_keeps_v324_schema():
    legacy = H3ContinuumSamplerProduction.INPUT_TYPES()
    unified = H3ContinuumSamplerTimelineVideo.INPUT_TYPES()
    assert "timeline_video" not in legacy["required"]
    assert "timeline_video_size" not in legacy["required"]
    assert "timeline_video" not in unified["required"]
    assert unified["optional"]["timeline_video"][0] == "VIDEO"

    from ComfyUI_H3_Continuum_Join import nodes as root_nodes

    assert root_nodes.NODE_DISPLAY_NAME_MAPPINGS["H3ContinuumSamplerTimelineVideo"] == (
        "[Legacy] H3 Continuum Sampler V3.3"
    )
    assert root_nodes.NODE_DISPLAY_NAME_MAPPINGS["H3ContinuumSamplerProduction"] == (
        "[Legacy] H3 Continuum Sampler V3.2.4"
    )


def test_timeline_node_without_video_delegates_to_stable_engine(monkeypatch):
    calls = []

    def fake_run(self, **kwargs):
        calls.append(kwargs)
        return "stable"

    monkeypatch.setattr(H3ContinuumSamplerProduction, "run", fake_run)
    result = H3ContinuumSamplerTimelineVideo().run(
        timeline_video=None,
        timeline_video_size="Efficient - 0.4 MP",
        marker="no-video",
    )

    assert result == "stable"
    assert calls == [{"marker": "no-video"}]


def test_timeline_node_with_video_prepares_source(monkeypatch):
    video = object()
    source = object()
    prepared = []
    delegated = []

    def fake_prepare(value, **kwargs):
        prepared.append((value, kwargs))
        return source

    def fake_run(self, **kwargs):
        delegated.append(kwargs)
        return "timeline"

    monkeypatch.setattr(
        "ComfyUI_H3_Continuum_Join.timeline_video.prepare_timeline_video_source",
        fake_prepare,
    )
    monkeypatch.setattr(H3ContinuumSamplerProduction, "run", fake_run)
    result = H3ContinuumSamplerTimelineVideo().run(
        timeline_video=video,
        timeline_video_size="Efficient - 0.4 MP",
        chunks=2,
        chunk_seconds=5.0,
        width=800,
        height=800,
    )

    assert result == "timeline"
    assert prepared == [
        (
            video,
            {
                "chunks": 2,
                "chunk_seconds": 5.0,
                "output_width": 800,
                "output_height": 800,
                "size_mode": "Efficient - 0.4 MP",
            },
        )
    ]
    assert delegated == [
        {
            "timeline_video_source": source,
            "chunks": 2,
            "chunk_seconds": 5.0,
            "width": 800,
            "height": 800,
        }
    ]


def test_timeline_contract_is_deterministic_and_chunked():
    _, first = _source()
    _, second = _source()
    assert first.contract == second.contract
    assert len(first.contract["chunk_slices"]) == 2
    assert first.contract["chunk_slices"][1]["start_seconds"] == 5.0


def test_efficient_mode_resolves_about_point_four_megapixels():
    video = _Video(torch.zeros((120, 1080, 1920, 3)), width=1920, height=1080)
    source = prepare_timeline_video_source(
        video,
        chunks=1,
        chunk_seconds=5.0,
        output_width=1344,
        output_height=768,
        size_mode=TIMELINE_VIDEO_SIZE_EFFICIENT,
    )
    assert 350_000 <= source.target_width * source.target_height <= 450_000


def test_balanced_mode_resolves_about_point_six_megapixels():
    video = _Video(torch.zeros((120, 1080, 1920, 3)), width=1920, height=1080)
    source = prepare_timeline_video_source(
        video,
        chunks=1,
        chunk_seconds=5.0,
        output_width=1344,
        output_height=768,
        size_mode=TIMELINE_VIDEO_SIZE_BALANCED,
    )
    assert 550_000 <= source.target_width * source.target_height <= 650_000


def test_encode_processes_only_requested_chunk_and_builds_core_payload():
    video, source = _source()
    vae = _VAE()
    assets = encode_timeline_video_chunk(vae, source, 1)
    assert video.calls == [(5.0, 5.0, False)]
    assert len(vae.calls) == 1
    assert int(vae.calls[0].shape[0]) % 17 == 5
    assert assets.item["type"] == "video"
    assert assets.block["kind"] == "video"
    assert assets.block["ref_audio_t"] == 0


def test_physical_descriptor_authenticates_resolved_window_before_conditioning(monkeypatch):
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "1")
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO", "1")
    video, source = _source()
    geometry = _continuation_geometry(total_frames=143, context_frames=22)

    descriptor = make_normal_descriptor(
        geometry=geometry,
        retained_before=120,
        target_duration_frames=240,
        continuation_method="Guide",
        initial_state_external=False,
        assets=_descriptor_assets(),
        include_first=False,
        include_last=False,
        timeline_video_source=source,
    )

    presentation = descriptor.presentation_contract["video"]
    selection = presentation["selection_contract"]
    assert descriptor.global_start_frame == 98
    assert selection["global_start_frame"] == 98
    assert selection["global_end_frame"] == 241
    assert selection["frame_count"] == 143
    assert selection["trailing_clamped_frames"] == 1
    assert len(presentation["processed_sha256"]) == 64
    assert presentation["processed_sha256"] == selection["processed_sha256"]
    assert video.calls[0][0] == pytest.approx(98 / 24)


def test_physical_timeline_video_gate_is_separate_and_legacy_descriptor_does_not_decode(monkeypatch):
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "1")
    monkeypatch.delenv("H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO", raising=False)
    video, source = _source()
    descriptor = make_normal_descriptor(
        geometry=_continuation_geometry(),
        retained_before=120,
        target_duration_frames=240,
        continuation_method="Guide",
        initial_state_external=False,
        assets=_descriptor_assets(),
        include_first=False,
        include_last=False,
        timeline_video_source=source,
    )

    presentation = descriptor.presentation_contract["video"]
    assert presentation["adapter"] == "legacy_nominal_chunk_v1"
    assert "processed_sha256" not in presentation
    assert video.calls == []


def test_prepared_physical_frames_are_exact_payload_given_to_vae():
    _, source = _source()
    prepared = prepare_timeline_video_physical_frames(
        source,
        global_start_frame=98,
        total_frames=143,
    )
    vae = _VAE()
    assets = encode_timeline_video_prepared(vae, source, prepared)

    assert len(vae.calls) == 1
    assert torch.equal(vae.calls[0], prepared.frames)
    assert assets.processed_sha256 == prepared.processed_sha256
    assert assets.selection_contract == prepared.selection_contract


def test_runtime_rejects_second_decode_that_differs_from_authenticated_presentation(monkeypatch):
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "1")
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO", "1")
    _, source = _source()
    descriptor = make_normal_descriptor(
        geometry=_continuation_geometry(),
        retained_before=120,
        target_duration_frames=240,
        continuation_method="Guide",
        initial_state_external=False,
        assets=_descriptor_assets(),
        include_first=False,
        include_last=False,
        timeline_video_source=source,
    )
    presentation = descriptor.presentation_contract["video"]
    bad = TimelineVideoAssets(
        item={},
        block={},
        processed_sha256="0" * 64,
        frame_count=presentation["frame_count"],
        selection_contract=dict(presentation["selection_contract"]),
    )

    with pytest.raises(PhysicalPromptError, match="processed presentation changed"):
        _validate_physical_timeline_video_assets(descriptor, bad)


def test_physical_reuse_identity_rejects_processed_timeline_video_change(monkeypatch):
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_PROMPTS", "1")
    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO", "1")
    _, source = _source()
    descriptor = make_normal_descriptor(
        geometry=_continuation_geometry(),
        retained_before=120,
        target_duration_frames=240,
        continuation_method="Guide",
        initial_state_external=False,
        assets=_descriptor_assets(),
        include_first=False,
        include_last=False,
        timeline_video_source=source,
    )
    plan = {"prompts": ["first", "second"]}
    compiled = compile_legacy_nominal(plan, descriptor, text="second")
    stored = physical_metadata(
        descriptor,
        compiled,
        timeline_video=descriptor.presentation_contract["video"],
    )

    changed_presentation = dict(descriptor.presentation_contract)
    changed_video = dict(changed_presentation["video"])
    changed_video["processed_sha256"] = "f" * 64
    changed_selection = dict(changed_video["selection_contract"])
    changed_selection["processed_sha256"] = "f" * 64
    changed_video["selection_contract"] = changed_selection
    changed_presentation["video"] = changed_video
    changed_descriptor = replace(descriptor, presentation_contract=changed_presentation)
    changed_compiled = compile_legacy_nominal(plan, changed_descriptor, text="second")
    expected = physical_metadata(
        changed_descriptor,
        changed_compiled,
        timeline_video=changed_video,
    )

    assert not physical_metadata_matches(stored, expected)


def test_timeline_identity_is_noop_when_absent_and_changes_when_present():
    _, source = _source()
    assert combine_timeline_video_identity("visual", None) == "visual"
    assert combine_timeline_video_identity("visual", source) != "visual"


def test_missing_video_tag_warns_without_stopping():
    _, source = _source()
    assert "H3C-P103" in validate_timeline_video_prompts(["A dancer moves."], source)
    assert validate_timeline_video_prompts(["Follow <Video 1>."], source) == ""

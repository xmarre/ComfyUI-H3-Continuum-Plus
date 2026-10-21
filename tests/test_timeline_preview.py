"""Short interactive previews cannot re-render a 30-second movie on Play."""

from fractions import Fraction
import numpy as np
import pytest

from ComfyUI_H3_Continuum_Join.timeline.media import (
    ProjectStore,
    audio_window,
    preview_audio_window,
    probe,
    video_frames,
)
from ComfyUI_H3_Continuum_Join.timeline.model import blank_project, validate
from ComfyUI_H3_Continuum_Join.timeline.preview import (
    MAX_SEGMENT_FRAMES,
    NEXT_SEGMENT_FRAMES,
    FIRST_SEGMENT_FRAMES,
    render_preview_segment,
    simple_source,
)
from tests.test_timeline_media import movie  # noqa: F401 - shared FFmpeg fixture


def project_with_original(movie, tmp_path):
    store = ProjectStore(tmp_path / "project")
    with movie.open("rb") as source:
        asset = store.assets.register(source, movie.name)
    p = blank_project()
    p.update(
        width=64, height=64, duration_frames=336,
        selection=[0, 336], assets=[asset],
        tracks=[
            {"id":"video", "role":"retained", "clips":[{
                "id":"source", "asset_id":asset["id"], "start":0, "end":336,
                "source_in":"0", "speed":"1", "padding":"error",
                "audio_role":"neither",
            }]},
            {"id":"audio", "role":"audio", "clips":[{
                "id":"sound", "asset_id":asset["id"], "start":0, "end":336,
                "source_in":"0", "speed":"1", "padding":"error",
                "audio_role":"final", "gain":"1",
            }]},
        ],
    )
    return validate(p), store


def test_two_second_preview_seeks_original_video_without_full_composition(movie, tmp_path):
    p, store = project_with_original(movie, tmp_path)
    assert FIRST_SEGMENT_FRAMES == 48
    assert NEXT_SEGMENT_FRAMES == 144
    assert MAX_SEGMENT_FRAMES == 192
    assert simple_source(p, 168, 216) is not None
    target = tmp_path / "play-window.mp4"
    assert render_preview_segment(p, store, target, 168, 216) == target
    assert probe(target)["video"]
    assert probe(target)["audio"]
    assert abs(float(probe(target)["duration"]) - 2) < 0.12
    expected = video_frames(movie, [Fraction(7)], 64, 64)[0].astype("int16")
    actual = video_frames(target, [Fraction(0)], 64, 64)[0].astype("int16")
    assert np.abs(expected - actual).mean() < 25


def test_preview_source_audio_seek_uses_only_window_not_two_hour_prefix(movie, tmp_path, monkeypatch):
    import ComfyUI_H3_Continuum_Join.timeline.media as media

    exact = audio_window(movie, "7", "8", 44100)
    original_run = media.run_process
    invocations = []

    def instrumented(args, *, cancel=media.check_cancel):
        invocations.append(args)
        assert "-seek_timestamp" in args
        assert "-ss" in args
        assert args.index("-ss") < args.index("-i")
        return original_run(args, cancel=cancel)

    monkeypatch.setattr(media, "run_process", instrumented)
    fast = preview_audio_window(movie, "7", "8", 44100)
    assert fast.shape == (2, 44100)
    assert np.sqrt(np.mean((fast - exact)**2)) < 0.05
    assert len(invocations) == 1


def test_complicated_audio_edits_never_use_source_passthrough(movie, tmp_path):
    p, _ = project_with_original(movie, tmp_path)
    p["tracks"][1]["clips"][0]["gain"] = "0.5"
    assert simple_source(p, 168, 216) is None
    p["tracks"][1]["clips"][0]["gain"] = "1"
    p["tracks"][0]["clips"][0]["speed"] = "-1"
    assert simple_source(p, 168, 216) is None


def test_preview_segments_are_bounded_and_do_not_publish_partial_output(movie, tmp_path):
    p, store = project_with_original(movie, tmp_path)
    with pytest.raises(ValueError, match="eight seconds"):
        render_preview_segment(p, store, tmp_path / "oversize.mp4", 0, 200)
    result = tmp_path / "cancelled.mp4"

    def cancelled():
        raise InterruptedError("preview cancelled")

    with pytest.raises(InterruptedError):
        render_preview_segment(p, store, result, 0, 48, cancel=cancelled)
    assert not result.exists()
    assert not list(tmp_path.glob("cancelled-*.mp4"))


def test_preview_audio_routes_preserve_exact_production_mode(movie, tmp_path):
    p, store = project_with_original(movie, tmp_path)
    output = tmp_path / "mix-preview.mp4"
    p["tracks"][1]["clips"][0]["gain"] = "0.75"
    assert simple_source(p, 0, 48) is None
    render_preview_segment(p, store, output, 0, 48)
    assert output.is_file()
    assert abs(float(probe(output)["duration"]) - 2) < 0.12


def test_hour_late_video_preview_uses_pre_input_seek_not_source_prefix(
    movie, tmp_path, monkeypatch
):
    import ComfyUI_H3_Continuum_Join.timeline.preview as preview

    p, store = project_with_original(movie, tmp_path)
    clip = p["tracks"][0]["clips"][0]
    clip["source_in"] = "3600"
    # No long movie is needed: assert the exact native FFmpeg invocation
    # without letting the fake one-hour source seek execute in CI.
    used = []

    def capture(args, *, cancel):
        used.append(args)

    monkeypatch.setattr(preview, "run_process", capture)
    preview._quick_source_segment(
        p, store, tmp_path / "not-encoded.mp4",
        0, 48, (clip, False), cancel=lambda: None,
    )
    assert len(used) == 1
    args = used[0]
    assert args[args.index("-ss") + 1] == "3600.0"
    assert args.index("-seek_timestamp") < args.index("-i")
    assert args.index("-ss") < args.index("-i")
    assert args[args.index("-t") + 1] == "2"
    assert args[args.index("-threads") + 1] == "1"

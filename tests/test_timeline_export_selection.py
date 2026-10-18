"""Regression for selecting 14 seconds inside a multi-hour timeline.

Neither the Electron export buttons nor the native Export output node may
silently produce the whole project when Render [in,out) is a short interval.
"""

from fractions import Fraction

import pytest

from ComfyUI_H3_Continuum_Join.timeline.compose import export_project
from ComfyUI_H3_Continuum_Join.timeline.media import ProjectStore, probe
from ComfyUI_H3_Continuum_Join.timeline.model import (
    blank_project, selected_export_range, validate,
    verify_export_clip_selection,
)
from tests.test_timeline_media import movie  # noqa: F401


def make_long_project(movie, tmp_path, *, selection=(0, 336)):
    store = ProjectStore(tmp_path / "studio")
    with movie.open("rb") as file:
        media = store.assets.register(file, "original.mkv")
    p = blank_project()
    # Imagine a nearly 2h source/timeline, but author only 14s here.
    p.update(
        width=64, height=64,
        duration_frames=159000,
        selection=list(selection),
        assets=[media],
    )
    video = {
        "asset_id": media["id"], "start": 0, "end": 336,
        "source_in": "0", "speed": "1", "padding": "error",
    }
    p["tracks"] = [
        {"id": "v", "role": "retained",
         "clips": [{"id": "c", **video, "audio_role": "neither"}]},
        {"id": "a", "role": "audio",
         "clips": [{"id": "s", **video, "audio_role": "final"}]},
    ]
    return validate(p), store


def test_http_export_requires_exact_explicit_selected_range():
    p = validate({
        **blank_project(),
        "duration_frames": 159000,
        "selection": [0, 336],
    })
    assert selected_export_range(p, [0, 336]) == (0, 336)
    with pytest.raises(ValueError, match="needs the selected"):
        selected_export_range(p, None)
    with pytest.raises(ValueError, match="differs"):
        selected_export_range(p, [0, 159000])
    with pytest.raises(ValueError, match="differs"):
        selected_export_range(p, [1, 336])
    with pytest.raises(ValueError, match="must be integers"):
        selected_export_range(p, [False, 336])
    with pytest.raises(ValueError, match="outside the project"):
        selected_export_range(p, [0, 159001])


def test_native_export_defaults_to_14s_selection_not_nearly_two_hour_project(
    movie, tmp_path, monkeypatch
):
    import ComfyUI_H3_Continuum_Join.timeline.media as media
    p, store = make_long_project(movie, tmp_path)
    decoded = []
    real = media._decode_audio_stream

    def trace(*args, **kwargs):
        decoded.append(kwargs.get("limit"))
        return real(*args, **kwargs)

    monkeypatch.setattr(media, "_decode_audio_stream", trace)
    output = tmp_path / "fourteen_seconds.mkv"
    updates = []
    export_project(
        p, store, output, codec="lossless",
        on_progress=lambda **u: updates.append(u),
    )
    assert output.is_file()
    info = probe(output)
    assert info["video"] and info["audio"]
    assert 13.9 < float(info["duration"]) < 14.1
    assert updates[-1]["total_frames"] == 336
    assert updates[-1]["frames_done"] == 336
    assert decoded == [14 * 44100], (
        "Selected 14s must cache only the first 14s of exact audio, "
        "not decode the full movie"
    )


def test_middle_range_uses_nonzero_frame_origin_and_only_rendered_frames(
    movie, tmp_path
):
    p, store = make_long_project(movie, tmp_path, selection=(168, 336))
    output = tmp_path / "middle_seven_seconds.mp4"
    export_project(
        p, store, output, codec="h264",
        render_range=(168, 336),
    )
    info = probe(output)
    assert 6.9 < float(info["duration"]) < 7.1
    assert info["video"]["width"] == 64
    assert info["video"]["height"] == 64
    # Timeline-in source 7s is encoded as output PTS zero.
    from ComfyUI_H3_Continuum_Join.timeline.media import video_frames
    import numpy as np

    expected = video_frames(movie, [Fraction(7)], 64, 64)[0].astype("int16")
    actual = video_frames(output, [Fraction(0)], 64, 64)[0].astype("int16")
    assert np.abs(expected - actual).mean() < 20


def test_highlighted_clip_id_guards_absolute_timeline_export_scope(movie, tmp_path):
    p, store = make_long_project(movie, tmp_path, selection=(17136, 17304))
    p["tracks"][0]["clips"][0].update(
        start=17136, end=17304, source_in="7"
    )
    p["tracks"][1]["clips"][0].update(
        start=17136, end=17304, source_in="7"
    )
    p = validate(p)
    verify_export_clip_selection(p, ["c"], 17136, 17304)
    verify_export_clip_selection(p, ["c", "s"], 17136, 17304)
    with pytest.raises(ValueError, match="differs"):
        verify_export_clip_selection(p, ["c"], 0, 168)
    with pytest.raises(ValueError, match="missing or unavailable"):
        verify_export_clip_selection(p, ["unknown"], 17136, 17304)
    from ComfyUI_H3_Continuum_Join.timeline.media import video_frames
    import numpy as np

    output = tmp_path / "highlighted_scene.mp4"
    export_project(
        p, store, output, codec="h264", render_range=(17136, 17304)
    )
    info = probe(output)
    assert info["video"] and info["audio"]
    assert 6.9 < float(info["duration"]) < 7.1
    original = video_frames(movie, [Fraction(7)], 64, 64)[0].astype("int16")
    exported = video_frames(output, [Fraction(0)], 64, 64)[0].astype("int16")
    assert np.abs(original - exported).mean() < 20


def test_source_resolution_export_preserves_native_pixels_independent_of_h3_canvas(
    movie, tmp_path
):
    import numpy as np
    from ComfyUI_H3_Continuum_Join.timeline.media import video_frames
    from ComfyUI_H3_Continuum_Join.timeline.compose import export_dimensions

    p, store = make_long_project(movie, tmp_path, selection=(0, 48))
    p["width"], p["height"] = 32, 32
    for track in p["tracks"]:
        track["clips"][0]["end"] = 48
    p = validate(p)

    assert export_dimensions(p, store, 0, 48, "timeline") == (32, 32)
    assert export_dimensions(p, store, 0, 48, "source", ["c"]) == (64, 64)
    with pytest.raises(ValueError, match="must be source or timeline"):
        export_dimensions(p, store, 0, 48, "unrecognized")

    original_file = tmp_path / "source_pixels.mkv"
    export_project(
        p, store, original_file, codec="lossless",
        resolution="source", selected_clip_ids=["c"],
    )
    info = probe(original_file)
    assert (info["video"]["width"], info["video"]["height"]) == (64, 64)
    frames = [Fraction(i, 24) for i in range(48)]
    np.testing.assert_array_equal(
        video_frames(movie, frames, 64, 64),
        video_frames(original_file, frames, 64, 64),
    )

    canvas_file = tmp_path / "canvas_pixels.mkv"
    export_project(p, store, canvas_file, codec="lossless")
    canvas = probe(canvas_file)
    assert (canvas["video"]["width"], canvas["video"]["height"]) == (32, 32)


def test_native_resolution_rejects_ambiguous_mixed_sources_and_audio_only(
    movie, tmp_path
):
    from ComfyUI_H3_Continuum_Join.timeline.compose import export_dimensions
    p, store = make_long_project(movie, tmp_path, selection=(0, 48))
    p["tracks"][0]["clips"][0]["end"] = 48
    p["tracks"][1]["clips"][0]["end"] = 48
    audio_only = validate({**p, "tracks": [p["tracks"][1]]})
    with pytest.raises(ValueError, match="requires a visible video"):
        export_dimensions(audio_only, store, 0, 48, "source")

    # Another registered source with a genuinely different native frame size.
    import subprocess
    other = tmp_path / "other.mkv"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi",
        "-i", "testsrc2=s=96x64:r=24:d=2",
        "-c:v", "ffv1", "-threads", "1", str(other),
    ], check=True)
    with other.open("rb") as stream:
        asset = store.assets.register(stream, "other.mkv")
    p["assets"].append(asset)
    p["tracks"].append({
        "id": "v2", "role": "retained",
        "clips": [{
            "id": "v2c", "asset_id": asset["id"],
            "start": 0, "end": 48, "source_in": "0",
            "speed": "1", "padding": "error", "audio_role": "neither",
        }],
    })
    p = validate(p)
    with pytest.raises(ValueError, match="Multiple source resolutions"):
        export_dimensions(p, store, 0, 48, "source")
    assert export_dimensions(p, store, 0, 48, "source", ["c"]) == (64, 64)

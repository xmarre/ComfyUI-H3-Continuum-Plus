from fractions import Fraction
import io
from pathlib import Path
import subprocess
import numpy as np
import pytest
from ComfyUI_H3_Continuum_Join.timeline.media import (
    AssetStore,
    ProjectStore,
    probe,
    video_frames,
    audio_window,
    mix_audio,
)
from ComfyUI_H3_Continuum_Join.timeline.model import blank_project, validate
from ComfyUI_H3_Continuum_Join.timeline.compose import export_project


@pytest.fixture
def movie(tmp_path):
    file = tmp_path / "source.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=64x64:r=25:d=14",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=14",
            "-c:v",
            "ffv1",
            "-c:a",
            "pcm_s16le",
            "-threads",
            "1",
            str(file),
        ],
        check=True,
    )
    return file


def test_pts_window_and_native_audio(movie):
    first = video_frames(movie, [Fraction(i, 24) for i in range(168)], 64, 64)
    second = video_frames(movie, [Fraction(i + 168, 24) for i in range(168)], 64, 64)
    assert first.shape == second.shape == (168, 64, 64, 3)
    assert not np.array_equal(first[-1], second[0])
    assert np.array_equal(second[0], video_frames(movie, ["7"], 64, 64)[0])
    for rate in (44100, 48000):
        a = audio_window(movie, "0", "7", rate)
        b = audio_window(movie, "7", "14", rate)
        whole = audio_window(movie, "0", "14", rate)
        assert a.shape == b.shape == (2, 7 * rate)
        np.testing.assert_allclose(np.concatenate([a, b], axis=1), whole, atol=1e-4)


def test_store_roundtrip_and_stale(movie, tmp_path):
    store = ProjectStore(tmp_path / "store")
    with movie.open("rb") as source:
        asset = store.assets.register(source, "source.mkv")
    assert store.assets.verify(asset["id"]).is_file()
    p = blank_project()
    p["assets"] = [asset]
    saved = store.save(p, 0)
    assert store.read(p["project_id"]) == saved
    with pytest.raises(ValueError, match="edited elsewhere"):
        store.save(p, 0)
    with pytest.raises(ValueError):
        store.assets.path("../source")
    store.assets.path(asset["id"]).write_bytes(b"stale")
    with pytest.raises(ValueError, match="changed"):
        store.assets.verify(asset["id"])


def test_audio_only_and_composition(movie, tmp_path):
    store = ProjectStore(tmp_path / "store")
    with movie.open("rb") as source:
        asset = store.assets.register(source, "source.mkv")
    p = blank_project()
    p.update(
        width=64, height=64, duration_frames=336, selection=[0, 336], assets=[asset]
    )
    c = {
        "id": "a",
        "asset_id": asset["id"],
        "start": 0,
        "end": 336,
        "source_in": "0",
        "speed": "1",
        "padding": "error",
        "audio_role": "both",
    }
    p["tracks"] = [
        {"id": "source", "role": "retained", "clips": [{**c, "audio_role": "neither"}]},
        {"id": "a", "role": "audio", "clips": [c]},
    ]
    p = validate(p)
    mixed = mix_audio(p, store.assets, 168, 336, 44100, "guide")
    np.testing.assert_allclose(mixed, audio_window(movie, "7", "14", 44100), atol=1e-6)
    p["duration_frames"] = 48
    p["selection"] = [0, 48]
    for t in p["tracks"]:
        t["clips"][0]["end"] = 48
    output = export_project(p, store, tmp_path / "out.mkv")
    info = probe(output)
    assert Fraction(info["duration"]) == 2
    original = video_frames(movie, [Fraction(i, 24) for i in range(48)], 64, 64)
    exported = video_frames(output, [Fraction(i, 24) for i in range(48)], 64, 64)
    np.testing.assert_array_equal(original, exported)
    np.testing.assert_array_equal(
        audio_window(movie, "0", "2", 44100), audio_window(output, "0", "2", 44100)
    )
    replacement = tmp_path / "replacement.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=24:d=2",
            "-c:v",
            "ffv1",
            "-threads",
            "1",
            str(replacement),
        ],
        check=True,
    )
    with replacement.open("rb") as file:
        blue = store.assets.register(file, "blue.mkv")
    p["assets"].append(blue)
    p["tracks"].append(
        {
            "id": "replacement",
            "role": "generated",
            "clips": [
                {
                    "id": "new",
                    "asset_id": blue["id"],
                    "start": 12,
                    "end": 36,
                    "source_in": "0",
                    "speed": "1",
                    "padding": "error",
                    "audio_role": "neither",
                }
            ],
        }
    )
    mixed = export_project(validate(p), store, tmp_path / "mixed.mkv")
    frames = video_frames(mixed, [Fraction(i, 24) for i in range(48)], 64, 64)
    np.testing.assert_array_equal(frames[:12], original[:12])
    np.testing.assert_array_equal(frames[36:], original[36:])
    expected = video_frames(replacement, [Fraction(i, 24) for i in range(24)], 64, 64)
    np.testing.assert_array_equal(frames[12:36], expected)
    np.testing.assert_array_equal(
        audio_window(mixed, "0", "2", 44100), audio_window(output, "0", "2", 44100)
    )


def test_cancel_leaves_no_partial_export(movie, tmp_path):
    store = ProjectStore(tmp_path / "store")
    p = blank_project()
    p.update(width=64, height=64, duration_frames=24, selection=[0, 24])

    def cancel():
        raise ValueError("cancelled")

    with pytest.raises(ValueError, match="cancelled"):
        export_project(p, store, tmp_path / "output.mkv", cancel=cancel)
    assert not (tmp_path / "output.mkv").exists()
    assert not list(tmp_path.glob("output-*.mkv"))


def test_vfr_nonzero_pts_and_audio_offset(tmp_path):
    file = tmp_path / "offset.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=64x64:r=30:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=sample_rate=48000:duration=3",
            "-vf",
            "select=not(mod(n\\,3)),setpts=PTS+5/TB",
            "-af",
            "asetpts=PTS+5/TB",
            "-fps_mode",
            "vfr",
            "-c:v",
            "ffv1",
            "-c:a",
            "pcm_s16le",
            "-threads",
            "1",
            str(file),
        ],
        check=True,
    )
    info = probe(file)
    assert Fraction(info["video"]["start_time"]) == 5
    assert Fraction(info["duration"]) == 3
    frames = video_frames(file, ["5", "5.1", "5.2", "5.9"], 64, 64)
    assert frames.shape == (4, 64, 64, 3)
    assert not np.array_equal(frames[0], frames[2])
    pcm = audio_window(file, "5", "6", 48000)
    assert pcm.shape == (2, 48000)
    before = audio_window(file, "4.5", "5.5", 48000)
    assert np.max(np.abs(before[:, :24000])) == 0
    np.testing.assert_allclose(before[:, 24000:], pcm[:, :24000], atol=1e-5)


def test_playlist_import_cannot_read_external_media(movie, tmp_path):
    playlist = tmp_path / "bad.m3u8"
    playlist.write_text("#EXTM3U\n#EXTINF:14,\n" + str(movie) + "\n")
    store = AssetStore(tmp_path / "assets")
    with playlist.open("rb") as source:
        with pytest.raises(subprocess.CalledProcessError):
            store.register(source, "bad.m3u8")
    assert not list(store.root.iterdir())


def test_two_hour_video_only_decodes_the_selected_short_window(tmp_path, monkeypatch):
    import av

    file = tmp_path / "long.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=1",
            "-t",
            "7200",
            "-c:v",
            "ffv1",
            "-threads",
            "1",
            str(file),
        ],
        check=True,
    )
    original = av.open
    decoded = []

    class Traced:
        def __init__(self, *args, **kwargs):
            self.container = original(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.container.close()

        @property
        def streams(self):
            return self.container.streams

        def seek(self, *args, **kwargs):
            return self.container.seek(*args, **kwargs)

        def decode(self, *args, **kwargs):
            for frame in self.container.decode(*args, **kwargs):
                decoded.append(frame.pts)
                yield frame

    monkeypatch.setattr(av, "open", Traced)
    frames = video_frames(
        file, [Fraction(3600 * 24 + i, 24) for i in range(168)], 64, 64
    )
    assert frames.shape == (168, 64, 64, 3)
    assert 1 < len(decoded) < 64  # requested window plus bounded GOP seek context


def test_real_video_reference_materialization_has_exact_logical_windows(
    movie, tmp_path
):
    import torch
    from ComfyUI_H3_Continuum_Join.timeline.runtime import TimelineInputs
    from ComfyUI_H3_Continuum_Join.timeline.model import compile_project
    from ComfyUI_H3_Continuum_Join.temporal import video_latent_t

    store = ProjectStore(tmp_path / "store")
    with movie.open("rb") as file:
        asset = store.assets.register(file, "source.mkv")
    p = blank_project()
    p.update(
        width=64, height=64, duration_frames=336, selection=[0, 336], assets=[asset]
    )
    p["tracks"] = [
        {
            "id": "guide",
            "role": "video_guide",
            "clips": [
                {
                    "id": "v",
                    "asset_id": asset["id"],
                    "start": 0,
                    "end": 336,
                    "source_in": "0",
                    "speed": "1",
                    "padding": "error",
                }
            ],
        }
    ]
    compiled = compile_project(p)
    provider = TimelineInputs(compiled["project"], compiled["jobs"][0], store)

    class VAE:
        def __init__(self):
            self.inputs = []

        def encode(self, frames):
            self.inputs.append(frames.clone())
            return torch.zeros(1, 24, video_latent_t(frames.shape[0]), 4, 4)

    vae = VAE()
    for index in (0, 1):
        provider.materialize(index, video_vae=vae, audio_vae=None, width=64, height=64)
    for index, frames in enumerate(vae.inputs):
        assert frames.shape[0] == 175
        expected = video_frames(
            movie, [Fraction(index * 168 + i, 24) for i in range(168)], 64, 64
        )
        np.testing.assert_array_equal(
            frames[:168].numpy(), expected.astype(np.float32) / 255
        )
        assert torch.equal(frames[168:], frames[167:168].expand(7, -1, -1, -1))


def test_cached_audio_windows_match_sequential_decode_and_decode_once(
    movie, tmp_path, monkeypatch
):
    import ComfyUI_H3_Continuum_Join.timeline.media as media

    cache = media.PcmCache(tmp_path / "pcm")
    calls = []
    original = media._decode_audio_stream
    monkeypatch.setattr(
        media,
        "_decode_audio_stream",
        lambda *a, **k: calls.append(k["limit"]) or original(*a, **k),
    )
    windows = [("0", "7"), ("7", "14"), ("3.5", "10.25"), ("13.5", "15")]
    for start, end in windows:
        cached = audio_window(movie, start, end, 48000, cache=cache)
        direct = audio_window(movie, start, end, 48000)
        np.testing.assert_array_equal(cached, direct)
    assert calls.count(None) == 1  # one full-stream decode serves every window


def test_duplicate_import_keeps_registered_file_identity(movie, tmp_path):
    store = AssetStore(tmp_path / "assets")
    with movie.open("rb") as source:
        first = store.register(source, "a.mkv")
    before = store.path(first["id"]).stat()
    with movie.open("rb") as source:
        second = store.register(source, "b.mkv")
    after = store.path(second["id"]).stat()
    assert first["id"] == second["id"]
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert store.verify(first["id"]).is_file()


def test_project_store_round_trips_non_ascii_text_as_utf8(tmp_path):
    store = ProjectStore(tmp_path / "store")
    p = blank_project()
    p["prompts"]["global"] = "雨の中で微笑む · café"
    store.save(p, 0)
    raw = (store.project_dir(p["project_id"]) / "project.json").read_bytes()
    assert "雨の中で微笑む".encode("utf-8") in raw
    assert store.read(p["project_id"])["prompts"]["global"] == p["prompts"]["global"]

"""Bounded composition of retained footage and selected rendered alternatives."""

from __future__ import annotations

import math
from fractions import Fraction
from pathlib import Path
import tempfile
import uuid

import numpy as np

from .media import check_cancel, mix_audio, run_process, video_frames
from .model import active_clips, source_time


def render_frames(
    p,
    assets,
    start,
    end,
    width,
    height,
    *,
    roles=None,
    gap="black",
    cancel=check_cancel,
):
    roles = roles or {"retained", "generated"}
    count = end - start
    output = np.zeros((count, height, width, 3), dtype=np.uint8)
    coverage = np.zeros(count, dtype=bool)
    # Process one clip window at a time. Last track/clip owns overlapping pixels.
    for _, c in active_clips(p, start, end, roles):
        a, b = max(start, c["start"]), min(end, c["end"])
        stamps = [source_time(c, f) for f in range(a, b)]
        frames = video_frames(
            assets.verify(c["asset_id"], cancel=cancel),
            stamps,
            width,
            height,
            padding=c["padding"],
            cancel=cancel,
        )
        output[a - start : b - start] = frames
        coverage[a - start : b - start] = True
    if gap == "error" and not coverage.all():
        raise ValueError(
            "Video guidance has a gap; add footage or remove guidance for this chunk"
        )
    return output


def write_video_frames(frames, path, fps=24):
    """Lossless CPU take encoding with an explicit RGB -> GBR pixel format."""
    import av

    with av.open(str(path), "w", format="matroska") as container:
        stream = container.add_stream("ffv1", rate=fps)
        stream.width = frames.shape[2]
        stream.height = frames.shape[1]
        stream.pix_fmt = "bgr0"
        for i, pixels in enumerate(frames):
            check_cancel()
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            frame.pts = i
            frame.time_base = Fraction(1, fps)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def export_project(
    p, store, output, *, codec="lossless", cancel=check_cancel, render_range=None
):
    """Stream a long composition in one-second CPU windows.

    FFV1 + PCM is pixel/sample preserving after declared scaling/time sampling.
    H.264/AAC is a distribution re-encode, never claimed bit exact.
    """
    import av

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.stem + "-" + uuid.uuid4().hex + output.suffix)
    start, stop = render_range or (0, p["duration_frames"])
    # A retained render must never silently use stale generated footage.
    from .nodes import stale_take, takes

    records = takes(store, p["project_id"])
    by_id = {r["id"]: r for r in records}
    for track in p["tracks"]:
        if track.get("muted") or track["role"] != "generated":
            continue
        for c in track["clips"]:
            if c["end"] <= start or c["start"] >= stop:
                continue
            take_id = c.get("take_id")
            if take_id and (
                take_id not in by_id
                or stale_take(
                    p,
                    by_id[take_id],
                    records,
                    end_frame=by_id[take_id].get(
                        "output_range", by_id[take_id]["range"]
                    )[0]
                    + math.ceil(
                        max(source_time(c, c["start"]), source_time(c, c["end"] - 1))
                        * 24
                        + 1
                    ),
                    check_selection=True,
                )
            ):
                raise ValueError(
                    "Selected generated footage has stale inputs or continuation; choose a valid take before export"
                )
    rate = int(
        p.get(
            "audio_sample_rate",
            next(
                (int(a["audio"]["sample_rate"]) for a in p["assets"] if a.get("audio")),
                48000,
            ),
        )
    )
    try:
        with tempfile.TemporaryDirectory(prefix="h3-export-") as directory:
            silent = Path(directory) / "video.mkv"
            pcmfile = Path(directory) / "audio.f32"
            with av.open(str(silent), "w", format="matroska") as container:
                stream = container.add_stream("ffv1", rate=24)
                stream.width = p["width"]
                stream.height = p["height"]
                stream.pix_fmt = "bgr0"
                with pcmfile.open("wb") as wav:
                    for a in range(start, stop, 24):
                        cancel()
                        b = min(a + 24, stop)
                        frames = render_frames(
                            p,
                            store.assets,
                            a,
                            b,
                            p["width"],
                            p["height"],
                            cancel=cancel,
                        )
                        for n, pixels in enumerate(frames, a):
                            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
                            frame.pts = n - start
                            frame.time_base = Fraction(1, 24)
                            for packet in stream.encode(frame):
                                container.mux(packet)
                        pcm = mix_audio(
                            p, store.assets, a, b, rate, "final", cancel=cancel
                        )
                        wav.write(pcm.T.astype("<f4").tobytes())
                for packet in stream.encode():
                    container.mux(packet)
            options = (
                ["-c:v", "copy", "-c:a", "pcm_f32le"]
                if codec == "lossless"
                else [
                    "-c:v",
                    "libx264",
                    "-crf",
                    "16",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                ]
            )
            run_process(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-threads",
                    "1",
                    "-i",
                    str(silent),
                    "-f",
                    "f32le",
                    "-ar",
                    str(rate),
                    "-ac",
                    "2",
                    "-i",
                    str(pcmfile),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    *options,
                    str(temp),
                ],
                cancel=cancel,
            )
        temp.replace(output)
    finally:
        temp.unlink(missing_ok=True)
    return output

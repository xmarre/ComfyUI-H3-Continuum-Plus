"""Bounded composition of retained footage and selected rendered alternatives."""

from __future__ import annotations

import math
from fractions import Fraction
from pathlib import Path

import numpy as np

from .media import check_cancel, mix_audio, run_process, video_frames
from .model import active_clips, source_time

AUDIO_BLOCK_FRAMES = 24 * 30


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
    if gap == "hold" and coverage.any() and not coverage.all():
        # Uncovered guide frames repeat the nearest earlier covered frame, or
        # the first covered frame before any coverage.
        covered = np.flatnonzero(coverage)
        source = covered[
            np.clip(np.searchsorted(covered, np.arange(count), "right") - 1, 0, None)
        ]
        output = output[source]
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



def export_dimensions(
    p, store, start, stop, resolution="timeline", selected_clip_ids=(),
    *, cancel=check_cancel,
):
    """Choose output pixels independently of the H3 sampling canvas.

    Source-size exports require one unambiguous native source geometry. A
    highlighted visual clip supplies that geometry even when audio is selected.
    Never silently fall back to the lower-resolution generation canvas.
    """
    if resolution == "timeline":
        return p["width"], p["height"]
    if resolution != "source":
        raise ValueError("Export resolution must be source or timeline")
    visible = active_clips(p, start, stop, {"retained", "generated"})
    selected = set(selected_clip_ids)
    highlighted = [(t, c) for t, c in visible if c["id"] in selected]
    candidates = highlighted or visible
    if not candidates:
        raise ValueError(
            "Original-size export requires a visible video clip in this range; "
            "choose Timeline canvas for audio-only exports"
        )
    geometries = set()
    for _, clip in candidates:
        path = store.assets.verify(clip["asset_id"], cancel=cancel)
        from .media import probe
        stream = probe(path).get("video")
        if not stream:
            raise ValueError("Selected export clip contains no video")
        geometries.add((int(stream["width"]), int(stream["height"])))
    if len(geometries) != 1:
        raise ValueError(
            "Multiple source resolutions in the export range. Highlight one "
            "video clip to choose its size, or choose Timeline canvas explicitly."
        )
    return geometries.pop()


def export_project(
    p, store, output, *, codec="lossless", cancel=check_cancel,
    render_range=None, preview=False, on_progress=None,
    resolution="timeline", selected_clip_ids=(),
):
    """Stream a long composition in one-second CPU windows.

    FFV1 + PCM is pixel/sample preserving after declared scaling/time sampling.
    H.264/AAC is a distribution re-encode, never claimed bit exact.
    """
    import av

    # Default to the user's explicit Render [in,out) selection, never the
    # entire movie/project just because a caller omitted render_range.
    start, stop = (render_range if render_range is not None
                   else p["selection"])
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
    width, height = export_dimensions(
        p, store, start, stop, resolution, selected_clip_ids,
        cancel=cancel,
    )
    from .export_stream import encode_export

    return encode_export(
        p, store, output, start, stop, rate,
        codec=codec, cancel=cancel, preview=preview,
        width=width, height=height,
        on_progress=on_progress,
    )


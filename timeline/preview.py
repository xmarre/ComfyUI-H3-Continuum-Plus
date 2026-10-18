"""Small, cancellable low-latency previews, independent of full-film export.

Preview is an approximation for monitoring, never an exact Continuum render.
The regular export path retains its verified frame/sample clocks.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
import uuid

from .media import MEDIA_FORMATS, check_cancel, run_process
from .model import active_clips, rational, source_time

FIRST_SEGMENT_FRAMES = 48
NEXT_SEGMENT_FRAMES = 144
MAX_SEGMENT_FRAMES = 192
PREVIEW_MAX_EDGE = 480


def simple_source(p, start, stop):
    """Only direct-seek if the exact picture and audio map to one original.

    More complicated overlays, ramps, reversals, final-audio mixes or
    generated alternatives use the short composed render instead.
    """
    video = active_clips(p, start, stop, {"retained", "generated"})
    if len(video) != 1:
        return None
    _, clip = video[0]
    if (
        clip["start"] > start
        or clip["end"] < stop
        or rational(clip.get("speed", "1")) != 1
        or clip.get("padding", "error") not in {"error", "hold"}
    ):
        return None
    audio = [
        c for _, c in active_clips(p, start, stop)
        if c.get("audio_role") in {"final", "both"}
    ]
    with_audio = False
    if audio:
        if len(audio) != 1:
            return None
        sound = audio[0]
        if (
            sound["asset_id"] != clip["asset_id"]
            or sound["start"] > start or sound["end"] < stop
            or source_time(sound, start) != source_time(clip, start)
            or rational(sound.get("speed", "1")) != 1
            or rational(sound.get("gain", "1")) != 1
            or sound.get("fade_in") or sound.get("fade_out")
            or sound.get("audio_fallback")
        ):
            return None
        with_audio = True
    return clip, with_audio


def _quick_source_segment(p, store, output, start, stop, selection, *, cancel):
    """Seek directly to a short source window; never decode a preceding hour."""
    clip, with_audio = selection
    file = store.assets.verify(clip["asset_id"], cancel=cancel)
    if not any(a["id"] == clip["asset_id"] and a.get("video") for a in p["assets"]):
        raise ValueError("Preview clip has no registered video")
    when = source_time(clip, start)
    if when < 0:
        raise ValueError("Preview source timestamp is negative")
    # Match the source's absolute stream PTS as used by model.py, including
    # media with nonzero initial PTS. ffmpeg normally seeks relative to file
    # origin, so -seek_timestamp=1 is required here.
    width, height = p["width"], p["height"]
    filters = (
        f"fps=24,scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin", "-threads", "1",
        "-filter_threads", "1", "-seek_timestamp", "1",
        "-ss", str(float(when)),
        "-protocol_whitelist", "file,pipe",
        "-format_whitelist", MEDIA_FORMATS,
        "-i", str(file),
        "-t", str(Fraction(stop - start, 24)),
        "-map", "0:v:0",
        *(["-map", "0:a:0?"] if with_audio else ["-an"]),
        "-vf", filters,
        "-c:v", "libx264", "-threads:v", "1", "-preset", "ultrafast", "-crf", "20",
        "-r", "24", "-pix_fmt", "yuv420p",
        *(
            ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
            if with_audio else []
        ),
        "-movflags", "+faststart", str(output),
    ]
    run_process(cmd, cancel=cancel)


def render_preview_segment(p, store, output, start, stop, *, cancel=check_cancel):
    """Render at most eight seconds and atomically publish only on success."""
    from .compose import export_project

    if not 0 <= start < stop <= p["duration_frames"]:
        raise ValueError("Invalid playback interval")
    if stop - start > MAX_SEGMENT_FRAMES:
        raise ValueError("Playback segment exceeds eight seconds")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + "-" + uuid.uuid4().hex + ".mp4")
    try:
        cancel()
        selection = simple_source(p, start, stop)
        if selection is not None:
            _quick_source_segment(
                p, store, temporary, start, stop, selection, cancel=cancel
            )
        else:
            # Exact export has a two-pass FFV1 intermediate; this fallback is
            # deliberately tiny and uses bounded source-seeking preview audio.
            export_project(
                p, store, temporary, codec="h264",
                render_range=(start, stop), preview=True, cancel=cancel
            )
        cancel()
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output

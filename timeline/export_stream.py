"""Single-pass, atomic video+audio muxing for full Timeline Studio exports.

The prior implementation created a potentially huge FFV1 video and raw PCM
intermediate, then transcoded the *entire movie again*. This module encodes
video/audio into the final container together, with <= one second of RGB frames
in memory, and one temporary decoded PCM cache per distinct final-audio source
to prevent O(N^2) prefix decoding on two-hour timelines.

This path does not change H3 generation, final sample-index semantics, or the
native no-copy original asset store.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
import shutil
import tempfile
import uuid

import numpy as np

from .media import PcmCache, check_cancel, mix_audio, probe
from .model import active_clips, rational

AUDIO_CHUNK_SAMPLES = 1024
AUDIO_CHANNELS = 2
BYTES_PER_SAMPLE = 4
AUDIO_CACHE_MARGIN = 64 * 1024**2


def _export_pcm_cache(p, store, start, stop, rate, workdir, *, cancel):
    """Use temporary exact decoded audio, never a persistent full-film duplicate.

    Without a cache, decoding N successive exact windows from source start
    performs approximately N² work. Source audio is decoded once per asset
    into a temporary PCM file and reused with absolute sample-index slices.
    The files are removed automatically when export completes or fails.
    """
    assets = {a["id"]: a for a in p["assets"]}
    final_ids = {
        c["asset_id"]
        for _, c in active_clips(p, start, stop)
        if c.get("audio_role") in {"final", "both"}
    }
    if not final_ids:
        return None
    expected = 0
    for asset_id in sorted(final_ids):
        cancel()
        file = store.assets.verify(asset_id, cancel=cancel)
        info = probe(file)
        if not info.get("audio"):
            continue
        duration = rational(
            info["audio"].get("duration") or info["duration"]
        )
        expected += int(max(Fraction(0), duration) * rate) * (
            AUDIO_CHANNELS * BYTES_PER_SAMPLE
        )
    if not expected:
        return None
    available = shutil.disk_usage(workdir).free
    required = expected + AUDIO_CACHE_MARGIN
    if required > available:
        raise ValueError(
            "Not enough disk space for sample-exact export audio: "
            f"requires approximately {required / 1024**3:.2f} GiB, "
            f"available {available / 1024**3:.2f} GiB. "
            "Free space in the ComfyUI output location or export a shorter range."
        )
    return PcmCache(Path(workdir) / "pcm", limit_bytes=required)


def encode_export(
    p,
    store,
    output,
    start,
    stop,
    rate,
    *,
    codec,
    cancel=check_cancel,
    on_progress=None,
    preview=False,
):
    """One-pass direct container mux, bounded per-second media preparation."""
    import av
    from .compose import render_frames

    if codec not in {"lossless", "h264"}:
        raise ValueError("Unknown export codec")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(
        output.stem + "-" + uuid.uuid4().hex + output.suffix
    )
    total = stop - start
    if not 0 <= start < stop <= p["duration_frames"]:
        raise ValueError("Invalid export range")
    on_progress = on_progress or (lambda **info: None)

    try:
        on_progress(
            stage="preparing exact source audio",
            frames_done=0, total_frames=total,
        )
        with tempfile.TemporaryDirectory(
            prefix="h3-export-scratch-", dir=output.parent
        ) as scratch:
            cache = (
                None if preview else _export_pcm_cache(
                    p, store, start, stop, rate, scratch, cancel=cancel
                )
            )
            on_progress(stage="encoding", frames_done=0, total_frames=total)
            container_format = "matroska" if codec == "lossless" else "mp4"
            options = (
                {} if codec == "lossless" else {"movflags": "+faststart"}
            )
            with av.open(
                str(temporary), "w", format=container_format, options=options
            ) as container:
                vstream = container.add_stream(
                    "ffv1" if codec == "lossless" else "libx264", rate=24
                )
                vstream.width = p["width"]
                vstream.height = p["height"]
                vstream.pix_fmt = "bgr0" if codec == "lossless" else "yuv420p"
                if codec == "h264":
                    vstream.options = {
                        "crf": "27" if preview else "16",
                        "preset": "ultrafast" if preview else "medium",
                    }
                if preview:
                    vstream.thread_count = 1
                astream = container.add_stream(
                    "pcm_f32le" if codec == "lossless" else "aac", rate=rate
                )
                astream.layout = "stereo"
                if codec == "h264":
                    astream.bit_rate = 192_000
                audio_format = "flt" if codec == "lossless" else "fltp"
                sample_cursor = 0
                for a in range(start, stop, 24):
                    cancel()
                    b = min(a + 24, stop)
                    frames = render_frames(
                        p, store.assets, a, b,
                        p["width"], p["height"], cancel=cancel,
                    )
                    for n, pixels in enumerate(frames):
                        cancel()
                        frame = av.VideoFrame.from_ndarray(
                            pixels, format="rgb24"
                        )
                        frame.pts = a - start + n
                        frame.time_base = Fraction(1, 24)
                        for packet in vstream.encode(frame):
                            container.mux(packet)
                    pcm = mix_audio(
                        p, store.assets, a, b, rate, "final",
                        cancel=cancel, preview=preview,
                        export_cache=cache,
                    )
                    for index in range(
                        0, pcm.shape[1], AUDIO_CHUNK_SAMPLES
                    ):
                        cancel()
                        chunk = pcm[:, index:index + AUDIO_CHUNK_SAMPLES]
                        data = (
                            np.ascontiguousarray(chunk)
                            if audio_format == "fltp"
                            else np.ascontiguousarray(chunk.T.reshape(1, -1))
                        )
                        af = av.AudioFrame.from_ndarray(
                            data, format=audio_format, layout="stereo"
                        )
                        af.sample_rate = rate
                        af.pts = sample_cursor
                        af.time_base = Fraction(1, rate)
                        sample_cursor += chunk.shape[1]
                        for packet in astream.encode(af):
                            container.mux(packet)
                    on_progress(
                        stage="encoding",
                        frames_done=b - start,
                        total_frames=total,
                    )
                cancel()
                on_progress(
                    stage="finalizing", frames_done=total, total_frames=total
                )
                for packet in vstream.encode(None):
                    container.mux(packet)
                for packet in astream.encode(None):
                    container.mux(packet)
        cancel()
        temporary.replace(output)
        on_progress(stage="complete", frames_done=total, total_frames=total)
        return output
    finally:
        temporary.unlink(missing_ok=True)

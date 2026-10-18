"""CPU media access. Assets are immutable, content-addressed local files.

PyAV selects video by absolute presentation timestamp. Only requested frames
are retained; no source movie is materialized as a full IMAGE batch.
"""

from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import uuid
import weakref

import numpy as np

from .model import rational, sample_boundary, source_time

# Excludes playlist demuxers that can dereference editor-supplied local paths.
MEDIA_FORMATS = "mov,matroska,avi,wav,flac,mp3,ogg,aac,image2,png_pipe,jpeg_pipe,webp_pipe,bmp_pipe,gif,apng,mxf,mpeg,mpegts,asf"


def check_cancel():
    try:
        import comfy.model_management

        comfy.model_management.throw_exception_if_processing_interrupted()
    except ImportError:
        pass


def run_process(args, *, cancel=check_cancel):
    """Bounded stderr and cancellation with no shell or pipe deadlock."""
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors
        )
        try:
            while proc.poll() is None:
                cancel()
                time.sleep(0.05)
            if proc.returncode:
                errors.seek(0, 2)
                size = errors.tell()
                errors.seek(max(0, size - 4000))
                raise ValueError(
                    "Media process failed: " + errors.read().decode(errors="replace")
                )
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


_probe_cache = {}
_probe_lock = threading.Lock()


def _file_stamp(path):
    st = Path(path).stat()
    return (
        str(Path(path).resolve()),
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
        st.st_ino,
    )


def probe(path):
    """ffprobe stream summary, memoized by file identity (path, size, mtime)."""
    key = _file_stamp(path)
    with _probe_lock:
        cached = _probe_cache.get(key)
    if cached is not None:
        return json.loads(cached)
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            MEDIA_FORMATS,
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    duration = (video or audio or {}).get(
        "duration", data.get("format", {}).get("duration", "0")
    )
    if not (video or audio or {}).get("duration"):
        # Container duration can include a positive absolute timestamp offset.
        duration = max(
            rational(0),
            rational(duration)
            - rational(data.get("format", {}).get("start_time", "0")),
        )
    info = {"duration": str(rational(duration)), "video": video, "audio": audio}
    with _probe_lock:
        _probe_cache[key] = json.dumps(info)
        while len(_probe_cache) > 256:
            _probe_cache.pop(next(iter(_probe_cache)))
    return info


class AssetStore:
    _verified = {}
    _verification_lock = threading.RLock()

    @staticmethod
    def _stamp(path):
        s = path.stat()
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

    @classmethod
    def _remember(cls, path, stamp):
        with cls._verification_lock:
            cls._verified[str(path)] = stamp
            while len(cls._verified) > 512:
                cls._verified.pop(next(iter(cls._verified)))

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.pcm_cache = PcmCache(self.root.parent / "pcm_cache")

    def path(self, asset_id):
        if not re.fullmatch(r"[a-f0-9]{64}", str(asset_id)):
            raise ValueError("Invalid registered media identifier")
        p = (self.root / asset_id).resolve()
        if p.parent != self.root or not p.is_file():
            raise ValueError("Registered media is missing; import or relink it")
        return p

    def upload_path(self):
        """A fresh temporary path inside the store, on the assets' filesystem."""
        return self.root / ("upload-" + uuid.uuid4().hex)

    def register(self, source, name=None):
        """Stream a file-like source into immutable content-addressed storage."""
        h = hashlib.sha256()
        temporary = self.upload_path()
        try:
            with temporary.open("xb") as out:
                while block := source.read(1024 * 1024):
                    h.update(block)
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            return self.adopt(temporary, h.hexdigest(), name)
        finally:
            temporary.unlink(missing_ok=True)

    def adopt(self, temporary, asset_id, name=None):
        """Register a complete upload written to :meth:`upload_path`."""
        info = probe(temporary)
        final = self.root / asset_id
        # Publish without replacing a concurrent import's open file. Existing
        # bytes must be verified before they can be remembered as this hash.
        try:
            os.link(temporary, final)
        except FileExistsError:
            self.verify(asset_id)
        else:
            self._remember(final, self._stamp(final))
        return {
            "id": asset_id,
            "name": Path(name or "Media").name,
            "kind": "video" if info["video"] else "audio",
            **info,
        }

    def verify(self, asset_id, *, cancel=check_cancel):
        path = self.path(asset_id)
        stamp = self._stamp(path)
        with self._verification_lock:
            if self._verified.get(str(path)) == stamp:
                return path
        h = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                cancel()
                h.update(block)
        if h.hexdigest() != asset_id or self._stamp(path) != stamp:
            raise ValueError(
                "Registered asset content changed; relink it before rendering"
            )
        self._remember(path, stamp)
        return path


def video_frames(
    path, timestamps, width, height, *, padding="error", cancel=check_cancel
):
    """Nearest source PTS with earlier-frame tie breaks, including VFR/offsets.

    The output sequence follows requested timestamps exactly. Reverse is an
    explicit source mapping; a bounded reverse window is retained locally.
    """
    import av

    if not timestamps:
        return np.empty((0, height, width, 3), dtype=np.uint8)
    requested = [rational(t) for t in timestamps]
    unique = sorted(set(requested))
    sampled = {}
    with av.open(
        str(path),
        options={"protocol_whitelist": "file,pipe", "format_whitelist": MEDIA_FORMATS},
    ) as container:
        if not container.streams.video:
            raise ValueError("Selected media has no video stream")
        stream = container.streams.video[0]
        stream.thread_type = "SLICE"
        origin = Fraction(stream.start_time or 0) * stream.time_base
        if unique[0] < origin and padding == "error":
            raise ValueError("Video selection starts before the first source PTS")
        seek = max(origin, unique[0])
        container.seek(int(seek / stream.time_base), stream=stream, backward=True)
        iterator = iter(container.decode(stream))
        previous = None
        future = next(iterator, None)
        for target in unique:
            cancel()
            if target < origin and padding == "silence":
                sampled[target] = np.zeros((height, width, 3), dtype=np.uint8)
                continue
            while (
                future is not None
                and future.pts is not None
                and Fraction(future.pts) * future.time_base <= target
            ):
                cancel()
                previous = future
                future = next(iterator, None)
            chosen = previous or future
            if previous is not None and future is not None and future.pts is not None:
                before = Fraction(previous.pts) * previous.time_base
                after = Fraction(future.pts) * future.time_base
                if after - target < target - before:
                    chosen = future
            if chosen is None:
                raise ValueError("Source selection contains no decodable video")
            if future is None:
                last_pts = Fraction(chosen.pts) * chosen.time_base
                duration = Fraction(chosen.duration or 0) * chosen.time_base
                if not duration:
                    duration = 1 / (stream.average_rate or Fraction(24))
                if target >= last_pts + duration and padding == "silence":
                    sampled[target] = np.zeros((height, width, 3), dtype=np.uint8)
                    continue
                if target >= last_pts + duration and padding == "error":
                    raise ValueError(
                        "Video selection extends beyond source; choose Hold explicitly"
                    )
            # One RGB frame per requested output timestamp, independent of movie duration.
            scale = min(width / chosen.width, height / chosen.height)
            fw, fh = (
                max(1, round(chosen.width * scale)),
                max(1, round(chosen.height * scale)),
            )
            rgb = chosen.reformat(width=fw, height=fh, format="rgb24").to_ndarray()
            fitted = np.zeros((height, width, 3), dtype=np.uint8)
            x, y = (width - fw) // 2, (height - fh) // 2
            fitted[y : y + fh, x : x + fw] = rgb
            sampled[target] = fitted
    return np.stack([sampled[t] for t in requested])


def _decode_audio_stream(path, info, rate, channels, output, *, limit, cancel, first=0):
    """Decode the stream from its first sample onto the absolute output grid.

    Output sample ``j`` is stream-origin time ``j / rate``. Packet PTS in
    Matroska can be quantized to milliseconds; seeking and time trimming
    independently would introduce one-sample shifts between windows, so the
    stream is always decoded sequentially from its start and selected by
    sample index. ``limit`` stops decoding after that many output samples.
    """
    channel_filter = (
        "pan=stereo|c0=c0|c1=c0,"
        if channels == 2 and int(info["audio"].get("channels", 2)) == 1
        else ""
    )
    filters = channel_filter + f"aformat=sample_fmts=flt,aresample={rate}:async=1"
    if limit is not None:
        filters += f",atrim=start_sample={max(0, first)}:end_sample={max(0, limit)}"
    run_process(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-threads",
            "1",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            MEDIA_FORMATS,
            "-i",
            str(path),
            "-vn",
            "-af",
            filters,
            "-ac",
            str(channels),
            "-ar",
            str(rate),
            "-f",
            "f32le",
            str(output),
        ],
        cancel=cancel,
    )


class PcmCache:
    """Disk cache of whole decoded audio streams, one file per (media, rate).

    The first window of a source pays one sequential decode; later windows are
    memory-mapped slices, so long exports and waveform scrolling stay linear in
    the requested duration. Least recently used files beyond ``limit_bytes``
    are removed. ``H3_TIMELINE_PCM_CACHE_GB`` overrides the default 16 GiB.
    """

    _locks = weakref.WeakValueDictionary()
    _guard = threading.Lock()

    def __init__(self, root, limit_bytes=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if limit_bytes is None:
            limit_bytes = int(
                float(os.environ.get("H3_TIMELINE_PCM_CACHE_GB", "16")) * 1024**3
            )
        self.limit = limit_bytes

    def _lock(self, name):
        with self._guard:
            return self._locks.setdefault(name, threading.Lock())

    def stream(self, path, info, rate, channels, *, cancel):
        frame_bytes = 4 * channels
        duration = rational(info["audio"].get("duration", info["duration"]))
        if self.limit < frame_bytes or duration * rate * frame_bytes > self.limit:
            return None  # The caller uses a bounded, sample-selected decode.
        identity = hashlib.sha256(
            json.dumps([*_file_stamp(path), rate, channels, 1]).encode()
        ).hexdigest()
        file = self.root / (identity + ".f32")
        lock = self._lock(identity)
        while not lock.acquire(timeout=0.1):
            cancel()
        try:
            cancel()
            # Guard opening against eviction. Open mappings survive unlink on
            # POSIX; Windows refuses to unlink a file while it is mapped.
            with self._guard:
                if file.is_file():
                    os.utime(file)
                    return self._map(file, channels)
            temporary = self.root / (identity + "-" + uuid.uuid4().hex + ".tmp")
            try:
                _decode_audio_stream(
                    path,
                    info,
                    rate,
                    channels,
                    temporary,
                    limit=self.limit // frame_bytes + 1,
                    cancel=cancel,
                )
                if temporary.stat().st_size > self.limit:
                    return None
                with self._guard:
                    os.replace(temporary, file)
                    mapped = self._map(file, channels)
                    self._evict(keep=file)
                    return mapped
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            lock.release()

    @staticmethod
    def _map(file, channels):
        frames = file.stat().st_size // (4 * channels)
        if not frames:
            return np.zeros((0, channels), dtype=np.float32)
        return np.memmap(file, dtype="<f4", mode="r", shape=(frames, channels))

    def _evict(self, *, keep):
        files = []
        for entry in self.root.glob("*.f32"):
            try:
                st = entry.stat()
            except OSError:
                continue
            files.append((st.st_mtime_ns, st.st_size, entry))
        total = sum(size for _, size, _ in files)
        for _, size, entry in sorted(files):
            if total <= self.limit:
                break
            if entry == keep:
                continue
            try:
                entry.unlink()
                total -= size
            except OSError:
                continue  # Still mapped elsewhere (Windows); retry on a later eviction.


def audio_window(
    path,
    start,
    end,
    rate,
    *,
    channels=2,
    padding="silence",
    cancel=check_cancel,
    cache=None,
):
    """Decode an exact absolute-PTS interval to a bounded PCM sample window.

    With a :class:`PcmCache` the decoded stream is reused across calls.
    Without one, only the stream prefix up to the window end is decoded.
    """
    start, end = rational(start), rational(end)
    count = round(end * rate) - round(start * rate)
    if count <= 0:
        return np.zeros((channels, 0), dtype=np.float32)
    info = probe(path)
    if not info["audio"]:
        if padding != "silence":
            raise ValueError("Selected audio route has no source audio stream")
        return np.zeros((channels, count), dtype=np.float32)
    origin = rational(info["audio"].get("start_time", "0"))
    if padding == "error" and (
        start < origin
        or end > origin + rational(info["audio"].get("duration", info["duration"]))
    ):
        raise ValueError("Audio selection extends beyond the source stream")
    first = round(start * rate) - round(origin * rate)
    last = first + count
    result = np.zeros((channels, count), dtype=np.float32)
    if last <= 0:
        return result
    stream = (
        cache.stream(path, info, rate, channels, cancel=cancel)
        if cache is not None
        else None
    )
    if stream is not None:
        try:
            selected = np.asarray(stream[max(0, first) : last]).T
            offset = max(0, -first)
            n = min(selected.shape[1], count - offset)
            result[:, offset : offset + n] = selected[:, :n]
        finally:
            if isinstance(stream, np.memmap):
                stream._mmap.close()
        return result
    else:
        with tempfile.TemporaryDirectory(prefix="h3-audio-") as directory:
            output = Path(directory) / "window.f32"
            _decode_audio_stream(
                path,
                info,
                rate,
                channels,
                output,
                limit=last,
                first=max(0, first),
                cancel=cancel,
            )
            pcm = np.fromfile(output, dtype="<f4")
        frames = pcm.size // channels
        selected = pcm[: frames * channels].reshape(-1, channels).T
    offset = max(0, -first)
    n = min(selected.shape[1], count - offset)
    result[:, offset : offset + n] = selected[:, :n]
    return result


def mix_audio(p, assets, start, end, rate, route, *, cancel=check_cancel):
    """Deterministic sample-grid mix. Route is guide, final or reference."""
    from .model import active_clips

    a, b = sample_boundary(start, rate), sample_boundary(end, rate)
    mix = np.zeros((2, b - a), dtype=np.float32)
    routes = {
        "guide": {"guide", "both"},
        "final": {"final", "both"},
        "reference": {"reference"},
    }[route]
    active = active_clips(p, start, end)
    for track, c in active:
        if c["audio_role"] not in routes:
            continue
        left, right = max(start, c["start"]), min(end, c["end"])
        sa, sb = source_time(c, left), source_time(c, right)
        speed = rational(c["speed"])
        pcm = audio_window(
            assets.verify(c["asset_id"], cancel=cancel),
            min(sa, sb),
            max(sa, sb),
            rate,
            padding=c["padding"],
            cancel=cancel,
            cache=getattr(assets, "pcm_cache", None),
        )
        if speed < 0:
            pcm = pcm[:, ::-1]
        n = sample_boundary(right, rate) - sample_boundary(left, rate)
        if speed != 1:
            # Explicit pitch-changing retime. No hidden stretch or pitch correction.
            pos = np.arange(n) * float(abs(speed))
            pcm = np.stack(
                [
                    np.interp(pos, np.arange(pcm.shape[1]), channel, left=0, right=0)
                    for channel in pcm
                ]
            )
        pcm = pcm[:, :n]
        if pcm.shape[1] < n:
            pcm = np.pad(pcm, ((0, 0), (0, n - pcm.shape[1])))
        absolute = np.arange(sample_boundary(left, rate), sample_boundary(right, rate))
        envelope = np.ones(n)
        if (
            route == "final"
            and track["role"] == "generated"
            and c.get("audio_fallback")
        ):
            for authored_track, authored in active:
                if authored_track["role"] != "generated" and authored["audio_role"] in {
                    "final",
                    "both",
                }:
                    envelope[
                        (absolute >= sample_boundary(authored["start"], rate))
                        & (absolute < sample_boundary(authored["end"], rate))
                    ] = 0
        for fade, distance in (
            (c.get("fade_in", 0), absolute - sample_boundary(c["start"], rate)),
            (c.get("fade_out", 0), sample_boundary(c["end"], rate) - absolute),
        ):
            if fade:
                envelope *= np.clip(
                    distance / max(1, round(float(rational(fade)) * rate)), 0, 1
                )
        offset = sample_boundary(left, rate) - a
        mix[:, offset : offset + n] += (
            pcm * envelope * float(rational(c.get("gain", 1)))
        )
    return np.clip(mix, -1, 1)


class ProjectStore:
    """Optimistic project saves and immutable job/take records, atomic on disk."""

    _lock = threading.RLock()

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets = AssetStore(self.root / "assets")

    def project_dir(self, project_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(project_id)):
            raise ValueError("Invalid project identifier")
        p = self.root / "projects" / project_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def read(self, project_id):
        return json.loads(
            (self.project_dir(project_id) / "project.json").read_text(encoding="utf-8")
        )

    def save(self, project, expected_revision):
        from .model import validate

        p = validate(project)
        with self._lock:
            file = self.project_dir(p["project_id"]) / "project.json"
            old = (
                json.loads(file.read_text(encoding="utf-8"))
                if file.exists()
                else {"revision": 0}
            )
            if old["revision"] != expected_revision:
                raise ValueError("Project was edited elsewhere; reload before saving")
            p["revision"] = expected_revision + 1
            self.atomic_json(file, p)
            return p

    @staticmethod
    def atomic_json(path, value):
        path = Path(path)
        tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with tmp.open("x", encoding="utf-8") as out:
                json.dump(value, out, ensure_ascii=False, allow_nan=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

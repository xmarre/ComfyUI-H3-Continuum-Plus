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

import numpy as np

from .model import rational, sample_boundary, source_time


def check_cancel():
    try:
        import comfy.model_management
        comfy.model_management.throw_exception_if_processing_interrupted()
    except ImportError:
        pass


def run_process(args, *, cancel=check_cancel):
    """Bounded stderr and cancellation with no shell or pipe deadlock."""
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors)
        try:
            while proc.poll() is None:
                cancel()
                time.sleep(0.05)
            if proc.returncode:
                errors.seek(0, 2)
                size = errors.tell()
                errors.seek(max(0, size - 4000))
                raise ValueError("Media process failed: " + errors.read().decode(errors="replace"))
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def probe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True, check=True, timeout=30)
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    duration = (video or audio or {}).get("duration", data.get("format", {}).get("duration", "0"))
    return {"duration": str(rational(duration)), "video": video, "audio": audio}


class AssetStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, asset_id):
        if not re.fullmatch(r"[a-f0-9]{64}", str(asset_id)):
            raise ValueError("Invalid registered media identifier")
        p = (self.root / asset_id).resolve()
        if p.parent != self.root or not p.is_file():
            raise ValueError("Registered media is missing; import or relink it")
        return p

    def register(self, source, name=None):
        """Stream a file-like source into immutable content-addressed storage."""
        h = hashlib.sha256()
        temporary = self.root / ("upload-" + uuid.uuid4().hex)
        try:
            with temporary.open("xb") as out:
                while block := source.read(1024 * 1024):
                    h.update(block)
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            asset_id = h.hexdigest()
            info = probe(temporary)
            final = self.root / asset_id
            os.replace(temporary, final)
            return {"id": asset_id, "name": Path(name or "Media").name, "kind": "video" if info["video"] else "audio", **info}
        finally:
            temporary.unlink(missing_ok=True)

    def verify(self, asset_id):
        path = self.path(asset_id)
        h = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                h.update(block)
        if h.hexdigest() != asset_id:
            raise ValueError("Registered asset content changed; relink it before rendering")
        return path


def video_frames(path, timestamps, width, height, *, padding="error", cancel=check_cancel):
    """Sample-and-hold on source PTS, including VFR and nonzero stream starts.

    The output sequence follows requested timestamps exactly. Reverse is an
    explicit source mapping; a bounded reverse window is retained locally.
    """
    import av
    if not timestamps:
        return np.empty((0, height, width, 3), dtype=np.uint8)
    requested = [rational(t) for t in timestamps]
    unique = sorted(set(requested))
    sampled = {}
    with av.open(str(path), options={"protocol_whitelist": "file,pipe"}) as container:
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
            while future is not None and future.pts is not None and Fraction(future.pts) * future.time_base <= target:
                previous = future
                future = next(iterator, None)
            chosen = previous or future
            if chosen is None:
                raise ValueError("Source selection contains no decodable video")
            if future is None:
                last_pts = Fraction(chosen.pts) * chosen.time_base
                duration = Fraction(chosen.duration or 0) * chosen.time_base
                if not duration:
                    duration = 1 / (stream.average_rate or Fraction(24))
                if target >= last_pts + duration and padding == "error":
                    raise ValueError("Video selection extends beyond source; choose Hold explicitly")
            # One RGB frame per requested output timestamp, independent of movie duration.
            sampled[target] = chosen.reformat(width=width, height=height, format="rgb24").to_ndarray()
    return np.stack([sampled[t] for t in requested])


def audio_window(path, start, end, rate, *, channels=2, padding="silence", cancel=check_cancel):
    """Decode an exact absolute-PTS interval to a bounded PCM sample window."""
    start, end = rational(start), rational(end)
    count = round(end * rate) - round(start * rate)
    if count <= 0:
        return np.zeros((channels, 0), dtype=np.float32)
    # Seek near the window, retaining PTS for atrim. -seek_timestamp handles
    # files whose first PTS is nonzero. Silence is an explicit missing-data policy.
    info = probe(path)
    if not info["audio"]:
        if padding != "silence":
            raise ValueError("Selected audio route has no source audio stream")
        return np.zeros((channels, count), dtype=np.float32)
    origin = rational(info["audio"].get("start_time", "0"))
    if padding == "error" and (start < origin or end > origin + rational(info["audio"].get("duration", info["duration"]))):
        raise ValueError("Audio selection extends beyond the source stream")
    with tempfile.TemporaryDirectory(prefix="h3-audio-") as directory:
        output = Path(directory) / "window.f32"
        # Async resampling fills a genuine stream-start offset with silence;
        # it never derives the audio clock from the video's nominal frame rate.
        filters = f"atrim=start={float(start):.12f}:end={float(end):.12f},asetpts=PTS-({float(start):.12f})/TB,aresample={rate}:async=1:first_pts=0,apad=whole_len={count},atrim=end_sample={count}"
        run_process(["ffmpeg", "-v", "error", "-nostdin", "-threads", "1", "-seek_timestamp", "1", "-ss", str(float(max(origin, start - 1))), "-copyts", "-protocol_whitelist", "file,pipe", "-i", str(path), "-vn", "-af", filters, "-ac", str(channels), "-ar", str(rate), "-f", "f32le", str(output)], cancel=cancel)
        pcm = np.fromfile(output, dtype="<f4")
    pcm = pcm[:count * channels]
    if pcm.size < count * channels:
        pcm = np.pad(pcm, (0, count * channels - pcm.size))
    return pcm.reshape(-1, channels).T.copy()


def mix_audio(p, assets, start, end, rate, route, *, cancel=check_cancel):
    """Deterministic sample-grid mix. Route is guide, final or reference."""
    from .model import active_clips
    a, b = sample_boundary(start, rate), sample_boundary(end, rate)
    mix = np.zeros((2, b-a), dtype=np.float32)
    routes = {"guide": {"guide", "both"}, "final": {"final", "both"}, "reference": {"reference"}}[route]
    for _, c in active_clips(p, start, end):
        if c["audio_role"] not in routes:
            continue
        left, right = max(start, c["start"]), min(end, c["end"])
        sa, sb = source_time(c, left), source_time(c, right)
        speed = rational(c["speed"])
        pcm = audio_window(assets.path(c["asset_id"]), min(sa, sb), max(sa, sb), rate, padding=c["padding"], cancel=cancel)
        if speed < 0:
            pcm = pcm[:, ::-1]
        n = sample_boundary(right, rate) - sample_boundary(left, rate)
        if speed != 1:
            # Explicit pitch-changing retime. No hidden stretch or pitch correction.
            pos = np.arange(n) * float(abs(speed))
            pcm = np.stack([np.interp(pos, np.arange(pcm.shape[1]), channel, left=0, right=0) for channel in pcm])
        pcm = pcm[:, :n]
        if pcm.shape[1] < n:
            pcm = np.pad(pcm, ((0,0),(0,n-pcm.shape[1])))
        absolute = np.arange(sample_boundary(left, rate), sample_boundary(right, rate))
        envelope = np.ones(n)
        for fade, distance in ((c.get("fade_in",0), absolute-sample_boundary(c["start"],rate)), (c.get("fade_out",0), sample_boundary(c["end"],rate)-absolute)):
            if fade:
                envelope *= np.clip(distance / max(1, round(float(rational(fade))*rate)), 0, 1)
        offset = sample_boundary(left,rate)-a
        mix[:, offset:offset+n] += pcm * envelope * float(rational(c.get("gain",1)))
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
        return json.loads((self.project_dir(project_id) / "project.json").read_text())

    def save(self, project, expected_revision):
        from .model import validate
        p = validate(project)
        with self._lock:
            file = self.project_dir(p["project_id"]) / "project.json"
            old = json.loads(file.read_text()) if file.exists() else {"revision": 0}
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

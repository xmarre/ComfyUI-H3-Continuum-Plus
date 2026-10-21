"""Streaming, PTS-preserving visual measurements. No model or audio access.

A small CPU descriptor is persisted per decoded frame; detector tuning and
reference selection reuse those measurements instead of decoding the film again.
"""
from __future__ import annotations

from collections import deque
from fractions import Fraction
from functools import lru_cache
import json
import math
import sqlite3
import zlib

import numpy as np
from PIL import Image

from .media import MEDIA_FORMATS
from .model import digest, rational

MEASUREMENT_VERSION = 2
DETECTOR_VERSION = 2
WIDTH, HEIGHT = 96, 54


def measure(rgb):
    # Exclude the usual subtitle band from boundary metrics. Reference quality
    # still evaluates the whole frame. This reduces, but cannot eliminate,
    # subtitle/occluder false positives.
    small = np.asarray(Image.fromarray(rgb).resize((WIDTH, HEIGHT), Image.Resampling.BILINEAR))
    crop = small[3:45, 4:-4].astype(np.float32) / 255
    gray = crop.mean(axis=2)
    exposure = float(np.mean((gray > .03) & (gray < .97)))
    gx, gy = np.diff(gray, axis=1), np.diff(gray, axis=0)
    sharpness = float(np.mean(gx * gx) + np.mean(gy * gy))
    hist = np.concatenate([np.histogram(crop[..., c], bins=16, range=(0, 1))[0]
                           for c in range(3)]).astype(np.float32)
    hist /= max(1, hist.sum())
    return small, {"luma": float(gray.mean()), "sharpness": sharpness,
                   "exposure": exposure, "hist": hist.tolist()}


def distance(left, right):
    """Spatial/chroma and illumination-normalized structure evidence."""
    a, b = left[3:45, 4:-4].astype(np.float32) / 255, right[3:45, 4:-4].astype(np.float32) / 255
    ag, bg = a.mean(2), b.mean(2)
    structure = float(np.mean(np.abs((ag - ag.mean()) - (bg - bg.mean()))))
    color = float(np.mean(np.abs(a - b)))
    ah = np.concatenate([np.histogram(a[..., c], bins=16, range=(0, 1))[0] for c in range(3)])
    bh = np.concatenate([np.histogram(b[..., c], bins=16, range=(0, 1))[0] for c in range(3)])
    hist = float(np.abs(ah / ah.sum() - bh / bh.sum()).sum() / 2)
    aligned = structure
    motion = None
    try:
        import cv2
        shift, response = cv2.phaseCorrelate(ag, bg)
        motion = {"dx": float(shift[0]), "dy": float(shift[1]), "response": float(response)}
        if response > .2 and abs(shift[0]) < 25 and abs(shift[1]) < 16:
            moved = cv2.warpAffine(ag, np.float32([[1, 0, shift[0]], [0, 1, shift[1]]]),
                                   (ag.shape[1], ag.shape[0]), borderMode=cv2.BORDER_REFLECT)
            delta = moved - bg
            aligned = min(structure, float(np.mean(np.abs(delta - delta.mean()))))
    except ImportError:
        pass
    return {"score": .5 * structure + .3 * color + .2 * hist,
            "structure": structure, "aligned": aligned, "histogram": hist,
            "luma_delta": abs(float(ag.mean() - bg.mean())), "motion": motion}


def open_measurements(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS frames (n INTEGER PRIMARY KEY, pts INTEGER, tb TEXT, duration TEXT, data BLOB, metrics TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
    return db


def _state(db, key, default=None):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _set(db, key, value):
    db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))


def decode_measurements(path, db, start, end, *, cancel, progress):
    """Resume after the last committed PTS. At most two small frames in RAM."""
    import av
    start, end = rational(start), rational(end)
    if _state(db, "complete", False):
        return _state(db, "summary")
    last = db.execute("SELECT n,pts,tb FROM frames ORDER BY n DESC LIMIT 1").fetchone()
    count = last[0] + 1 if last else 0
    resume = Fraction(last[1]) * rational(last[2]) if last else None
    with av.open(str(path), options={"protocol_whitelist": "file,pipe", "format_whitelist": MEDIA_FORMATS}) as container:
        stream = container.streams.video[0]
        stream.thread_type = "SLICE"
        seek = max(start, resume if resume is not None else start)
        container.seek(math.floor(seek / stream.time_base), stream=stream, backward=True)
        previous_time = resume

        def interval_frames():
            previous = None
            began = resume is not None
            for decoded in container.decode(stream):
                cancel()
                if decoded.pts is None:
                    raise ValueError("Scene analysis needs decoded presentation timestamps")
                when = Fraction(decoded.pts)*decoded.time_base
                if not began and when < start:
                    previous = decoded
                    continue
                if not began:
                    if previous is not None:
                        before = Fraction(previous.pts)*previous.time_base
                        if start-before <= when-start:
                            yield previous
                    began = True
                if when >= end:
                    break
                yield decoded

        try:
            for frame in interval_frames():
                cancel()
                if frame.pts is None:
                    raise ValueError("Scene analysis needs decoded presentation timestamps")
                when = Fraction(frame.pts) * frame.time_base
                if resume is not None and when <= resume:
                    continue
                if when >= end:
                    break
                if previous_time is not None and when <= previous_time:
                    raise ValueError("Scene analysis needs strictly increasing presentation timestamps")
                previous_time = when
                # Detection descriptors operate on encoded-domain SDR previews.
                # These pixels are never used as extracted H3 references.
                rgb = frame.reformat(width=WIDTH, height=HEIGHT, format="rgb24").to_ndarray()
                small, metrics = measure(rgb)
                duration = Fraction(frame.duration or 0) * frame.time_base
                if duration <= 0:
                    duration = 1 / (stream.average_rate or Fraction(24))
                db.execute("INSERT INTO frames VALUES (?,?,?,?,?,?)", (
                    count, frame.pts, str(frame.time_base), str(duration), zlib.compress(small.tobytes()), json.dumps(metrics)))
                count += 1
                if count % 128 == 0:
                    db.commit()
                    progress(stage="decode", frames=count, source_pts=str(when), fraction=float((when-start)/(end-start)))
            summary = {"frames": count, "start": str(start), "end": str(end),
                       "time_base": str(stream.time_base), "measurement_version": MEASUREMENT_VERSION}
            if not count:
                raise ValueError("Selected source interval contains no decoded frames")
            _set(db, "summary", summary)
            _set(db, "complete", True)
            db.commit()
            return summary
        except BaseException:
            # A cancelled job retains the last complete frame, never a partial
            # asset or a project edit. The next request can resume this DB.
            db.commit()
            raise


def frames(db, start=0, end=2**53):
    for n, pts, tb, duration, data, metrics in db.execute(
            "SELECT * FROM frames WHERE n>=? AND n<? ORDER BY n", (start, end)):
        yield {"n": n, "pts": pts, "time_base": tb, "time": Fraction(pts)*rational(tb),
               "duration": rational(duration), "image": np.frombuffer(zlib.decompress(data), np.uint8).reshape(HEIGHT, WIDTH, 3),
               **json.loads(metrics)}


def detect(db, settings, *, cancel):
    """Delayed adaptive cut detection, flash return veto, separate gradual events.

    Heuristics are evidence, not semantic classification. Ambiguous occlusions
    and whip pans stay reviewable; a VLM cannot change measured PTS.
    """
    sensitivity = settings["sensitivity"]
    threshold = .115 / sensitivity
    recent = deque(maxlen=24)
    window = deque(maxlen=5)
    events, last_cut = [], -100
    gradual = []
    anchor = None
    last_change = None
    def padded_frames():
        iterator = iter(frames(db))
        first = next(iterator, None)
        if first is None:
            return
        yield first
        yield first
        yield first
        last = first
        for item in iterator:
            last = item
            yield item
        yield last
        yield last

    for current in padded_frames():
        cancel()
        window.append(current)
        if len(window) < 5:
            continue
        prev2, prev, now, after, after2 = window
        change = distance(prev["image"], now["image"])
        score = change["score"]
        baseline = float(np.median(recent)) if recent else .005
        recent.append(score)
        n = now["n"]
        flash_return = distance(prev["image"], after["image"])["score"] < max(.025, score*.24)
        flash_return |= distance(prev["image"], after2["image"])["score"] < max(.025, score*.24)
        flash_return |= (distance(prev2["image"], now["image"])["score"] < .04
                         and distance(prev2["image"], prev["image"])["score"] > threshold)
        illumination_only = change["luma_delta"] > .12 and change["structure"] < .025
        motion_only = change["motion"] and change["motion"]["response"] > .3 and change["aligned"] < .025 and change["histogram"] < .12
        if (settings["detect_shots"] and score > threshold and score > baseline*2.7
                and n-last_cut >= settings["min_shot_frames"] and not flash_return and not illumination_only and not motion_only):
            events.append({"type": "hard_cut", "frame": n, "pts": now["pts"], "time_base": now["time_base"],
                           "confidence": min(.99, score/max(threshold, baseline*2.7)/2),
                           "evidence": {**change, "adaptive_baseline": baseline,
                                        "reason": "Persistent spatial/color discontinuity above local motion baseline"}})
            last_cut = n
            gradual = []
            anchor, last_change = now, now["time"]
        elif settings["detect_shots"]:
            # Gradual transitions retain their own interval, never a series of
            # hard cuts. Require a sustained luminance slope or visual blend.
            gradual.append((now, score))
            gradual = gradual[-24:]
            if len(gradual) >= 6:
                seq = [x[0] for x in gradual]
                slope = np.diff([x["luma"] for x in seq])
                fade = (max(x["luma"] for x in seq)-min(x["luma"] for x in seq) > .18
                        and (np.all(slope >= -.006) or np.all(slope <= .006))
                        and min(x["luma"] for x in seq) < .07)
                blend = (len(seq) >= 10 and all(.006 < s < threshold for _, s in gradual)
                         and distance(seq[0]["image"], seq[-1]["image"])["score"] > .20
                         and all(distance(a["image"], b["image"])["aligned"] > .012 for a, b in zip(seq, seq[1:])))
                if (fade or blend) and seq[0]["n"] > last_cut+2:
                    kind = "fade" if fade else "dissolve_candidate"
                    midpoint = seq[len(seq)//2]
                    events.append({"type": kind, "frame": midpoint["n"], "pts": midpoint["pts"],
                                   "time_base": midpoint["time_base"], "confidence": .65 if fade else .45,
                                   "transition": {"start_pts": seq[0]["pts"], "end_pts": now["pts"], "time_base": now["time_base"]},
                                   "evidence": {"reason": "Sustained luminance fade" if fade else "Sustained visual blend; motion/occlusion may look similar"}})
                    last_cut = midpoint["n"]
                    gradual = []
        if anchor is None:
            anchor, last_change = now, now["time"]
        elif settings["within_shot"] and now["time"]-last_change >= 2:
            delta = distance(anchor["image"], now["image"])
            if delta["score"] > .16/sensitivity and n-last_cut > 12:
                events.append({"type": "composition_change", "frame": n, "pts": now["pts"], "time_base": now["time_base"],
                               "confidence": .5, "evidence": {**delta, "reason": "Sustained within-shot composition change; subject/action needs review"}})
                anchor, last_change = now, now["time"]
    return sorted(events, key=lambda e: (e["frame"], e["type"]))


@lru_cache(maxsize=1)
def _face_classifier():
    try:
        import cv2
        from pathlib import Path
        file = Path(cv2.data.haarcascades)/"haarcascade_frontalface_default.xml"
        classifier = cv2.CascadeClassifier(str(file))
        return None if classifier.empty() else classifier
    except (ImportError, AttributeError):
        return None


def face_evidence(image):
    classifier = _face_classifier()
    if classifier is None:
        return None
    import cv2
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    faces = classifier.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(12, 12))
    return {"detected_frontal_faces": len(faces), "proxy_area": sum(int(w)*int(h) for _, _, w, h in faces)/(WIDTH*HEIGHT),
            "method": "CPU Haar on detection preview; absence is not evidence of no visible face"}


def select_candidates(db, start, end, count):
    """Bounded top-quality reservoir plus diversity, excluding transition edges."""
    buckets = {}
    midpoint = None
    for row in frames(db, start, end):
        if end-start > 8 and (row["n"] < start+2 or row["n"] >= end-2):
            continue
        row["quality"] = row["exposure"] * .6 + min(1, row["sharpness"]*80)*.4
        row["quality_metrics"] = {"sharpness": row["sharpness"], "exposure": row["exposure"],
                                  "face_visibility": None, "occlusion": None}
        bucket = min(23, (row["n"]-start)*24//max(1, end-start))
        old = buckets.get(bucket)
        if old is None or (row["quality"], -abs(row["n"]-(start+end)/2)) > (old["quality"], -abs(old["n"]-(start+end)/2)):
            buckets[bucket] = row
        if row["n"] == (start+end)//2:
            midpoint = row
    reservoir = list(buckets.values())
    if not reservoir:
        return []
    if midpoint is not None:
        reservoir.append(midpoint)
    for row in reservoir:
        faces = face_evidence(row["image"])
        row["quality_metrics"]["face_visibility"] = faces
        if faces:
            row["quality"] += min(.15, faces["proxy_area"]*.6)
    reservoir.sort(key=lambda x: (-x["quality"], abs(x["n"]-(start+end)/2), x["n"]))
    chosen = [reservoir[0]]
    for row in reservoir[1:]:
        if len(chosen) >= count:
            break
        if all(abs(row["time"]-old["time"]) >= Fraction(1, 2)
               and distance(row["image"], old["image"])["score"] > .10 for old in chosen):
            chosen.append(row)
    return chosen


def setup_match(candidate, prior):
    d = distance(candidate, prior)
    return d["score"] < .042 and d["structure"] < .035 and d["histogram"] < .09


def analysis_backend():
    import av
    import PIL
    try:
        import cv2
        vision = cv2.__version__
    except ImportError:
        vision = None
    return {"pyav": av.__version__, "libav": av.library_versions, "pillow": PIL.__version__, "opencv": vision}


def measurement_key(asset_id, start, end):
    return digest([MEASUREMENT_VERSION, asset_id, str(rational(start)), str(rational(end)), WIDTH, HEIGHT, analysis_backend()["pyav"], analysis_backend()["libav"]])


def reuse_measurements(source_db, destination, start, end, *, cancel):
    """Reuse a completed superset interval without touching the video decoder."""
    if _state(destination, "complete", False):
        return False
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    try:
        summary = _state(source, "summary")
        if not _state(source, "complete", False) or not summary or rational(summary["start"]) > start or rational(summary["end"]) < end:
            return False
        destination.execute("DELETE FROM frames")
        count = 0
        previous = None
        began = False
        for row in source.execute("SELECT * FROM frames ORDER BY n"):
            cancel()
            _, pts, tb, duration, data, metrics = row
            when = Fraction(pts)*rational(tb)
            if not began and when < start:
                previous = row
                continue
            if not began:
                if previous is not None:
                    before = Fraction(previous[1])*rational(previous[2])
                    if start-before <= when-start:
                        destination.execute("INSERT INTO frames VALUES (?,?,?,?,?,?)", (count, *previous[1:]))
                        count += 1
                began = True
            if when >= end:
                break
            destination.execute("INSERT INTO frames VALUES (?,?,?,?,?,?)", (count, pts, tb, duration, data, metrics))
            count += 1
        if not count:
            return False
        _set(destination, "summary", {**summary, "start": str(start), "end": str(end), "frames": count})
        _set(destination, "complete", True)
        destination.commit()
        return True
    except BaseException:
        destination.rollback()
        raise
    finally:
        source.close()

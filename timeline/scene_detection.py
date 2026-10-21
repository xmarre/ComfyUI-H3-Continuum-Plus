"""Streaming, PTS-preserving visual measurements. No model or audio access.

A small CPU descriptor is persisted per decoded frame; detector tuning and
reference selection reuse those measurements instead of decoding the film again.
"""
from __future__ import annotations

from collections import deque
from fractions import Fraction
import json
import math
import sqlite3
import zlib

import numpy as np
from PIL import Image

from .media import MEDIA_FORMATS
from .model import digest, rational

MEASUREMENT_VERSION = 1
DETECTOR_VERSION = 1
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
        try:
            for frame in container.decode(stream):
                cancel()
                if frame.pts is None:
                    raise ValueError("Scene analysis needs decoded presentation timestamps")
                when = Fraction(frame.pts) * frame.time_base
                if when < start or (resume is not None and when <= resume):
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
    for current in frames(db):
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


def select_candidates(db, start, end, count):
    """Bounded top-quality reservoir plus diversity, excluding transition edges."""
    reservoir = []
    for row in frames(db, start, end):
        if end-start > 8 and (row["n"] < start+2 or row["n"] >= end-2):
            continue
        row["quality"] = row["exposure"] * .6 + min(1, row["sharpness"]*80)*.4
        # No claim of facial visibility without an actual face detector.
        row["quality_metrics"] = {"sharpness": row["sharpness"], "exposure": row["exposure"],
                                  "face_visibility": None, "occlusion": None}
        if len(reservoir) < 24:
            reservoir.append(row)
        else:
            worst = min(range(len(reservoir)), key=lambda i: reservoir[i]["quality"])
            if row["quality"] > reservoir[worst]["quality"]:
                reservoir[worst] = row
        # Retain temporally distinct witnesses alongside the quality reservoir.
        if row["n"] == (start+end)//2:
            midpoint = row
    if not reservoir:
        return []
    if 'midpoint' in locals():
        reservoir.append(midpoint)
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


def measurement_key(asset_id, start, end):
    return digest([MEASUREMENT_VERSION, asset_id, str(rational(start)), str(rational(end)), WIDTH, HEIGHT])

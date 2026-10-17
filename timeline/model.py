"""Versioned media-light project contract and exact logical schedule.

All project intervals are half-open integer output frames. Source timestamps
are rational *absolute stream PTS seconds*, not decoded frame indices.
"""
from __future__ import annotations

import copy
from fractions import Fraction
import hashlib
import json
import re
import uuid

SCHEMA = "h3_continuum_timeline_project_v1"
ROLES = {"retained", "video_guide", "image_reference", "audio", "prompt", "generated"}
AUDIO_ROLES = {"neither", "guide", "final", "both", "reference"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def rational(value):
    result = Fraction(str(value))
    if abs(result.numerator) > 2**53 or result.denominator > 10**9:
        raise ValueError("Timeline rational is outside the supported precision")
    return result


def sample_boundary(frame, rate, fps=Fraction(24)):
    return round(Fraction(int(frame) * int(rate), 1) / fps)


def blank_project():
    return {"schema": SCHEMA, "project_id": uuid.uuid4().hex, "name": "Untitled timeline",
            "fps": "24/1", "duration_frames": 504, "chunk_frames": 168,
            "width": 768, "height": 512, "operation": "generate", "assets": [],
            "tracks": [], "prompts": {"global": "", "mode": "Fixed", "script": ""},
            "selection": [0, 504], "markers": [], "takes": [], "ui": {}}


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum or value > 2**53 - 1:
        raise ValueError(f"Timeline {name} must be an integer >= {minimum}")
    return value


def validate(project):
    p = copy.deepcopy(project)
    if not isinstance(p, dict) or p.get("schema") != SCHEMA:
        raise ValueError("Unsupported Timeline Studio project schema")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(p.get("project_id", ""))):
        raise ValueError("Invalid Timeline Studio project identifier")
    if rational(p.get("fps", "24")) != 24:
        raise ValueError("H3 Timeline Studio output requires 24 fps")
    duration = integer(p.get("duration_frames"), "duration", 1)
    integer(p.get("chunk_frames"), "chunk length", 1)
    for key in ("width", "height"):
        if integer(p.get(key), key, 32) % 32:
            raise ValueError(f"Timeline {key} must be a multiple of 32")
    if p.get("operation") not in {"generate", "recreate", "replace"}:
        raise ValueError("Unknown timeline generation operation")
    assets = p.setdefault("assets", [])
    ids = set()
    for a in assets:
        if a.get("id") in ids or not re.fullmatch(r"[a-f0-9]{64}", str(a.get("id", ""))):
            raise ValueError("Assets require unique registered SHA256 identifiers")
        ids.add(a["id"])
    seen = set()
    for t in p.setdefault("tracks", []):
        if t.get("id") in seen:
            raise ValueError("Duplicate timeline track identifier")
        seen.add(t.get("id"))
        if t.get("role") not in ROLES:
            raise ValueError("Unknown timeline track role")
        clips = set()
        for c in t.setdefault("clips", []):
            if c.get("id") in clips:
                raise ValueError("Duplicate clip identifier within a track")
            clips.add(c.get("id"))
            start, end = integer(c.get("start"), "clip start"), integer(c.get("end"), "clip end", 1)
            if start >= end or end > duration:
                raise ValueError("Clip range must satisfy 0 <= start < end <= project duration")
            if t["role"] != "prompt" and c.get("asset_id") not in ids:
                raise ValueError("Clip refers to missing registered media; relink the asset")
            rational(c.setdefault("source_in", "0/1"))
            speed = rational(c.setdefault("speed", "1/1"))
            if speed == 0 or abs(speed) > 16:
                raise ValueError("Clip speed must be nonzero and between -16 and 16")
            if c.setdefault("padding", "error") not in {"error", "hold", "silence"}:
                raise ValueError("Unknown clip end policy")
            if c.setdefault("audio_role", "neither") not in AUDIO_ROLES:
                raise ValueError("Unknown audio route")
            for key in ("gain", "fade_in", "fade_out"):
                value = rational(c.get(key, 1 if key == "gain" else 0))
                if value < 0 or (key == "gain" and value > 16):
                    raise ValueError(f"Invalid clip {key}")
    a, b = p.setdefault("selection", [0, duration])
    if integer(a, "selection start") >= integer(b, "selection end", 1) or b > duration:
        raise ValueError("Render selection is outside the timeline")
    p.setdefault("prompts", {"global": "", "script": "", "mode": "Fixed"})
    p.setdefault("takes", [])
    p.setdefault("markers", [])
    p.setdefault("ui", {})
    return p


def active_clips(p, start, end, roles=None):
    """Track order is bottom-to-top. Hidden affects display; muted affects output."""
    result = []
    for t in p["tracks"]:
        if t.get("muted") or (roles is not None and t["role"] not in roles):
            continue
        for c in t["clips"]:
            if c["start"] < end and start < c["end"] and not c.get("muted"):
                result.append((t, c))
    return result


def source_time(c, frame):
    return rational(c["source_in"]) + Fraction(frame - c["start"], 24) * rational(c["speed"])


def clip_input(p, t, c, start, end):
    assets = {a["id"]: a for a in p["assets"]}
    a, b = max(start, c["start"]), min(end, c["end"])
    # Labels, lock/visibility and inspector state are deliberately nonsemantic.
    return {"role": t["role"], "asset_id": c.get("asset_id"), "range": [a, b],
            "source": [str(source_time(c, a)), str(source_time(c, b))],
            "speed": str(rational(c["speed"])), "padding": c["padding"],
            "audio_role": c["audio_role"], "gain": str(rational(c.get("gain", 1))),
            "fade_in": c.get("fade_in", 0), "fade_out": c.get("fade_out", 0),
            "clip_range": [c["start"], c["end"]], "alias": c.get("alias", assets.get(c.get("asset_id"), {}).get("alias", "")),
            "text": c.get("text", ""), "override": bool(c.get("override"))}


def compile_window(p, index):
    size = p["chunk_frames"]
    start, end = index * size, min((index + 1) * size, p["duration_frames"])
    inputs = [clip_input(p, t, c, start, end) for t, c in active_clips(p, start, end)]
    images, seen = [], set()
    for i in inputs:
        if i["role"] == "image_reference" and i["asset_id"] not in seen:
            seen.add(i["asset_id"])
            images.append(i)
    if len(images) > 8:
        raise ValueError(f"Chunk {index + 1} exceeds H3's eight active reference images")
    aliases = {i["alias"]: f"<Picture {n}>" for n, i in enumerate(images, 1) if i["alias"]}
    prompts = p["prompts"]
    text = str(prompts.get("global", ""))
    # Reuse existing Fixed/List/Timeline parser, including permissive fallback.
    script = str(prompts.get("script", ""))
    if script:
        from ..v2.prompts import make_prompt_plan
        # The existing parser has a 16-chunk API. Resolve only the local batch;
        # timeline prompt clips provide unrestricted project-length authoring.
        batch = index // 16
        count = min(16, (p["duration_frames"] + size - 1) // size - batch * 16)
        plan = make_prompt_plan(mode=prompts.get("mode", "Fixed"), script=script,
                                chunks=count, chunk_seconds=size / 24)
        text += "\n" + plan["prompts"][index % 16]
    for i in inputs:
        if i["role"] == "prompt":
            body = i["text"]
            if i["override"]:
                text = str(prompts.get("global", "")) + "\n" + body
            else:
                a, b = i["range"]
                text += f"\n[{float(Fraction(a-start,24)):.6f}-{float(Fraction(b-start,24)):.6f}s] {body}"
    text = re.sub(r"@([\w-]+)", lambda m: aliases.get(m[1], m[0]), text).strip()
    guide = [i for i in inputs if i["audio_role"] in {"guide", "both"}]
    final = [i for i in inputs if i["audio_role"] in {"final", "both"}]
    semantic = {"prompt": text, "images": images, "video": [i for i in inputs if i["role"] == "video_guide"],
                "guide": guide, "voice": [i for i in inputs if i["audio_role"] == "reference"]}
    return {"index": index, "range": [start, end], **semantic, "aliases": aliases,
            "generation_hash": digest(semantic), "final_hash": digest(final)}


def compile_project(project):
    p = validate(project)
    size = p["chunk_frames"]
    a, b = p["selection"]
    # Selected intervals may cut inside a chunk. Render its complete causal
    # input window; the composer only replaces the selected intersection.
    first, stop = a // size, (b + size - 1) // size
    jobs = []
    for start in range(first, stop, 16):
        windows = [compile_window(p, i) for i in range(start, min(stop, start + 16))]
        jobs.append({"start_chunk": start, "range": [windows[0]["range"][0], windows[-1]["range"][1]],
                     "windows": windows, "chain": f"{p['project_id']}:{first}", "requires_previous_job": start > first})
    return {"project": p, "jobs": jobs, "snapshot_hash": digest(p),
            "capabilities": {"appearance_references": True, "native_first_last": True,
                             "internal_keyframes": False, "hot_lora_switching": False,
                             "export": "lossless FFV1/PCM or H.264/AAC re-encode; no stream copy"}}


def causal_hashes(windows, settings, previous="origin"):
    result = []
    for window in windows:
        previous = digest([previous, window["generation_hash"], settings])
        result.append(previous)
    return result

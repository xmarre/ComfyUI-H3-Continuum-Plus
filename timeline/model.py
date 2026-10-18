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
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def rational(value):
    result = Fraction(str(value))
    if "/" not in str(value):
        result = result.limit_denominator(10**9)
    if abs(result.numerator) > 2**53 or result.denominator > 10**9:
        raise ValueError("Timeline rational is outside the supported precision")
    return result


def seconds_text(frames):
    """Stable decimal seconds for prompt headers; equal frames, equal strings."""
    value = Fraction(int(frames), 24)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def sample_boundary(frame, rate, fps=Fraction(24)):
    return round(Fraction(int(frame) * int(rate), 1) / fps)


def blank_project():
    return {
        "schema": SCHEMA,
        "project_id": uuid.uuid4().hex,
        "name": "Untitled timeline",
        "fps": "24/1",
        "duration_frames": 504,
        "chunk_frames": 168,
        "width": 768,
        "height": 512,
        "operation": "generate",
        "assets": [],
        "tracks": [],
        "prompts": {"global": "", "mode": "Fixed", "script": ""},
        "selection": [0, 504],
        "markers": [],
        "takes": [],
        "ui": {},
    }


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
    if not 96 <= p["chunk_frames"] <= 360:
        raise ValueError("Continuum chunk duration must be between 4 and 15 seconds")
    for key in ("width", "height"):
        if integer(p.get(key), key, 32) % 32:
            raise ValueError(f"Timeline {key} must be a multiple of 32")
    if p.get("operation") not in {"generate", "recreate", "replace"}:
        raise ValueError("Unknown timeline generation operation")
    assets = p.setdefault("assets", [])
    ids = set()
    for a in assets:
        if a.get("id") in ids or not re.fullmatch(
            r"[a-f0-9]{64}", str(a.get("id", ""))
        ):
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
            start, end = (
                integer(c.get("start"), "clip start"),
                integer(c.get("end"), "clip end", 1),
            )
            if start >= end or end > duration:
                raise ValueError(
                    "Clip range must satisfy 0 <= start < end <= project duration"
                )
            if t["role"] != "prompt" and c.get("asset_id") not in ids:
                raise ValueError(
                    "Clip refers to missing registered media; relink the asset"
                )
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
    for marker in p["markers"]:
        frame = integer(marker.get("frame"), "marker frame")
        end = integer(marker.get("end", frame + 1), "region end", 1)
        if not frame < end <= duration:
            raise ValueError("Marker or named region is outside the project")
    for r in p.setdefault("accepted_windows", []):
        if not isinstance(r, dict):
            raise ValueError("Invalid accepted take window")
        first = integer(r.get("first_chunk"), "accepted first chunk")
        if integer(r.get("stop_chunk"), "accepted stop chunk", 1) <= first:
            raise ValueError("Accepted take window must span at least one chunk")
        take = r.get("take_id")
        parent = r.get("parent_take")
        if not (isinstance(take, str) and re.fullmatch(r"[0-9a-f]{32}", take)) or not (
            parent is None
            or (isinstance(parent, str) and re.fullmatch(r"[0-9a-f]{32}", parent))
        ):
            raise ValueError("Accepted take window requires take identifiers")
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
    return rational(c["source_in"]) + Fraction(frame - c["start"], 24) * rational(
        c["speed"]
    )


def clip_input(p, t, c, start, end):
    assets = {a["id"]: a for a in p["assets"]}
    a, b = max(start, c["start"]), min(end, c["end"])
    # Labels, lock/visibility and inspector state are deliberately nonsemantic.
    return {
        "role": t["role"],
        "asset_id": c.get("asset_id"),
        "range": [a, b],
        "source": [str(source_time(c, a)), str(source_time(c, b))],
        "speed": str(rational(c["speed"])),
        "padding": c["padding"],
        "audio_role": c["audio_role"],
        "gain": str(rational(c.get("gain", 1))),
        "fade_in": c.get("fade_in", 0),
        "fade_out": c.get("fade_out", 0),
        "clip_range": [c["start"], c["end"]],
        "alias": (c.get("alias") or assets.get(c.get("asset_id"), {}).get("alias", "")),
        "text": c.get("text", ""),
        "override": bool(c.get("override")),
    }


def compile_window(p, index, guide_scope=None):
    size = p["chunk_frames"]
    start, end = index * size, min((index + 1) * size, p["duration_frames"])
    active = active_clips(p, start, end)
    inputs = [clip_input(p, t, c, start, end) for t, c in active]
    if p["operation"] == "recreate" and not any(
        i["role"] == "video_guide" for i in inputs
    ):
        # Recreate explicitly interprets the selected original footage as model
        # guidance. A retained clip can opt out for audio-only spans.
        inputs.extend(
            {
                **clip_input(p, t, c, start, end),
                "role": "video_guide",
                "audio_role": "neither",
            }
            for t, c in active
            if t["role"] == "retained" and not c.get("guide_disabled")
        )
    images, seen = [], set()
    for i in inputs:
        if i["role"] == "image_reference" and i["asset_id"] not in seen:
            seen.add(i["asset_id"])
            images.append(i)
    if len(images) > 8:
        raise ValueError(
            f"Chunk {index + 1} exceeds H3's eight active reference images"
        )
    picture_by_asset = {
        i["asset_id"]: f"<Picture {n}>" for n, i in enumerate(images, 1)
    }
    aliases = {
        i["alias"]: picture_by_asset[i["asset_id"]]
        for i in inputs
        if i["role"] == "image_reference" and i["alias"]
    }
    prompts = p["prompts"]
    global_text = str(prompts.get("global", ""))
    base = global_text
    # Timed items are (start_frame, end_frame, body, override) in project frames.
    timed = []
    # Reuse existing Fixed/List/Timeline parser, including permissive fallback.
    script = str(prompts.get("script", ""))
    if script:
        from ..v2.prompts import (
            resolve_prompt_mode,
            parse_timeline_sections,
            timeline_preamble,
            _list_values,
        )
        from ..constants import PROMPT_MODE_LIST, PROMPT_MODE_TIMELINE

        try:
            mode = resolve_prompt_mode(prompts.get("mode", "Fixed"), script)
            if mode == PROMPT_MODE_LIST:
                values = _list_values(script)
                base += "\n" + values[min(index, len(values) - 1)]
            elif mode == PROMPT_MODE_TIMELINE:
                sections = parse_timeline_sections(script)
                exact = [
                    s
                    for s in sections
                    if (s["kind"] == "chunk" and s["index"] == index + 1)
                    or (
                        s["kind"] == "time"
                        and rational(s["start"]) == Fraction(start, 24)
                        and rational(s["end"]) == Fraction(end, 24)
                    )
                ]
                selected = exact or [
                    s
                    for s in sections
                    if s["kind"] == "time"
                    and rational(s["start"]) < Fraction(end, 24)
                    and Fraction(start, 24) < rational(s["end"])
                ]
                base += "\n" + timeline_preamble(script)
                if exact:
                    from ..v2.physical_prompts import inspect_strict_inner_ranges

                    inner = inspect_strict_inner_ranges(
                        {**exact[0], "kind": "time"},
                        outer_start=Fraction(start, 24),
                        outer_end=Fraction(end, 24),
                    )
                    if inner:
                        timed.extend(
                            (
                                round(rational(s["start"]) * 24),
                                round(rational(s["end"]) * 24),
                                s["body"],
                                False,
                            )
                            for s in inner
                        )
                    else:
                        base += "\n" + exact[0]["body"]
                elif selected:
                    for section in selected:
                        a = max(start, round(rational(section["start"]) * 24))
                        b = min(end, round(rational(section["end"]) * 24))
                        if a < b:
                            timed.append((a, b, section["body"], False))
                else:
                    preceding = [
                        s
                        for s in sections
                        if (s["kind"] == "chunk" and s["index"] <= index + 1)
                        or (
                            s["kind"] == "time"
                            and rational(s["end"]) <= Fraction(start, 24)
                        )
                    ]
                    base += "\n" + (preceding[-1] if preceding else sections[0])["body"]
            else:
                base += "\n" + script
        except (ValueError, TypeError, IndexError, KeyError):
            # Same permissive opaque fallback as existing prompt plans.
            base += "\n" + script
    for i in inputs:
        if i["role"] == "prompt":
            timed.append((*i["range"], i["text"], i["override"]))
    authored = "\n".join([global_text, script, *(t[2] for t in timed)])

    def resolve(value):
        return re.sub(r"@([\w-]+)", lambda m: aliases.get(m[1], m[0]), value).strip()

    segments = None
    if timed:
        # A strict contiguous partition of the window. An override replaces the
        # inherited script text inside its own span; the global prompt remains.
        cuts = sorted({start, end, *(x for a, b, *_ in timed for x in (a, b))})
        segments = []
        for a, b in zip(cuts, cuts[1:]):
            active = [t for t in timed if t[0] < b and a < t[1]]
            head = global_text if any(t[3] for t in active) else base
            body = resolve("\n".join([head, *(t[2] for t in active)]))
            if segments and (not body or body == segments[-1]["text"]):
                segments[-1]["range"][1] = b
            else:
                segments.append({"range": [a, b], "text": body})
        if len(segments) > 1 and not segments[0]["text"]:
            segments[1]["range"][0] = segments[0]["range"][0]
            segments.pop(0)
        if len(segments) == 1:
            base, segments, timed = segments[0]["text"], None, []
    # Nominal text keeps window-local times for the non-physical transport.
    text = resolve(
        "\n".join(
            [base]
            + [
                f"{seconds_text(a - start)}-{seconds_text(b - start)}s:\n{body}"
                for a, b, body, _ in timed
            ]
        )
    )
    warnings = []
    known = {
        str(x.get("alias"))
        for x in [*p["assets"], *(c for t in p["tracks"] for c in t["clips"])]
        if x.get("alias")
    }
    for name in sorted(set(re.findall(r"@([\w-]+)", authored)) & known - set(aliases)):
        warnings.append(
            f"@{name} has no active image reference in chunk {index + 1}; the text is kept as written"
        )
    by_alias = {}
    for i in inputs:
        if i["role"] == "image_reference" and i["alias"]:
            by_alias.setdefault(i["alias"], set()).add(i["asset_id"])
    for name, assets_for_alias in sorted(by_alias.items()):
        if len(assets_for_alias) > 1:
            warnings.append(
                f"@{name} names {len(assets_for_alias)} different active images in chunk {index + 1}; it resolves to {aliases[name]}"
            )

    def audio_identity(i):
        return {
            k: v
            for k, v in i.items()
            if k not in {"audio_role", "role", "alias", "text", "override"}
        }

    guide = [audio_identity(i) for i in inputs if i["audio_role"] in {"guide", "both"}]
    final = [i for i in inputs if i["audio_role"] in {"final", "both"}]
    images = [{"asset_id": i["asset_id"], "alias": i["alias"]} for i in images]
    video = [
        {
            k: v
            for k, v in i.items()
            if k in {"asset_id", "range", "source", "speed", "padding"}
        }
        for i in inputs
        if i["role"] == "video_guide"
    ]
    # Guide audio is encoded full-context over each bounded native job. Until
    # a smaller receptive-field/phase contract is proven on the actual H3 VAE,
    # a guide edit anywhere in that encoded scope invalidates the job's
    # windows. Without a scope (records predating it) the scope is the project.
    scope = guide_scope or (0, p["duration_frames"])
    full_guide = [
        audio_identity(clip_input(p, t, c, *scope))
        for t, c in active_clips(p, *scope)
        if c["audio_role"] in {"guide", "both"}
    ]
    if guide_scope is None:
        scope_identity = [p["duration_frames"], p["chunk_frames"], full_guide]
    else:
        scope_identity = [list(scope), p["chunk_frames"], full_guide]
    covered = sorted(tuple(i["range"]) for i in inputs if i["role"] == "video_guide")
    cursor, gaps = start, 0
    for a, b in covered:
        gaps += max(0, a - cursor)
        cursor = max(cursor, b)
    gaps += max(0, end - cursor)
    if covered and gaps:
        warnings.append(
            f"Video guidance covers {end - start - gaps} of {end - start} frames in chunk {index + 1}; uncovered frames hold the nearest guide frame"
        )
    semantic = {
        "audio_encoder_scope_hash": digest(["exact_audio_scope_v2", scope_identity])
        if full_guide
        else "none",
        "prompt": text,
        "segments": segments,
        "images": images,
        "video": video,
        "guide": guide,
        "voice": [audio_identity(i) for i in inputs if i["audio_role"] == "reference"],
    }
    return {
        "index": index,
        "range": [start, end],
        "timed_prompt": segments is not None,
        "warnings": warnings,
        **semantic,
        "aliases": aliases,
        "generation_hash": digest(
            {**semantic, "images": [i["asset_id"] for i in images]}
        ),
        "final": final,
        "final_hash": digest(final),
    }


def guide_scope_for(p, indices, offset=None):
    """Encoder interval; runtime supplies the actual causal output offset."""
    size = p["chunk_frames"]
    a = indices[0] * size
    b = min(p["duration_frames"], (indices[-1] + 1) * size)
    origin = a if offset is None else offset
    return (origin, origin + b - a + 34)


def compile_project(project):
    p = validate(project)
    # Editor view state is not part of the request-local plan or its identity.
    p.pop("ui", None)
    size = p["chunk_frames"]
    a, b = p["selection"]
    # Selected intervals may cut inside a chunk. Render its complete causal
    # input window; the composer only replaces the selected intersection.
    first, stop = a // size, (b + size - 1) // size
    # Accepted native prefixes are included for reuse, even when the requested
    # replacement begins inside their job. Only the selection is composited.
    reuse = next(
        (
            r
            for r in p.get("accepted_windows", [])
            if r["first_chunk"] <= first < r["stop_chunk"]
        ),
        None,
    )
    if reuse is not None:
        first = reuse["first_chunk"]
        stop = max(
            stop, min(reuse["stop_chunk"], (p["duration_frames"] + size - 1) // size)
        )
    jobs = []
    for start in range(first, stop, 16):
        indices = range(start, min(stop, start + 16))
        scope = guide_scope_for(p, indices)
        windows = [compile_window(p, i, guide_scope=scope) for i in indices]
        jobs.append(
            {
                "start_chunk": start,
                "guide_scope": list(scope),
                "range": [windows[0]["range"][0], windows[-1]["range"][1]],
                "windows": windows,
                "chain": f"{p['project_id']}:{first}",
                "requires_previous_job": start > first,
                "resume_take_id": reuse["take_id"]
                if reuse is not None and start == first
                else None,
                "resume_parent_take": reuse.get("parent_take")
                if reuse is not None and start == first
                else None,
            }
        )
    return {
        "project": p,
        "jobs": jobs,
        "snapshot_hash": digest(p),
        "capabilities": {
            "appearance_references": True,
            "native_first_last": True,
            "internal_keyframes": False,
            "hot_lora_switching": False,
            "export": "lossless FFV1/PCM or H.264/AAC re-encode; no stream copy",
        },
    }


def causal_hashes(windows, settings, previous="origin"):
    result = []
    for window in windows:
        previous = digest([previous, window["generation_hash"], settings])
        result.append(previous)
    return result

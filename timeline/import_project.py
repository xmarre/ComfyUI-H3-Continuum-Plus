"""Preview/apply operations on copied projects; imports never edit live state."""

from __future__ import annotations

import copy
import uuid
from fractions import Fraction

from .model import blank_project, rational, validate
from .prompt_regions import migrate_chunk_prompts, replace_regions


def prompt_mapping(document, project, time_origin="auto"):
    origin = project.get("chunk_origin_frame", 0)
    size = project["chunk_frames"]
    stop = project["selection"][1]
    if time_origin not in {"auto", "scene", "movie"}:
        raise ValueError("Choose scene-relative or movie-absolute prompt times")
    timed = [
        (rational(e["start_seconds"]) * 24, rational(e["end_seconds"]) * 24)
        for e in document.get("entries", [])
        if "start_seconds" in e
    ]
    used = time_origin
    if used == "auto":
        absolute = timed and all(origin <= a < b <= stop for a, b in timed)
        local = any(0 <= a < b <= stop - origin for a, b in timed)
        used = "movie" if absolute and not local else "scene"
    offset = origin if used == "scene" else 0
    incoming = []
    surplus = []
    notes = [
        f"Prompt times use {used}-"
        + (
            "relative seconds from the scene start."
            if used == "scene"
            else "absolute seconds from movie frame zero."
        )
    ]
    for i, entry in enumerate(document.get("entries", [])):
        if "start_seconds" in entry:
            raw_a = offset + rational(entry["start_seconds"]) * 24
            raw_b = offset + rational(entry["end_seconds"]) * 24
            a, b = round(raw_a), round(raw_b)
            if raw_a != a or raw_b != b:
                notes.append(
                    f"Entry {i + 1}: source seconds [{entry['start_seconds']}, {entry['end_seconds']}) map to 24-fps frames [{a}, {b}); rounding is at most half a frame."
                )
            if (a - origin) % size or (b - origin) % size:
                notes.append(
                    f"Entry {i + 1} does not match the current {size}-frame chunk grid; it becomes timed inner sections."
                )
        else:
            a = origin + (entry["chunk"] - 1) * size
            b = a + size
        left, right = max(origin, a), min(stop, b)
        if a < origin or b > stop:
            surplus.append(
                {
                    "entry": i + 1,
                    "range": [a, b],
                    "text": entry["text"],
                    "applied_range": [left, right] if left < right else None,
                }
            )
        if left < right:
            region = {
                "id": uuid.uuid4().hex,
                "start": left,
                "end": right,
                "text": entry["text"],
                "entry": i + 1,
            }
            if "chunk" in entry and (left, right) == (a, b):
                region["chunk_index"] = entry["chunk"] - 1
            incoming.append(region)
    incoming.sort(key=lambda e: (e["start"], e["end"]))
    if any(a["end"] > b["start"] for a, b in zip(incoming, incoming[1:])):
        raise ValueError(
            "Imported entries overlap on the current scene grid; fix the source timing before applying"
        )
    rows = []
    gaps = []
    uncovered = []
    for index, start in enumerate(range(origin, stop, size)):
        end = min(stop, start + size)
        parts = [
            {
                "range": [max(start, e["start"]), min(end, e["end"])],
                "text": e["text"],
                "entry": e["entry"],
            }
            for e in incoming
            if e["start"] < end and start < e["end"]
        ]
        cursor = start
        for part in parts:
            if part["range"][0] > cursor:
                gaps.append([cursor, part["range"][0]])
            cursor = part["range"][1]
        if cursor < end:
            gaps.append([cursor, end])
        if not parts:
            uncovered.append(index + 1)
        rows.append(
            {
                "chunk": index + 1,
                "range": [start, end],
                "seconds": [str(Fraction(start, 24)), str(Fraction(end, 24))],
                "parts": parts,
                "uncovered": not parts,
            }
        )
    if len(document.get("entries", [])) != len(rows):
        notes.append(
            f"{len(document.get('entries', []))} imported entries map onto {len(rows)} current scene chunks; uncovered chunks retain their existing prompts."
        )
    return {
        "regions": incoming,
        "rows": rows,
        "surplus": surplus,
        "uncovered": uncovered,
        "gaps": gaps,
        "notes": notes,
        "time_origin": used,
    }


def apply_components(
    current,
    document,
    media,
    selected,
    target="merge",
    provenance=None,
    soundtrack_mode="replace",
    time_origin="auto",
):
    """Return candidate plus mapping. Preview owns confirmation, caller owns Undo."""
    validate(current)
    if target not in {"merge", "replace"}:
        raise ValueError("Choose merge or replace scene")
    if target == "replace" and not (
        selected.get("video") or selected.get("soundtrack")
    ):
        raise ValueError("Replace scene is available only when media is selected")
    if soundtrack_mode not in {"replace", "add"}:
        raise ValueError("Choose replace or add soundtrack")
    p = migrate_chunk_prompts(copy.deepcopy(current))
    videos = (
        [m for m in media if m["component"] == "video"] if selected.get("video") else []
    )
    sounds = (
        [m for m in media if m["component"] == "soundtrack"]
        if selected.get("soundtrack")
        else []
    )
    current = p
    if target == "replace":
        p = blank_project()
        for key in ("project_id", "revision", "width", "height"):
            if key in current:
                p[key] = copy.deepcopy(current[key])
        p["prompts"] = copy.deepcopy(current.get("prompts", {}))
        p["prompt_source"] = copy.deepcopy(current.get("prompt_source", {}))
        # Media-only replacement preserves prompt tracks and their authored span.
        if not selected.get("chunks"):
            p["tracks"] = [
                copy.deepcopy(t) for t in current["tracks"] if t["role"] == "prompt"
            ]
        spans = [m["end"] for m in (videos or sounds)]
        spans += [c["end"] for t in p["tracks"] for c in t["clips"]]
        p["duration_frames"] = max(spans or [current["duration_frames"]])
        p["chunk_frames"] = current["chunk_frames"]
        durations = {m["end"] - m["start"] for m in videos[:-1] or videos}
        if len(durations) == 1 and 96 <= next(iter(durations)) <= 360:
            p["chunk_frames"] = next(iter(durations))
        p["selection"] = [0, p["duration_frames"]]
        p["name"] = "Imported scene"
    origin = p.get("chunk_origin_frame", 0) if target == "merge" else 0
    replacing_audio = (
        selected.get("soundtrack")
        and soundtrack_mode == "replace"
        and (bool(sounds) or any(m["asset"].get("audio") for m in videos))
    )
    if replacing_audio:
        for track in p["tracks"]:
            for clip in track["clips"]:
                if clip.get("audio_role") in {"guide", "final", "both"}:
                    clip["audio_role"] = "neither"
    if videos:
        lane = {
            "id": uuid.uuid4().hex,
            "role": "retained",
            "name": "Imported source pictures",
            "clips": [],
        }
        for m in videos:
            a, b = m["start"] + origin, m["end"] + origin
            if target == "merge" and b > p["duration_frames"]:
                # Media is retained in full; extending the movie does not move its grid.
                p["duration_frames"] = b
            clip = {
                "id": uuid.uuid4().hex,
                "asset_id": m["asset"]["id"],
                "start": a,
                "end": b,
                "source_in": m["source_in"],
                "speed": "1",
                "padding": "hold",
                "audio_padding": "silence",
                "audio_role": "neither",
            }
            if selected.get("soundtrack") and not sounds and m["asset"].get("audio"):
                clip["audio_role"] = "both"
            lane["clips"].append(clip)
        p["tracks"].append(lane)
        p["operation"] = "recreate"
    if sounds:
        if soundtrack_mode == "replace":
            for t in p["tracks"]:
                for c in t["clips"]:
                    if c.get("audio_role") in {"guide", "final", "both"}:
                        c["audio_role"] = "neither"
        lane = {
            "id": uuid.uuid4().hex,
            "role": "audio",
            "name": "Imported original soundtrack",
            "clips": [],
        }
        for sound_index, m in enumerate(sounds):
            a = origin + m["start"]
            b = origin + m["end"]
            # A selected soundtrack covers the active scene, with explicit
            # silence after a short source. No generated-audio fallback.
            if sound_index == len(sounds) - 1:
                b = p["selection"][1] if videos else max(b, p["selection"][1])
            p["duration_frames"] = max(p["duration_frames"], b)
            lane["clips"].append(
                {
                    "id": uuid.uuid4().hex,
                    "asset_id": m["asset"]["id"],
                    "start": a,
                    "end": b,
                    "source_in": m["source_in"],
                    "speed": "1",
                    "padding": "silence",
                    "audio_role": "both",
                }
            )
            p["audio_sample_rate"] = int(m["asset"]["audio"]["sample_rate"])
        p["tracks"].append(lane)
    for m in videos + sounds:
        if not any(a["id"] == m["asset"]["id"] for a in p["assets"]):
            p["assets"].append(copy.deepcopy(m["asset"]))
    mapping = prompt_mapping(document, p, time_origin)
    if target == "replace" and (
        p["chunk_frames"] != current["chunk_frames"]
        or p.get("chunk_origin_frame", 0) != current.get("chunk_origin_frame", 0)
    ):
        mapping["notes"].append(
            f"Replace scene uses origin 0 and {p['chunk_frames']}-frame chunks; previous scene origin/grid were {current.get('chunk_origin_frame', 0)}/{current['chunk_frames']}."
        )
    if selected.get("global"):
        p.setdefault("prompts", {})["global"] = document.get("global", "")
        # Replace the old script's shared portion as well as the visible field.
        # Otherwise editing the imported identity can expose an older copy of
        # that identity still stored as a Fixed script or Timeline preamble.
        from ..constants import PROMPT_MODE_FIXED, PROMPT_MODE_TIMELINE
        from ..v2.prompts import resolve_prompt_mode
        from .prompt_import import _CHUNK, _TIME

        script = p["prompts"].get("script", "")
        mode = resolve_prompt_mode(p["prompts"].get("mode", "Fixed"), script)
        if mode == PROMPT_MODE_FIXED:
            p["prompts"]["script"] = ""
        elif mode == PROMPT_MODE_TIMELINE:
            lines = script.splitlines()
            first = next(
                (
                    i
                    for i, line in enumerate(lines)
                    if _TIME.fullmatch(line) or _CHUNK.fullmatch(line)
                ),
                None,
            )
            if first is not None:
                p["prompts"]["script"] = "\n".join(lines[first:])
    if selected.get("chunks"):
        p.setdefault("prompts", {})["regions"] = replace_regions(
            p.get("prompts", {}).get("regions", []), mapping["regions"]
        )
    if selected.get("global") or selected.get("chunks"):
        p["prompt_source"] = {
            "mode": "studio",
            "imported_from": provenance or {"kind": "file"},
        }
    p["import_notes"] = mapping["notes"]
    validate(p)
    return p, mapping

"""Canonical editable chunk actions, including migration from v18/v19 tracks."""

import copy
import uuid


def replace_regions(existing, incoming):
    result = copy.deepcopy(existing)
    for new in incoming:
        retained = []
        for old in result:
            if old["end"] <= new["start"] or old["start"] >= new["end"]:
                retained.append(old)
                continue
            if old["start"] < new["start"]:
                part = {**old, "id": uuid.uuid4().hex, "end": new["start"]}
                part.pop("chunk_index", None)
                retained.append(part)
            if old["end"] > new["end"]:
                part = {**old, "id": uuid.uuid4().hex, "start": new["end"]}
                part.pop("chunk_index", None)
                retained.append(part)
        result = retained + [copy.deepcopy(new)]
    return sorted(result, key=lambda x: (x["start"], x["end"]))


def migrate_chunk_prompts(project):
    """Move legacy chunk editor clips; generic prompt lanes remain untouched."""
    legacy = []
    tracks = []
    for track in project.get("tracks", []):
        if track["role"] != "prompt":
            tracks.append(track)
            continue
        kept = []
        for clip in track.get("clips", []):
            if not clip.get("chunk_prompt"):
                kept.append(clip)
                continue
            legacy = replace_regions(
                legacy,
                [
                    {
                        **clip,
                        "muted": bool(track.get("muted") or clip.get("muted")),
                        "locked": bool(track.get("locked") or clip.get("locked")),
                    }
                ],
            )
        if kept or not track.get("clips"):
            track["clips"] = kept
            tracks.append(track)
    if legacy:
        prompts = project.setdefault("prompts", {})
        prompts["regions"] = replace_regions(legacy, prompts.get("regions", []))
        project["tracks"] = tracks
    return project

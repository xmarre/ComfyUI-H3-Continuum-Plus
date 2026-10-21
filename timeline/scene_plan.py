"""Editable shot plan and capacity-aware use of native scoped references."""
from __future__ import annotations

import copy
from fractions import Fraction
import math

from .model import digest, rational, source_time

SCHEMA = "h3_scene_plan_v1"
DEFAULTS = {"sensitivity": 1., "detect_shots": True, "within_shot": True,
            "deduplicate": True, "min_shot_frames": 3, "max_per_shot": 2,
            "h3_mp": .7, "vlm_mp": .25, "tone_mapping": "zscale",
            "sdr_nits": 125, "downsample": "lanczos",
            "scene_budget": 3, "reserved_identity_slots": 1, "global_budget": 128,
            "generate_descriptions": False}


def settings(value=None):
    value = value or {}
    if not isinstance(value, dict) or set(value)-set(DEFAULTS):
        raise ValueError("Unknown scene-analysis settings")
    result = {**DEFAULTS, **value}
    for key in ("detect_shots", "within_shot", "deduplicate", "generate_descriptions"):
        if type(result[key]) is not bool:
            raise ValueError(f"{key} must be enabled or disabled")
    for key, low, high in [("min_shot_frames", 1, 120), ("max_per_shot", 1, 4), ("scene_budget", 0, 8),
                           ("reserved_identity_slots", 0, 8), ("global_budget", 1, 2048), ("sdr_nits", 80, 300)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"Invalid {key}")
    for key, low, high in [("sensitivity", .25, 4), ("h3_mp", .01, 32), ("vlm_mp", .01, 4)]:
        if isinstance(result[key], bool) or not isinstance(result[key], (int, float)) or not math.isfinite(result[key]) or not low <= result[key] <= high:
            raise ValueError(f"Invalid {key}")
    if result["tone_mapping"] not in {"zscale", "libplacebo"} or result["downsample"] not in {"lanczos", "bicubic", "spline"}:
        raise ValueError("Unsupported reference color or resize mode")
    return result


def validate_plan(plan, asset_ids, duration):
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA:
        raise ValueError("Unsupported scene plan schema")
    settings(plan.get("settings"))
    refs = plan.get("references", {})
    if not isinstance(refs, dict) or len(refs) > 8192:
        raise ValueError("Invalid scene reference records")
    for rid, ref in refs.items():
        if not isinstance(rid, str) or not isinstance(ref, dict):
            raise ValueError("Invalid scene reference identity")
        if ref.get("asset_id") not in asset_ids or ref.get("source_asset_id") not in asset_ids:
            raise ValueError("Scene reference points to unregistered media")
    shots = plan.get("shots", [])
    if not isinstance(shots, list) or len(shots) > 100000:
        raise ValueError("Invalid shot plan")
    seen = set()
    for shot in shots:
        if not isinstance(shot.get("id"), str) or shot["id"] in seen:
            raise ValueError("Shot identities must be unique")
        seen.add(shot["id"])
        a, b = shot.get("range", [None, None])
        if type(a) is not int or type(b) is not int or not 0 <= a < b <= duration:
            raise ValueError("Shot interval must be a half-open project frame range")
        if shot.get("source_asset_id") not in asset_ids:
            raise ValueError("Shot source is unregistered")
        for key in ("source_start", "source_end"):
            rational(shot[key])
        if not isinstance(shot.get("reference_ids", []), list) or any(r not in refs for r in shot.get("reference_ids", [])):
            raise ValueError("Shot refers to an unavailable reference")
        user = shot.get("user", {})
        if not isinstance(user, dict) or not isinstance(user.get("description", ""), str) or len(user.get("description", "")) > 8000:
            raise ValueError("Invalid shot correction")
        for key in ("scene_id", "setup_id"):
            if shot.get(key) is not None and (not isinstance(shot[key], str) or len(shot[key]) > 256):
                raise ValueError("Invalid scene/camera setup identity")
        subjects = shot.get("visible_subjects", [])
        if not isinstance(subjects, list) or len(subjects) > 12 or any(not isinstance(v, str) or len(v) > 1200 for v in subjects):
            raise ValueError("Invalid visible subject descriptions")
        if type(user.get("enabled", True)) is not bool:
            raise ValueError("Invalid shot activation")
    return plan


def mapped_plan(project, analysis):
    """Rebase exact source PTS through actual clip mappings, including VFR/speed.

    This is a detached candidate. The browser applies it under its edit-version
    lease as one History transaction; no job writes an active project.
    """
    plan = {"schema": SCHEMA, "analysis_id": analysis["id"], "source_asset_id": analysis["source_asset_id"],
            "settings": analysis["settings"], "references": copy.deepcopy(analysis["references"]),
            "shots": [], "timing": {"detected": "source PTS", "requested": "project frame intervals",
                                     "enforced": False, "generated_measurements": None}}
    old = project.get("scene_plan", {})
    corrections = {(s["source_asset_id"], s["source_start"], s["source_end"], tuple(s["range"])): s for s in old.get("shots", [])}
    for track in project["tracks"]:
        if track["role"] != "retained" or track.get("muted"):
            continue
        for clip in track["clips"]:
            if clip["asset_id"] != analysis["source_asset_id"] or clip.get("muted"):
                continue
            speed = rational(clip.get("speed", 1))
            source_a, source_b = source_time(clip, clip["start"]), source_time(clip, clip["end"])
            for source_shot in analysis["shots"]:
                x, y = rational(source_shot["source_start"]), rational(source_shot["source_end"])
                low, high = max(min(source_a, source_b), x), min(max(source_a, source_b), y)
                if low >= high:
                    continue
                endpoints = [Fraction(clip["start"]) + (v-source_a)*24/speed for v in (low, high)]
                a, b = max(clip["start"], round(min(endpoints))), min(clip["end"], round(max(endpoints)))
                if analysis["scope"] == "selection":
                    a, b = max(a, analysis["project_range"][0]), min(b, analysis["project_range"][1])
                if a >= b:
                    continue
                shot = {**copy.deepcopy(source_shot), "id": digest([source_shot["id"], clip["id"], a, b]),
                        "range": [a, b], "source_asset_id": clip["asset_id"], "mapping": {
                            "clip_id": clip["id"], "source_in": clip["source_in"], "speed": str(speed), "clip_start": clip["start"]}}
                key = (shot["source_asset_id"], shot["source_start"], shot["source_end"], tuple(shot["range"]))
                if key in corrections:
                    before = corrections[key]
                    shot["user"] = copy.deepcopy(before.get("user", {}))
                    if before.get("user", {}).get("references_overridden"):
                        shot["reference_ids"] = list(before["reference_ids"])
                        for rid in shot["reference_ids"]:
                            plan["references"][rid] = copy.deepcopy(old["references"][rid])
                plan["shots"].append(shot)
    # User-split/merged intervals are durable source corrections even when a
    # new detector partition differs. Preserve them while their mapping is
    # still valid, splitting only the newly measured neighboring intervals.
    for corrected in old.get("shots", []):
        if (not corrected.get("user") or corrected["source_asset_id"] != analysis["source_asset_id"]
                or not mapping_valid(project, corrected)):
            continue
        a, b = corrected["range"]
        if not any(s["range"][0] < b and a < s["range"][1] for s in plan["shots"]):
            continue
        remaining = []
        for new in plan["shots"]:
            x, y = new["range"]
            if y <= a or x >= b:
                remaining.append(new)
                continue
            for left, right in [(x, min(y, a)), (max(x, b), y)]:
                if left >= right:
                    continue
                part = copy.deepcopy(new)
                part.update(id=digest([new["id"], left, right]), range=[left, right])
                mapping = part["mapping"]
                source = [rational(mapping["source_in"])+Fraction(v-mapping["clip_start"], 24)*rational(mapping["speed"]) for v in (left, right)]
                part["source_start"], part["source_end"] = str(min(source)), str(max(source))
                remaining.append(part)
        for rid in corrected["reference_ids"]:
            plan["references"][rid] = copy.deepcopy(old["references"][rid])
        plan["shots"] = remaining+[copy.deepcopy(corrected)]
    plan["shots"].sort(key=lambda x: (x["range"], x["id"]))
    return plan


def mapping_valid(project, shot):
    mapping = shot.get("mapping")
    if not mapping:
        return True
    from .model import active_clips
    overlapping = active_clips(project, *shot["range"], {"retained"})
    position = next((i for i, (_, c) in enumerate(overlapping) if c["id"] == mapping["clip_id"]), None)
    if position is None or any(c["id"] != mapping["clip_id"] for _, c in overlapping[position+1:]):
        return False
    return any(t["role"] == "retained" and not t.get("muted") and any(
        c["id"] == mapping["clip_id"] and c["asset_id"] == shot["source_asset_id"] and not c.get("muted")
        and c["start"] <= shot["range"][0] < shot["range"][1] <= c["end"]
        and source_time(c, shot["range"][0]) == rational(mapping["source_in"]) + Fraction(shot["range"][0]-mapping["clip_start"], 24)*rational(mapping["speed"])
        and rational(c["speed"]) == rational(mapping["speed"])
        for c in t["clips"]) for t in project["tracks"])


def allocate(project, start, end, persistent_count, manual_images):
    plan = project.get("scene_plan")
    if not plan:
        return [], [], []
    cfg = settings(plan["settings"])
    shots = [s for s in plan["shots"] if s["range"][0] < end and start < s["range"][1]
             and s.get("user", {}).get("enabled", True) and mapping_valid(project, s)]
    existing = {i["asset_id"] for i in manual_images}
    capacity = max(0, min(cfg["scene_budget"], 8-max(persistent_count, cfg["reserved_identity_slots"])-len(existing)))
    ranked = {}
    for shot in shots:
        coverage = min(end, shot["range"][1])-max(start, shot["range"][0])
        for rid in shot.get("reference_ids", []):
            ranked[rid] = ranked.get(rid, 0)+coverage
    images, chosen, selected_assets = [], [], set(existing)
    for rid in sorted(ranked, key=lambda r: (-ranked[r], r)):
        ref = plan["references"][rid]
        extraction = ref.get("extraction")
        if extraction:
            h3 = next(a for a in extraction["assets"] if a["representation"] == "h3")
            if min(h3["width"], h3["height"]) < 32:
                continue
        if ref["asset_id"] in selected_assets:
            chosen.append(rid)
            continue
        if len(images) >= capacity:
            continue
        selected_assets.add(ref["asset_id"])
        chosen.append(rid)
        images.append({"role": "image_reference", "asset_id": ref["asset_id"], "alias": "scene-"+rid[:16],
                       "range": [start, end], "scene_reference_id": rid, "audio_role": "neither"})
    omitted = sorted(set(ranked)-set(chosen))
    warnings = [f"Scene-reference budget omitted {len(omitted)} candidate(s); existing identity/manual references retain their slots."] if omitted else []
    return images, shots, warnings


def shot_instructions(shot, plan, picture_by_asset):
    user = shot.get("user", {})
    description = user.get("description")
    if description is None and plan["settings"].get("generate_descriptions"):
        description = shot.get("vlm", {}).get("description", "")
    description = description or ""
    tags = []
    for rid in shot.get("reference_ids", []):
        asset_id = plan["references"][rid]["asset_id"]
        if asset_id in picture_by_asset:
            tags.append(picture_by_asset[asset_id])
    text = []
    if tags:
        text.append("Scene composition evidence: "+", ".join(dict.fromkeys(tags))+". Use for framing, staging, wardrobe and environment. Dedicated character identity references and authored character instructions control facial appearance.")
    if description:
        text.append(description)
    return "\n".join(text)


def augment_prompt(project, shots, start, end, base, segments, text, picture_by_asset):
    """Add a lower-priority visual layer without parsing/rephrasing dialogue."""
    if not shots or not any(shot_instructions(s, project["scene_plan"], picture_by_asset) for s in shots):
        return segments, text
    plan = project["scene_plan"]
    intervals = segments or [{"range": [start, end], "text": text}]
    cuts = sorted({start, end, *(max(start, s["range"][0]) for s in shots),
                   *(min(end, s["range"][1]) for s in shots), *(v for s in intervals for v in s["range"])})
    result = []
    for a, b in zip(cuts, cuts[1:]):
        authored = next((s["text"] for s in intervals if s["range"][0] <= a < s["range"][1]), base)
        visual = [shot_instructions(s, plan, picture_by_asset) for s in shots if s["range"][0] < b and a < s["range"][1]]
        extra = "\n".join(dict.fromkeys(x for x in visual if x))
        body = ("Visual shot request (timing advisory; authored instructions take precedence):\n"+extra+"\n\n" if extra else "")+authored
        if result and result[-1]["text"] == body:
            result[-1]["range"][1] = b
        else:
            result.append({"range": [a, b], "text": body})
    from .model import seconds_text
    return result, "\n".join(f"{seconds_text(s['range'][0]-start)}-{seconds_text(s['range'][1]-start)}s:\n{s['text']}" for s in result)


def edit_plan(project, operation, shot_id, *, frame=None, other_id=None, reference_ids=None, setup_id=None):
    """Detached exact split/merge/reuse edits for the editor's normal undo stack."""
    from .model import validate
    p = validate(project)
    plan = p.get("scene_plan")
    if not plan:
        raise ValueError("Analyze footage before editing a shot plan")
    shot = next((s for s in plan["shots"] if s["id"] == shot_id), None)
    if not shot:
        raise ValueError("Unknown shot occurrence")
    if operation == "split":
        a, b = shot["range"]
        if type(frame) is not int or not a < frame < b:
            raise ValueError("Split must be inside the selected shot")
        mapping = shot["mapping"]
        pts = rational(mapping["source_in"]) + Fraction(frame-mapping["clip_start"], 24)*rational(mapping["speed"])
        left, right = copy.deepcopy(shot), copy.deepcopy(shot)
        left["range"][1], right["range"][0] = frame, frame
        left["id"], right["id"] = digest([shot_id, frame, "left"]), digest([shot_id, frame, "right"])
        if rational(mapping["speed"]) > 0:
            left["source_end"] = right["source_start"] = str(pts)
        else:
            left["source_start"] = right["source_end"] = str(pts)
        left["provenance"] = right["provenance"] = "user-split"
        plan["shots"] = [s for s in plan["shots"] if s["id"] != shot_id]+[left, right]
    elif operation == "merge":
        other = next((s for s in plan["shots"] if s["id"] == other_id), None)
        if other is None:
            raise ValueError("Choose the adjacent shot to merge")
        left, right = sorted([shot, other], key=lambda s: s["range"][0])
        if left["range"][1] != right["range"][0] or left.get("mapping") != right.get("mapping"):
            raise ValueError("Merge requires adjacent shots on the same source mapping")
        merged = copy.deepcopy(left)
        merged.update(id=digest([left["id"], right["id"], "merged"]), range=[left["range"][0], right["range"][1]],
                      source_start=str(min(rational(left["source_start"]), rational(right["source_start"]))),
                      source_end=str(max(rational(left["source_end"]), rational(right["source_end"]))), provenance="user-merged")
        merged["reference_ids"] = list(dict.fromkeys(left["reference_ids"]+right["reference_ids"]))
        # Never silently discard either user correction.
        merged["user"] = {**left.get("user", {}), "description": "\n".join(dict.fromkeys(
            s.get("user", {}).get("description", "") for s in [left, right])).strip(), "references_overridden": True}
        plan["shots"] = [s for s in plan["shots"] if s["id"] not in {left["id"], right["id"]}]+[merged]
    elif operation == "references":
        if not isinstance(reference_ids, list) or len(reference_ids) > 8 or any(r not in plan["references"] for r in reference_ids):
            raise ValueError("Choose registered scene references")
        shot["reference_ids"] = list(dict.fromkeys(reference_ids))
        shot.setdefault("user", {})["references_overridden"] = True
        if setup_id is not None:
            if not isinstance(setup_id, str) or len(setup_id) > 128:
                raise ValueError("Invalid camera setup identity")
            shot["setup_id"] = setup_id
    else:
        raise ValueError("Unknown shot-plan edit")
    plan["shots"].sort(key=lambda s: (s["range"], s["id"]))
    return validate(p)

"""Persistent CPU scene jobs. Analysis never edits or queues the active project."""
from __future__ import annotations

import copy
from fractions import Fraction
import json
import re
import threading
import time
import uuid

from .media import probe
from .model import digest, rational, source_time, validate
from .scene_color import extract_reference, COLOR_VERSION
from .scene_detection import (decode_measurements, detect, frames, measurement_key,
                              open_measurements, select_candidates, setup_match, DETECTOR_VERSION)
from .scene_plan import settings, mapped_plan

_lock = threading.RLock()
_workers = {}
# Serial analysis protects cache publication and bounds CPU/disk decoder work.
_slots = threading.Semaphore(1)


def analysis_source(project, asset_id, scope):
    p = validate(project)
    source = next((a for a in p["assets"] if a["id"] == asset_id and a.get("video") and a.get("kind") != "image"), None)
    if source is None:
        raise ValueError("Choose registered original video footage")
    if scope not in {"selection", "source"}:
        raise ValueError("Analysis scope must be selected scene or full source")
    start = rational(source["video"].get("start_time", "0"))
    end = start+rational(source["duration"])
    if scope == "selection":
        mapped = []
        for track in p["tracks"]:
            if track["role"] != "retained" or track.get("muted"):
                continue
            for clip in track["clips"]:
                a, b = max(clip["start"], p["selection"][0]), min(clip["end"], p["selection"][1])
                if clip["asset_id"] == asset_id and not clip.get("muted") and a < b:
                    mapped.extend([source_time(clip, a), source_time(clip, b)])
        if not mapped:
            raise ValueError("Selected scene has no active mapping to that source")
        start, end = max(start, min(mapped)), min(end, max(mapped))
    if start >= end:
        raise ValueError("Empty source analysis interval")
    return p, source, start, end


class SceneAnalysis:
    def __init__(self, store):
        self.store = store
        self.root = store.root / "scene_analysis"
        self.root.mkdir(exist_ok=True)

    def path(self, token):
        if not re.fullmatch(r"[a-f0-9]{64}", str(token)):
            raise ValueError("Invalid scene analysis identity")
        root = self.root/token
        root.mkdir(exist_ok=True)
        return root

    def status(self, token):
        file = self.path(token)/"status.json"
        if not file.is_file():
            raise ValueError("Unknown scene analysis")
        return json.loads(file.read_text(encoding="utf-8"))

    def result(self, token):
        file = self.path(token)/"result.json"
        if not file.is_file():
            raise ValueError("Scene analysis has no completed result")
        return json.loads(file.read_text(encoding="utf-8"))

    def cancel(self, token):
        self.path(token)
        with _lock:
            worker = _workers.get((str(self.root), token))
            if worker:
                worker[1].set()
        return {"cancel_requested": bool(worker)}

    def start(self, project, asset_id, *, scope="selection", config=None):
        p, source, start, end = analysis_source(project, asset_id, scope)
        cfg = settings(config)
        # Mapping/range are part of the result lease, not measurement identity.
        token = digest([DETECTOR_VERSION, COLOR_VERSION, source["id"], str(start), str(end), cfg,
                        scope, p["selection"], [(t["role"], t.get("muted"), t["clips"]) for t in p["tracks"] if t["role"] == "retained"]])
        root = self.path(token)
        key = (str(self.root), token)
        with _lock:
            if key in _workers:
                return {"id": token, **self.status(token)}
            if (root/"result.json").is_file():
                return {"id": token, "stage": "complete", "cache_hit": True}
            stop = threading.Event()
            self.store.atomic_json(root/"status.json", {"id": token, "stage": "waiting", "cache_hit": False})
            worker = threading.Thread(target=self._run, args=(token, p, source, start, end, scope, cfg, stop), daemon=True)
            _workers[key] = (worker, stop)
            worker.start()
        return {"id": token, "stage": "waiting", "cache_hit": False}

    def _run(self, token, project, source, start, end, scope, cfg, stop):
        root = self.path(token)
        started = time.monotonic()
        state = {"id": token, "stage": "waiting", "cache_hit": False}

        def cancel():
            if stop.is_set():
                raise InterruptedError("Scene analysis cancelled; measurements and completed references retained")

        def progress(**updates):
            state.update(updates, elapsed_seconds=round(time.monotonic()-started, 3))
            self.store.atomic_json(root/"status.json", state)
        acquired = False
        try:
            while not _slots.acquire(timeout=.1):
                cancel()
            acquired = True
            cancel()
            file = self.store.assets.verify(source["id"], cancel=cancel)
            # Refresh registered metadata from verified content; project JSON is
            # never trusted to declare a Dolby Vision file to be ordinary SDR.
            info = probe(file)
            source = {**source, **info}
            db = open_measurements(self.path(measurement_key(source["id"], start, end))/"measurements.sqlite")
            try:
                count_before = db.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
                summary = decode_measurements(file, db, start, end, cancel=cancel, progress=progress)
                progress(stage="detect", frames=summary["frames"], cached_frames=count_before)
                events = detect(db, cfg, cancel=cancel)
                cuts = sorted({0, summary["frames"], *(e["frame"] for e in events if e["type"] != "composition_change")})
                shots, references, setups = [], {}, []
                for ordinal, (a, b) in enumerate(zip(cuts, cuts[1:])):
                    cancel()
                    first = next(frames(db, a, a+1))
                    last = next(frames(db, b-1, b))
                    pts_end = min(end, last["time"]+last["duration"])
                    candidates = select_candidates(db, a, b, cfg["max_per_shot"])
                    setup = None
                    reuse = None
                    if candidates and cfg["deduplicate"]:
                        # Bounded setup comparison; no unbounded image corpus in
                        # memory. Old references remain accessible by ID.
                        for prior in setups[-256:]:
                            if setup_match(candidates[0]["image"], prior["image"]):
                                setup, reuse = prior["id"], prior["reference_id"]
                                break
                    setup = setup or digest([source["id"], first["pts"], "camera_setup"])
                    selected = []
                    for ci, candidate in enumerate(candidates):
                        if ci == 0 and reuse:
                            selected.append(reuse)
                            continue
                        if len(references) >= cfg["global_budget"]:
                            break
                        progress(stage="extract", shot=ordinal+1, shots=len(cuts)-1, references=len(references))
                        extracted = extract_reference(self.store, source, candidate, cfg, cancel=cancel)
                        h3 = next(x for x in extracted["assets"] if x["representation"] == "h3")
                        vlm = next(x for x in extracted["assets"] if x["representation"] == "vlm")
                        ref = {"id": extracted["id"], "asset_id": h3["id"], "vlm_asset_id": vlm["id"],
                               "source_asset_id": source["id"], "pts": candidate["pts"], "time_base": candidate["time_base"],
                               "quality": candidate["quality_metrics"], "extraction": extracted}
                        references[ref["id"]] = ref
                        selected.append(ref["id"])
                    if candidates and selected and not reuse:
                        setups.append({"id": setup, "image": candidates[0]["image"], "reference_id": selected[0]})
                    event = next((e for e in events if e["frame"] == a and e["type"] != "composition_change"), None)
                    shots.append({"id": digest([source["id"], first["pts"], str(pts_end)]), "scene_id": None,
                                  "setup_id": setup, "source_start": str(first["time"]), "source_end": str(pts_end),
                                  "source_frame_range": [a, b], "start_pts": first["pts"], "time_base": first["time_base"],
                                  "event": event, "events": [e for e in events if a <= e["frame"] < b],
                                  "reference_ids": selected, "reused_reference": bool(reuse), "visible_subjects": [],
                                  "provenance": "measured", "user": {}})
                result = {"schema": "h3_source_scene_analysis_v1", "id": token, "source_asset_id": source["id"],
                          "scope": scope, "project_range": project["selection"], "source_range": [str(start), str(end)],
                          "settings": cfg, "summary": summary, "events": events, "shots": shots, "references": references,
                          "metrics": {"elapsed_seconds": round(time.monotonic()-started, 3), "cached_frames": count_before,
                                      "decoded_frames": summary["frames"]-count_before, "descriptor_ram_frames": 32}}
                cancel()
                self.store.atomic_json(root/"result.json", result)
                progress(stage="complete", fraction=1, result_available=True, **result["metrics"])
            finally:
                db.close()
        except InterruptedError as exc:
            progress(stage="cancelled", error=str(exc))
        except Exception as exc:
            progress(stage="failed", error=str(exc))
        finally:
            if acquired:
                _slots.release()
            with _lock:
                _workers.pop((str(self.root), token), None)

    def preview(self, project, token):
        p = validate(project)
        analysis = self.result(token)
        if analysis["source_asset_id"] not in {a["id"] for a in p["assets"]}:
            raise ValueError("Analysis source is no longer registered in this project")
        candidate = copy.deepcopy(p)
        registered = {a["id"] for a in p["assets"]}
        for ref in analysis["references"].values():
            for asset in ref["extraction"]["assets"]:
                if asset["id"] not in registered:
                    candidate["assets"].append(asset)
                    registered.add(asset["id"])
        candidate["scene_plan"] = mapped_plan(p, analysis)
        return validate(candidate)

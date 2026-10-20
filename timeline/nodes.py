"""Compact project node, native sampler facade, take commit and export nodes."""

from __future__ import annotations

import copy
import logging
import time
import json
import os
from pathlib import Path
import tempfile
import uuid

import numpy as np

from .media import ProjectStore, run_process
from .model import blank_project, compile_project, digest, with_external_prompt_source
from .runtime import TimelineInputs

CATEGORY = "MiniMax H3/Continuum/Timeline Studio"
LOG = logging.getLogger("h3_continuum_timeline")


def project_store():
    import folder_paths

    from .mounts import wsl_drive_roots

    input_root = Path(folder_paths.get_input_directory()).resolve()
    # Enumerate mounted drive letters only to constrain native-path linking.
    # Never traverse drive contents or guess paths from browser metadata.
    extra = [p for p in os.environ.get("H3_TIMELINE_LINK_ROOTS", "").split(os.pathsep) if p]
    return ProjectStore(
        input_root / "h3_timeline_studio",
        link_roots=[input_root, *wsl_drive_roots(), *extra],
    )


def takes(store, project_id):
    directory = store.project_dir(project_id)
    return sorted(
        [
            json.loads(p.read_text(encoding="utf-8"))
            for p in directory.glob("take-*.json")
        ],
        key=lambda r: r.get("created_ns", 0),
    )


def take_path(store, project_id, take_id):
    if len(take_id) != 32 or any(c not in "0123456789abcdef" for c in take_id):
        raise ValueError("Invalid take identifier")
    return store.project_dir(project_id) / f"take-{take_id}.json"


def stale_take(
    p, record, records, end_frame=None, check_selection=False, ignore_selection=False
):
    from .model import compile_window

    if record.get("prompt_override"):
        p = with_external_prompt_source(p, record["prompt_override"])
    limit = len(record["indices"])
    ranges = record.get("chunk_output_ranges", [])
    if end_frame is not None and ranges:
        limit = sum(a < end_frame for a, b in ranges)
    scope = record.get("guide_scope")
    from .model import reference_slots_for_chunk

    sizes = record.get("reference_slot_sizes")
    for index, old in zip(record["indices"][:limit], record["input_hashes"][:limit]):
        picture_offset = record.get("picture_offset", 0)
        selected = reference_slots_for_chunk(p, index)
        if sizes is not None and selected is not None:
            picture_offset = sum(sizes[slot - 1] for slot in selected)
        window = compile_window(
            p,
            index,
            guide_scope=tuple(scope) if scope else None,
            picture_offset=picture_offset,
        )
        if window["generation_hash"] != old:
            return True
    by_id = {r["id"]: r for r in records}
    # A different accepted take can share a valid exact prefix but replace its
    # suffix. Compare immutable native chunk payload identities up to this cut.
    chosen = p.get("accepted", {}).get(str(record["indices"][0]))
    if check_selection and not ignore_selection and chosen and chosen != record["id"]:
        current = by_id.get(chosen)
        if current is None:
            return True
        left, right = record.get("chunk_hashes", []), current.get("chunk_hashes", [])
        if not left or not right:
            return True
        if left[: min(limit, len(right))] != right[: min(limit, len(right))]:
            return True
    parent = record.get("parent_take")
    if parent:
        previous = by_id.get(parent)
        if (
            parent in p.get("rejected", [])
            or previous is None
            or previous["indices"][0] >= record["indices"][0]
        ):
            return True
        if stale_take(
            p,
            previous,
            records,
            check_selection=True,
            ignore_selection=ignore_selection,
        ):
            return True
    return False


def retained_anchor(p, store, frame):
    """First-frame anchor for Replace: the retained frame the take starts on."""
    from .compose import render_frames
    from .model import active_clips

    if not active_clips(p, frame, frame + 1, {"retained"}):
        return None, None
    import torch

    pixels = render_frames(
        p, store.assets, frame, frame + 1, p["width"], p["height"], roles={"retained"}
    )
    note = f"Replace anchored the first generated frame to retained frame {frame}"
    return torch.from_numpy(pixels).float() / 255, note


def take_seed_request(
    p, job, records, previous, settings_hash, storage_name, boundary, nonce
):
    """Resolve normal queueing against immutable take seed schedules.

    Explicit reroll inputs remain reproducible. A normal queue after rejecting
    the latest matching take asks native Run Storage to allocate a new nonce.
    """
    boundary, nonce = int(boundary), int(nonce)
    if boundary:
        return boundary, nonce, None
    matching = [
        r
        for r in records
        if r["indices"][0] == job["start_chunk"]
        and r.get("settings_hash") == settings_hash
    ]
    if matching and matching[-1]["id"] in p.get("rejected", []):
        first = max(1, p["selection"][0] // p["chunk_frames"] - job["start_chunk"] + 1)
        if job["requires_previous_job"] or job.get("resume_parent_take"):
            first = 1
        return (
            first,
            0,
            "Latest matching take was rejected; native storage allocates a fresh seed nonce",
        )
    if previous and previous.get("storage_name") == storage_name:
        saved = previous.get("sampling_contract")
        if not saved:
            return (
                1,
                0,
                "Native storage resolves the interrupted take's missing seed schedule",
            )
        first = int(saved.get("reroll_from_chunk", 0))
        if 0 < first <= len(job["windows"]):
            return (
                first,
                int(saved["effective_reroll_nonce"]),
                "Restoring the selected take's stored seed schedule",
            )
    return 0, nonce, None


class H3ContinuumTimelineStudio:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project_json": (
                    "STRING",
                    {"default": json.dumps(blank_project()), "multiline": True},
                )
            }
        }

    RETURN_TYPES = ("H3_CONTINUUM_TIMELINE_PROJECT", "STRING")
    RETURN_NAMES = ("timeline", "input_report")
    FUNCTION = "compile"
    CATEGORY = CATEGORY
    DESCRIPTION = "Open Timeline Studio to edit retained footage, per-window prompts, references and audio. The project owns duration, dimensions and chunk length."

    def compile(self, project_json):
        plan = compile_project(json.loads(project_json))
        return plan, json.dumps(
            {k: v for k, v in plan.items() if k != "project"}, indent=2
        )


class H3ContinuumTimelineSampler:
    @classmethod
    def INPUT_TYPES(cls):
        from ..v3.driving_nodes import H3ContinuumSamplerV34

        native = copy.deepcopy(H3ContinuumSamplerV34.INPUT_TYPES())
        schema = {
            "required": {
                "timeline": ("H3_CONTINUUM_TIMELINE_PROJECT",),
                "model": ("MODEL",),
                "clip": ("CLIP",),
                "video_vae": ("VAE",),
                "audio_vae": ("VAE",),
                "sampler": ("SAMPLER",),
                "sigmas": ("SIGMAS",),
                "job_index": ("INT", {"default": 0, "min": 0}),
                "base_seed": native["required"]["base_seed"],
                "continuity": native["required"]["continuity"],
                "continuation_method": native["required"]["continuation_method"],
                "regenerate_from": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 16,
                        "display_name": "Regenerate From",
                        "tooltip": "0 = Auto. Otherwise reuse the compatible prefix before this one-based chunk of the selected job and regenerate its suffix.",
                    },
                ),
                "reroll_nonce": native["required"]["reroll_nonce"],
                "emit_refine_state": ("BOOLEAN", {"default": False}),
            },
            "optional": {"first_frame": ("IMAGE",), "last_frame": ("IMAGE",)},
            "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"},
        }
        # Keep the original Timeline widget order. Native definitions remain
        # shared, including sizing options, tooltips and seed controls.
        for name in (
            "audio_continuity",
            "diagnostics",
            "debug",
            "show_preview",
            "run_storage",
            "run_name",
            "reference_size",
            "project_id",
            "video_reference_size",
            "strict_compatibility",
        ):
            schema["required"][name] = native["required"][name]
        schema["required"]["run_storage"][1]["default"] = "Save + Auto Resume"
        for name, definition in native["optional"].items():
            if name != "audio_vae":
                schema["optional"][name] = definition
        schema["optional"]["sequence_prompt"] = native["required"]["sequence_prompt"]
        schema["optional"]["sequence_prompt"][1]["tooltip"] = (
            "Optional native prompt source. When connected, replaces Studio's prompt script for the full project; prompt clips still belong to Studio when disconnected."
        )
        schema["optional"]["prompt_mode"] = native["required"]["prompt_mode"]
        for name in ("width", "height"):
            schema["optional"][name] = native["required"][name]
        return schema

    @classmethod
    def IS_CHANGED(cls, regenerate_from=0, **kwargs):
        return float("nan") if int(regenerate_from) > 0 else False

    RETURN_TYPES = (
        "LATENT",
        "LATENT",
        "H3_CONTINUUM_ASSEMBLY_PLAN",
        "H3_CONTINUUM_TIMELINE_TAKE",
        "STRING",
        "H3_CONTINUUM_REFINE_STATE",
        "AUDIO",
    )
    RETURN_NAMES = (
        "video_latents",
        "audio_latents",
        "assembly_plan",
        "take",
        "status",
        "refine_state",
        "driving_audio",
    )
    OUTPUT_IS_LIST = (True, True, False, False, False, True, False)
    FUNCTION = "sample"
    CATEGORY = CATEGORY
    DESCRIPTION = (
        "Native V3.4 sampling with Timeline takes. Width and height control sampling; "
        "Studio supplies the render selection, chunk timing and scoped prompts. Persistent references precede "
        "scoped pictures; scoped video replaces persistent Video Reference only "
        "in its active windows. Connected Driving Audio overrides guide lanes."
    )

    def sample(
        self,
        timeline,
        model,
        clip,
        video_vae,
        audio_vae,
        sampler,
        sigmas,
        job_index,
        base_seed,
        continuity,
        continuation_method,
        regenerate_from,
        reroll_nonce,
        emit_refine_state,
        first_frame=None,
        last_frame=None,
        prompt=None,
        unique_id=None,
        audio_continuity=True,
        diagnostics="Detailed Report",
        debug=False,
        show_preview=True,
        run_storage="Save + Auto Resume",
        run_name="",
        reference_size="Match Output",
        project_id="",
        video_reference_size=None,
        strict_compatibility=False,
        managed_prompt_source_json="",
        conditioning_lookahead=None,
        reference_video_1=None,
        driving_audio=None,
        sequence_prompt=None,
        prompt_mode="Auto",
        width=None,
        height=None,
        **reference_images,
    ):
        from ..v2.sequence import run_sequence
        from ..v2.prompts import make_prompt_plan
        from ..v2.session import model_fingerprint, entry_to_state
        from ..v2.session_io import load_session, save_session
        from ..v2.sampling import capture_chunk_refine_state
        from ..masked_continuation import (
            continuation_method_scope,
            validate_native_masked_request,
        )
        from ..v3.driving_nodes import (
            _normalize_v34_continuity,
            _attach_refine_masks,
            _refine_state_output,
        )
        from ..v3.plan import prepare_physical_decode_entries
        from ..v2.conditioning_lifecycle import conditioning_lookahead_scope
        from ..reference import prepare_reference_assets
        from ..reference_video import (
            prepare_reference_video_source,
            REFERENCE_VIDEO_SIZE_EFFICIENT,
        )
        from ..temporal import align_frame_count_up

        p = copy.deepcopy(timeline["project"])
        source_canvas = [p["width"], p["height"]]
        if width is not None:
            p["width"] = int(width)
        if height is not None:
            p["height"] = int(height)
        if not 0 <= int(job_index) < len(timeline["jobs"]):
            raise ValueError(
                "Timeline job index is outside the compiled selection; use Queue selection or choose an existing job"
            )
        job = copy.deepcopy(timeline["jobs"][int(job_index)])
        store = project_store()
        if job["requires_previous_job"] or job.get("resume_parent_take"):
            first_frame = None
        if int(job_index) != len(timeline["jobs"]) - 1:
            last_frame = None
        anchor_note = None
        if (
            p["operation"] == "replace"
            and first_frame is None
            and not (job["requires_previous_job"] or job.get("resume_parent_take"))
        ):
            first_frame, anchor_note = retained_anchor(p, store, job["range"][0])
        for asset_id in {
            i["asset_id"]
            for w in job["windows"]
            for key in ("images", "video", "guide", "voice")
            for i in w[key]
        }:
            store.assets.verify(asset_id)
        # Preserve physical socket boundaries. Image Conveyor provides one
        # IMAGE (or None) per stable ref_image_1..8 output; V3.4 can also
        # bundle several images in one compatible reference socket.
        from ..reference import ReferenceImageBundle
        from .model import reference_slots_needed

        needed = reference_slots_needed(
            p, (window["index"] for window in job["windows"])
        )
        # The original Conveyor outputs are ComfyUI inputs; only preprocess
        # references used by at least one logical chunk of this native job.
        # No image files or tensors are saved into the timeline project.
        job_references = {
            f"reference_image_{i}": reference_images.get(f"reference_image_{i}")
            if i in needed else None
            for i in range(1, 9)
        }
        slot_sizes = tuple(
            len(value.images) if isinstance(value, ReferenceImageBundle)
            else (1 if value is not None else 0)
            for value in job_references.values()
        )
        references = prepare_reference_assets(
            **job_references,
            output_width=p["width"],
            output_height=p["height"],
            size_mode=reference_size,
        )
        video_reference_size = video_reference_size or REFERENCE_VIDEO_SIZE_EFFICIENT
        video_source = prepare_reference_video_source(
            reference_video_1 if p.get("video_guidance_enabled", True) else None,
            target_frames=align_frame_count_up(p["chunk_frames"]),
            output_width=p["width"],
            output_height=p["height"],
            size_mode=video_reference_size,
        )
        provider = TimelineInputs(p, job, store)
        provider.reference_slot_sizes = slot_sizes
        provider.reference_size = reference_size
        provider.video_reference_size = video_reference_size
        provider.picture_offset = references.count if references else 0
        provider.driving_audio = driving_audio
        provider.set_offset(job["range"][0])
        job = provider.job
        guide = None
        continuity = _normalize_v34_continuity(continuity)
        validate_native_masked_request(
            method=continuation_method,
            continuity=continuity,
            audio_continuity=bool(audio_continuity),
            driving_audio_active=all(
                provider.guide_active(i) for i in range(1, len(job["windows"]))
            ),
            chunks=len(job["windows"]),
        )
        all_takes = takes(store, p["project_id"])
        rejected = set(p.get("rejected", []))
        initial = None
        parent = None
        # Native Run Storage authenticates the selected revision's prefix.
        if job["requires_previous_job"] or job.get("resume_parent_take"):
            parent_id = job.get("resume_parent_take")
            parents = [
                r
                for r in all_takes
                if r["indices"][-1] == job["start_chunk"] - 1
                and r.get("status") == "sampled"
                and r.get(
                    "generation_size",
                    [
                        r.get("project_snapshot", {}).get("width"),
                        r.get("project_snapshot", {}).get("height"),
                    ],
                )
                == [p["width"], p["height"]]
                and r["id"] not in rejected
                and not stale_take(
                    p,
                    r,
                    all_takes,
                    check_selection=True,
                    ignore_selection=bool(p.get("queued_batch_id"))
                    and r.get("batch_id") == p.get("queued_batch_id"),
                )
            ]
            batch_parents = [
                r
                for r in parents
                if p.get("queued_batch_id")
                and r.get("batch_id") == p["queued_batch_id"]
            ]
            parent = (
                batch_parents[-1]
                if batch_parents
                else next(
                    (r for r in parents if r["id"] == parent_id),
                    parents[-1] if parents else None,
                )
            )
            if parent is None:
                raise ValueError(
                    "This job needs the preceding job's valid continuation state; render jobs in order"
                )
            initial = entry_to_state(
                load_session(prefix=parent["session_prefix"], slot=1)["chunks"][-1]
            )
        provider.set_offset(
            parent.get("output_range", parent["range"])[1]
            if parent
            else job["range"][0]
        )
        job = provider.job
        guide = provider.audio_source(audio_vae)
        plan = make_prompt_plan(
            mode="List",
            script=json.dumps([w["prompt"] for w in job["windows"]]),
            chunks=len(job["windows"]),
            chunk_seconds=p["chunk_frames"] / 24,
        )
        connected_prompt_warning = ""
        studio_prompt_owned = p.get("prompt_source", {}).get("mode") == "studio"
        if studio_prompt_owned:
            sequence_prompt = None
        if sequence_prompt is not None:
            from fractions import Fraction
            from ..constants import PROMPT_MODE_LIST, PROMPT_MODE_TIMELINE
            from ..v2.prompts import build_sampler_prompt_plan, detect_prompt_mode

            # The timeline editor must not repeat an entire [0-7s] script
            # merely because an older workflow selected List explicitly.
            from .prompt_import import connected_import_warning
            connected_prompt_warning = connected_import_warning(sequence_prompt)
            detected_mode = detect_prompt_mode(sequence_prompt)
            effective_mode = (
                "Timeline"
                if detected_mode == PROMPT_MODE_TIMELINE
                and prompt_mode in ("List", PROMPT_MODE_LIST)
                else prompt_mode
            )
            external = build_sampler_prompt_plan(
                prompt_mode=effective_mode,
                prompt_script="",
                sequence_prompt=sequence_prompt,
                prompt_plan=None,
                # Native plans are bounded to 16 chunks. This pass resolves
                # format/provenance; compile_window routes the complete source
                # on the project clock, including sections after chunk 16.
                chunks=min(
                    16,
                    (p["duration_frames"] + p["chunk_frames"] - 1) // p["chunk_frames"],
                ),
                chunk_seconds=p["chunk_frames"] / 24,
                managed_prompt_source_json=managed_prompt_source_json,
            )
            source = external["source"]
            if (
                detected_mode == PROMPT_MODE_TIMELINE
                and source.get("kind") != "timeline"
            ):
                # The native sixteen-chunk preflight regards time sections
                # entirely beyond its horizon as unusable and falls back to
                # Fixed. Studio owns the full movie clock; preserve a valid
                # absolute section document without truncating its source.
                from ..v2.prompts import parse_timeline_sections, _timeline_source

                sections = parse_timeline_sections(source["original_text"])
                preflight_end = min(
                    16, (p["duration_frames"] + p["chunk_frames"] - 1)
                    // p["chunk_frames"]
                ) * Fraction(p["chunk_frames"], 24)
                if (
                    sections
                    and all(s["kind"] == "time" for s in sections)
                    and all(Fraction(str(s["start"])) >= preflight_end for s in sections)
                ):
                    source = _timeline_source(
                        source["original_text"], effective_mode,
                        PROMPT_MODE_TIMELINE,
                        p["chunk_frames"] / 24, sections,
                    )
                    external["source"] = source
                    external["mode"] = PROMPT_MODE_TIMELINE
                    external["notes"] = [
                        "Timeline Studio preserved movie-clock sections beyond the native 16-chunk preflight"
                    ]
            origin_frame = 0
            if source.get("kind") == "timeline":
                intervals = [
                    (Fraction(s["start"]), Fraction(s["end"]))
                    for s in source.get("sections", ())
                    if s.get("kind") == "time"
                ]
                # Time ranges already covering the movie position remain
                # project-absolute. A zero-based scene script that ends
                # before the selected movie position uses a scene-local clock.
                # Persist the origin for generation hashing and stale checks.
                if (
                    intervals
                    and min(a for a, _ in intervals) == 0
                    and not any(
                        a < Fraction(job["range"][1], 24)
                        and Fraction(job["range"][0], 24) < b
                        for a, b in intervals
                    )
                    and job["range"][0] >= max(int(b * 24) for _, b in intervals)
                ):
                    # A later render selection of the same scene must not
                    # restart its script from [0s]. Reuse the earliest take's
                    # source-specific origin across independent queue batches.
                    prior_origin = next(
                        (
                            override["origin_frame"]
                            for record in all_takes
                            for override in [record.get("prompt_override")]
                            if isinstance(override, dict)
                            and override.get("script") == source["original_text"]
                            and override.get("mode") == external["mode"]
                            and type(override.get("origin_frame")) is int
                        ),
                        None,
                    )
                    # An explicit new scene anchor takes precedence over
                    # earlier takes of the same prompt script. In legacy
                    # zero-origin projects, reuse the previous scene's first
                    # take when rendering a later selection.
                    scene_anchor = p.get("chunk_origin_frame", 0)
                    origin_frame = (
                        scene_anchor if scene_anchor
                        else prior_origin if prior_origin is not None
                        else job["range"][0]
                    )
            external_project = with_external_prompt_source(
                p, {
                    "global": p["prompts"].get("global", ""),
                    "mode": external["mode"],
                    "script": source["original_text"],
                    "origin_frame": origin_frame,
                },
            )
            provider.project = external_project
            provider.set_offset(provider.offset)
            job = provider.job
            plan = make_prompt_plan(
                mode="List",
                script=json.dumps([w["prompt"] for w in job["windows"]]),
                chunks=len(job["windows"]),
                chunk_seconds=p["chunk_frames"] / 24,
            )
            if external.get("managed_prompt_transport"):
                plan["managed_prompt_transport"] = copy.deepcopy(
                    external["managed_prompt_transport"]
                )
            plan["notes"].extend(external.get("notes", []))
            LOG.info(
                "H3 Timeline Studio external-prompt routing requested=%r detected=%r "
                "effective=%r source=%s scene_origin_frame=%d job_range=%s "
                "chunk_text_sha256_prefixes=%s",
                prompt_mode, detected_mode, external["mode"], source.get("kind"),
                origin_frame, job["range"], [value[:16] for value in plan["hashes"]],
            )
            if effective_mode != prompt_mode:
                plan["notes"].append(
                    "Timeline Studio interpreted time-coded sections instead of legacy List mode"
                )
            if origin_frame:
                plan["notes"].append(
                    f"Connected scene prompt starts at project frame {origin_frame}"
                )
        # The existing Run Storage contract fingerprints model patches/weights,
        # CLIP, VAE, sampler and sigma bytes. Use it to qualify selected sessions.
        from ..run_storage import build_sampling_contract
        from ..driving_audio import _tensor_sha256

        contract_plan = copy.deepcopy(plan)
        contract_plan["timeline_studio"] = provider.storage_contract
        contract, resume_safe, reasons = build_sampling_contract(
            model=model,
            model_fingerprint_value=model_fingerprint(model),
            clip=clip,
            video_vae=video_vae,
            sampler=sampler,
            sigmas=sigmas,
            prompt_plan=contract_plan,
            width=p["width"],
            height=p["height"],
            chunk_seconds=p["chunk_frames"] / 24,
            continuity=continuity,
            audio_continuity=bool(audio_continuity),
            base_seed=int(base_seed),
            reroll_from_chunk=0,
            reroll_nonce=0,
            first_frame_hash=_tensor_sha256(first_frame)
            if first_frame is not None
            else "none",
            last_frame_hash=_tensor_sha256(last_frame)
            if last_frame is not None
            else "none",
            strict_compatibility=False,
            driving_audio_vae=audio_vae,
            driving_audio_contract=guide.contract if guide is not None else None,
            reference_contract=references.contract if references else None,
            reference_video_contract=video_source.contract if video_source else None,
        )
        settings_hash = digest(
            [
                contract["global"],
                continuation_method,
                job["start_chunk"],
                reference_size,
                video_reference_size,
                digest(external["source"]) if sequence_prompt is not None else None,
            ]
        )
        candidates = [
            r
            for r in all_takes
            if r["indices"][0] == job["start_chunk"]
            and r["settings_hash"] == settings_hash
            and r["id"] not in rejected
        ]
        chosen = job.get("resume_take_id") or p.get("accepted", {}).get(
            str(job["start_chunk"])
        )
        previous = next(
            (r for r in candidates if r["id"] == chosen),
            candidates[-1] if candidates else None,
        )
        take_id = uuid.uuid4().hex
        prefix = "timeline_" + p["project_id"] + "_" + take_id
        record = {
            "id": take_id,
            "project_id": p["project_id"],
            "batch_id": p.get("queued_batch_id"),
            "job_index": int(job_index),
            "range": job["range"],
            "indices": [w["index"] for w in job["windows"]],
            "input_hashes": provider.hashes,
            "guide_scope": job.get("guide_scope"),
            "picture_offset": provider.picture_offset,
            "reference_slot_sizes": list(provider.reference_slot_sizes),
            "prompt_override": provider.project["prompts"]
            if sequence_prompt is not None
            else None,
            "settings_hash": settings_hash,
            "parent_take": parent["id"] if parent else None,
            "output_start": provider.offset,
            "created_ns": time.time_ns(),
            "session_prefix": prefix,
            "snapshot_hash": timeline["snapshot_hash"],
            "status": "sampled",
            "operation": p["operation"],
            "final_audio_source": p.get("final_audio_source", "original"),
            "selection": p["selection"],
            "project_snapshot": p,
            "source_canvas": source_canvas,
            "generation_size": [p["width"], p["height"]],
        }
        from ..run_storage import run_storage_scope, resolve_run_storage_name

        storage_name = (
            resolve_run_storage_name(
                project_id=project_id,
                legacy_run_name=run_name,
                automatic_key="timeline_" + p["project_id"],
            )
            + "_"
            + str(job["start_chunk"])
            + "_"
            + (
                (parent.get("chunk_hashes") or [parent["id"]])[-1][:16]
                if parent
                else "origin"
            )
        )
        from contextlib import nullcontext

        capture = capture_chunk_refine_state() if emit_refine_state else nullcontext([])
        storage_scope = (
            run_storage_scope(storage_name, prompt=prompt, unique_id=unique_id)
            if run_storage != "Off"
            else nullcontext(None)
        )
        with storage_scope as storage:
            if storage is not None:
                storage.force_fresh_nonce = bool(emit_refine_state) or (
                    bool(p.get("queued_batch_id")) and int(regenerate_from) > 0
                )
                storage.excluded_revisions = {
                    r["revision_id"]
                    for r in all_takes
                    if r["id"] in rejected
                    and r.get("storage_name") == storage_name
                    and r.get("revision_id")
                }
                if previous and previous.get("storage_name") == storage_name:
                    storage.preferred_revision = previous.get("revision_id")
            native_boundary, native_nonce, seed_note = (
                take_seed_request(
                    p,
                    job,
                    all_takes,
                    previous,
                    settings_hash,
                    storage_name,
                    regenerate_from,
                    reroll_nonce,
                )
                if not emit_refine_state
                else (1, int(reroll_nonce), None)
            )
            if storage is None:
                # Takes still need an immutable continuation Session, but Off
                # does not resume or write the native Run Storage cache.
                if native_boundary and not native_nonce:
                    native_nonce = 1 + max(
                        (
                            int(
                                (r.get("sampling_contract") or {}).get(
                                    "effective_reroll_nonce", 0
                                )
                            )
                            for r in all_takes
                            if r.get("settings_hash") == settings_hash
                        ),
                        default=0,
                    )
                seed_note = "Run Storage Off: all chunks in this job are sampled."

            def checkpoint(entries, identity_hash, first_frame_hash, last_frame_hash):
                if storage is None:
                    return
                partial_record = {
                    **record,
                    "status": "partial",
                    "storage_name": storage_name,
                    "revision_id": storage.revision_id,
                    "sampling_contract": copy.deepcopy(storage.manifest["contract"]),
                }
                store.atomic_json(
                    take_path(store, p["project_id"], take_id), partial_record
                )

            provider.checkpoint = checkpoint
            with (
                continuation_method_scope(continuation_method),
                conditioning_lookahead_scope(conditioning_lookahead),
                capture as captured,
            ):
                entries, last_state, new_session, report = run_sequence(
                    model=model,
                    clip=clip,
                    video_vae=video_vae,
                    audio_vae=None,
                    sampler=sampler,
                    sigmas=sigmas,
                    first_frame=first_frame,
                    last_frame=last_frame,
                    prompt_plan=plan,
                    width=p["width"],
                    height=p["height"],
                    continuity=continuity,
                    base_seed=(int(base_seed) + job["start_chunk"])
                    & 0xFFFFFFFFFFFFFFFF,
                    audio_continuity=bool(audio_continuity),
                    exact_total_duration=False,
                    diagnostics_mode=diagnostics,
                    reroll_from_chunk=native_boundary,
                    reroll_nonce=native_nonce,
                    strict_compatibility=False,
                    debug=bool(debug),
                    enable_preview=bool(show_preview),
                    latent_only=True,
                    session=None,
                    initial_state=initial,
                    driving_audio_source=guide,
                    driving_audio_vae=audio_vae,
                    timeline_inputs=provider,
                    reference_assets=references,
                    reference_video_source=video_source,
                )
            if storage is not None:
                storage.finalize(session=new_session, report=report)
                record.update(
                    storage_name=storage_name, revision_id=storage.revision_id
                )
                record["sampling_contract"] = copy.deepcopy(
                    storage.manifest["contract"]
                )
                record["chunk_hashes"] = [
                    c["file_sha256"] for c in storage.manifest["chunks"]
                ]
                report += "\n" + storage.summary(
                    detailed=diagnostics == "Detailed Report"
                )
            else:
                record["sampling_contract"] = {
                    **contract,
                    "reroll_from_chunk": native_boundary,
                    "effective_reroll_nonce": native_nonce,
                }
                record["chunk_hashes"] = [
                    digest(
                        [
                            _tensor_sha256(e["video"]),
                            _tensor_sha256(e["audio"]),
                            e["plan"],
                        ]
                    )
                    for e in entries
                ]
        save_session(new_session, prefix=prefix, slot=1)
        record["model_inputs"] = [
            {
                "descriptor": (entry["plan"].get("physical_prompt") or {}).get(
                    "descriptor", {}
                ),
                "window": copy.deepcopy(job["windows"][i]),
                "compiled": (entry["plan"].get("physical_prompt") or {}).get(
                    "compiled", {}
                ),
                "reused": bool(entry.get("reused")),
            }
            for i, entry in enumerate(entries)
        ]
        cursor = provider.offset
        record["chunk_output_ranges"] = []
        for entry in entries:
            record["chunk_output_ranges"].append(
                [cursor, cursor + int(entry["plan"]["net_frames"])]
            )
            cursor += int(entry["plan"]["net_frames"])
        record["output_range"] = [
            provider.offset,
            provider.offset + sum(int(e["plan"]["net_frames"]) for e in entries),
        ]
        record["reused_chunks"] = sum(bool(e.get("reused")) for e in entries)
        store.atomic_json(take_path(store, p["project_id"], take_id), record)
        decode, assembly = prepare_physical_decode_entries(
            entries,
            chunk_seconds=p["chunk_frames"] / 24,
            preserve_final_frame=last_frame is not None,
            terminal_merged=False,
        )
        from .runtime import limit_assembly_to_authored_window

        limit_assembly_to_authored_window(assembly, job, provider.offset)
        videos = [{"samples": e["video"]} for e in decode]
        audios = [{"samples": e["audio"]} for e in decode]
        if emit_refine_state:
            videos, audios = _attach_refine_masks(
                enabled=True,
                captured=captured,
                video_latents=videos,
                audio_latents=audios,
            )
        refine = _refine_state_output(
            enabled=emit_refine_state,
            captured=captured,
            video_latents=videos,
            debug=bool(debug),
        )
        if reasons:
            report += "\nSession auto reuse disabled: " + "; ".join(reasons)
        if connected_prompt_warning:
            report += "\nTimeline warning: " + connected_prompt_warning
        if studio_prompt_owned:
            report += "\nPrompt source: imported Studio fields; connected sequence_prompt is ignored until explicitly re-linked."
        if anchor_note:
            report += "\n" + anchor_note
        if seed_note:
            report += "\n" + seed_note
        for w in job["windows"]:
            for warning in w.get("warnings", []):
                report += "\nTimeline warning: " + warning
        from ..v3.driving_nodes import _FINAL_AUDIO_SOURCE_PLAN_KEY
        source_mode = p.get("final_audio_source", "original")
        assembly[_FINAL_AUDIO_SOURCE_PLAN_KEY] = source_mode
        selected_audio = None
        if source_mode == "generated":
            report += "\nFinal audio: generated H3 audio selected; source audio remains available for driving conditioning."
        elif driving_audio is not None and guide is not None:
            from ..v3.driving_nodes import _DRIVING_AUDIO_PLAN_KEY, _copy_audio
            from ..driving_audio import prepare_driving_audio_source

            selected_audio = _copy_audio(
                prepare_driving_audio_source(
                    guide.source_audio,
                    audio_vae,
                    target_frames=record["output_range"][1] - record["output_range"][0],
                    fps=24,
                ).source_audio
            )
            assembly[_DRIVING_AUDIO_PLAN_KEY] = _copy_audio(selected_audio)
            report += "\nConnected Driving Audio overrides Studio guide lanes and supplies final audio for this job."
        elif driving_audio is None:
            # Native H3 sampling may retain extra frames beyond the authored
            # movie window (17k+5 frame grid). Requiring original audio over
            # that native overhang would incorrectly discard a complete source
            # soundtrack, especially at the very end of a movie.
            from ..v3.driving_nodes import _DRIVING_AUDIO_PLAN_KEY, _copy_audio

            rate = int(p.get("audio_sample_rate") or next(
                (a["audio"]["sample_rate"] for a in p["assets"]
                 if a.get("audio") and a["audio"].get("sample_rate")),
                48000,
            ))
            natural_start, natural_end = record["output_range"]
            authored_end = min(natural_end, p["selection"][1], p["duration_frames"])
            selected_audio = provider.final_output_audio(
                natural_start, natural_end, rate
            )
            if selected_audio is not None:
                selected_audio = _copy_audio(selected_audio)
                assembly[_DRIVING_AUDIO_PLAN_KEY] = _copy_audio(selected_audio)
                report += (
                    "\nTimeline final audio: original/authored soundtrack "
                    "preserved at exact movie source timestamps; generated "
                    "H3 audio bypassed by the V3.4 assembler."
                    + (
                        f" Native overhang {natural_end - authored_end} frames "
                        "is silent pending exact-duration assembly."
                        if natural_end > authored_end else ""
                    )
                )
            elif guide is not None:
                report += (
                    "\nTimeline audio: guide conditioning is present, but "
                    "no complete authored final soundtrack covers this job; "
                    "V3.4 assembler retains generated audio."
                )
        if reference_video_1 is not None and any(w["video"] for w in job["windows"]):
            report += "\nScoped video guidance overrides persistent Video Reference in its active windows."
        return videos, audios, assembly, record, report, refine, selected_audio


class H3ContinuumTimelineTake:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "audio": ("AUDIO",),
                "take": ("H3_CONTINUUM_TIMELINE_TAKE",),
            }
        }

    RETURN_TYPES = ("H3_CONTINUUM_TIMELINE_TAKE", "STRING")
    RETURN_NAMES = ("take", "status")
    FUNCTION = "commit"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def commit(self, images, audio, take):
        from .compose import write_video_frames

        store = project_store()
        record = copy.deepcopy(take)
        # Exactly the requested job duration is placed into the output cut.
        n = record["output_range"][1] - record["output_range"][0]
        if images.shape[0] < n:
            raise ValueError(
                "Save Take requires the natural retained timeline from the assembler; select Natural retained timeline before saving"
            )
        with tempfile.TemporaryDirectory(prefix="h3-take-") as directory:
            d = Path(directory)
            video = d / "video.mkv"
            sound = d / "audio.f32"
            output = d / "take.mkv"
            write_video_frames(
                (images[:n].detach().cpu().clamp(0, 1).numpy() * 255)
                .round()
                .astype(np.uint8),
                video,
            )
            rate = int(audio["sample_rate"])
            pcm = audio["waveform"][0].detach().cpu().numpy()
            count = round(n * rate / 24)
            if pcm.shape[-1] < count:
                pcm = np.pad(pcm, ((0, 0), (0, count - pcm.shape[-1])))
            pcm[:, :count].T.astype("<f4").tofile(sound)
            run_process(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-i",
                    str(video),
                    "-f",
                    "f32le",
                    "-ar",
                    str(rate),
                    "-ac",
                    str(pcm.shape[0]),
                    "-i",
                    str(sound),
                    "-c:v",
                    "copy",
                    "-c:a",
                    "pcm_f32le",
                    "-t",
                    str(n / 24),
                    str(output),
                ]
            )
            with output.open("rb") as stream:
                asset = store.assets.register(
                    stream, "Take " + record["id"][:8] + ".mkv"
                )
        rendition = {
            "take_id": record["id"],
            "asset": asset,
            "range": record["output_range"],
        }
        path = store.project_dir(record["project_id"]) / (
            "render-" + record["id"] + ".json"
        )
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) != rendition:
            raise ValueError(
                "This take already has a different rendered output; create a new take"
            )
        store.atomic_json(path, rendition)
        return {
            "ui": {
                "text": [
                    "Take " + record["id"][:8] + " ready for review in Timeline Studio"
                ]
            },
            "result": ({**record, "rendition": rendition}, "Take saved"),
        }


class H3ContinuumTimelineExport:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "timeline": ("H3_CONTINUUM_TIMELINE_PROJECT",),
                "format": (["lossless", "h264"],),
            },
            "optional": {
                "resolution": (["timeline", "source"], {
                    "default": "timeline",
                    "tooltip": "Timeline uses the H3 sampling canvas. Source uses native source pixel dimensions with composition re-encoding.",
                }),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("output_file",)
    FUNCTION = "export"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Export selected timeline frames to ComfyUI output. Returns a file path; "
        "H3 guidance travels through Timeline Project and Continuum Timeline "
        "Sampler, not through an exported movie."
    )

    def export(self, timeline, format, resolution="timeline"):
        from .compose import export_project
        import folder_paths

        p = copy.deepcopy(timeline["project"])
        path = Path(folder_paths.get_output_directory()) / (
            "Timeline_"
            + p["project_id"]
            + "_"
            + uuid.uuid4().hex[:8]
            + (".mkv" if format == "lossless" else ".mp4")
        )
        output = str(export_project(
            p, project_store(), path, codec=format, resolution=resolution
        ))
        return {
            "ui": {"text": ["Timeline exported: " + Path(output).name]},
            "result": (output,),
        }


NODE_CLASS_MAPPINGS = {
    c.__name__: c
    for c in (
        H3ContinuumTimelineStudio,
        H3ContinuumTimelineSampler,
        H3ContinuumTimelineTake,
        H3ContinuumTimelineExport,
    )
}
NODE_DISPLAY_NAME_MAPPINGS = {
    c.__name__: name
    for c, name in (
        (H3ContinuumTimelineStudio, "Continuum Timeline Studio"),
        (H3ContinuumTimelineSampler, "Continuum Timeline Sampler"),
        (H3ContinuumTimelineTake, "Continuum Timeline Save Take"),
        (H3ContinuumTimelineExport, "Continuum Timeline Export"),
    )
}

"""Request-local media provider for Continuum's resolved physical invocations."""

from __future__ import annotations

from dataclasses import replace
import copy
from collections import OrderedDict
import torch

from .media import mix_audio


def limit_assembly_to_authored_window(assembly, job, output_start=None):
    """Keep native decode topology and record a short final picture's target."""
    requested = job["range"][1] - (
        job["range"][0] if output_start is None else output_start
    )
    if 0 < requested < assembly["target_frames"]:
        assembly["timeline_requested_frames"] = requested
        assembly["target_frames"] = requested
    return assembly


def merge_references(persistent, scoped):
    """Persistent socket pictures precede window-local pictures and aliases."""
    if persistent is None:
        return scoped
    if scoped is None:
        return persistent
    from ..reference import ReferenceAssets, _canonical_hash, REFERENCE_CONTRACT_VERSION

    hashes = persistent.image_hashes + scoped.image_hashes
    return ReferenceAssets(
        persistent.images + scoped.images,
        persistent.latents + scoped.latents,
        hashes,
        _canonical_hash(
            {
                "reference_contract_version": REFERENCE_CONTRACT_VERSION,
                "size_mode": scoped.size_mode,
                "image_hashes": list(hashes),
            }
        ),
        scoped.size_mode,
    )


class TimelineInputs:
    def __init__(self, project, job, store):
        self.project = copy.deepcopy(project)
        self.job = copy.deepcopy(job)
        self.store = store
        self.windows = self.job["windows"]
        self.manifests = []
        self.conditioning_cache = {}
        self.timeline_plan = None
        self.reference_cache = OrderedDict()
        self.reference_cache_bytes = 0
        self.offset = job["range"][0]
        self.reference_size = "Match Output"
        from ..reference_video import REFERENCE_VIDEO_SIZE_EFFICIENT

        self.video_reference_size = REFERENCE_VIDEO_SIZE_EFFICIENT
        self.picture_offset = 0
        # Slot sizes follow the eight physical sampler reference sockets.
        # A V3.4 bundle can occupy one socket but contain several images.
        self.reference_slot_sizes = (0,) * 8
        self.driving_audio = None

    def picture_offset_for(self, absolute_index):
        from .model import reference_slots_for_chunk

        selection = reference_slots_for_chunk(self.project, absolute_index)
        if selection is None:
            return sum(self.reference_slot_sizes)
        return sum(self.reference_slot_sizes[slot - 1] for slot in selection)

    def select_persistent_references(self, assets, local_index):
        """Slice the already prepared native socket tensors for this H3 chunk.

        Preserve each socket's original tensor identity and reference sizing.
        Only the chosen images are sent to this invocation's conditioning.
        """
        from .model import reference_slots_for_chunk

        selection = reference_slots_for_chunk(
            self.project, self.windows[local_index]["index"]
        )
        if selection is None or assets is None:
            return assets
        indices = []
        offset = 0
        selected = set(selection)
        for slot, size in enumerate(self.reference_slot_sizes, start=1):
            if slot in selected:
                indices.extend(range(offset, offset + size))
            offset += size
        if not indices:
            return None
        from ..reference import ReferenceAssets, _canonical_hash, REFERENCE_CONTRACT_VERSION

        hashes = tuple(assets.image_hashes[i] for i in indices)
        return ReferenceAssets(
            images=tuple(assets.images[i] for i in indices),
            latents=tuple(assets.latents[i] for i in indices),
            image_hashes=hashes,
            combined_hash=_canonical_hash({
                "reference_contract_version": REFERENCE_CONTRACT_VERSION,
                "size_mode": assets.size_mode,
                "image_hashes": list(hashes),
            }),
            size_mode=assets.size_mode,
        )

    def set_offset(self, offset):
        from .model import compile_window, guide_scope_for

        self.offset = offset
        indices = [w["index"] for w in self.windows]
        scope = guide_scope_for(self.project, indices, offset=offset)
        self.job["guide_scope"] = list(scope)
        self.windows = [
            compile_window(
                self.project, i, guide_scope=scope,
                picture_offset=self.picture_offset_for(i)
            )
            for i in indices
        ]
        self.job["windows"] = self.windows

    @property
    def hashes(self):
        return [w["generation_hash"] for w in self.windows]

    @property
    def storage_contract(self):
        result = {
            "contract": 2,
            "reference_size": self.reference_size,
            "video_reference_size": self.video_reference_size,
        }
        if self.audio_generation_mode != "guided":
            from ..source_audio_lock import SOURCE_AUDIO_LOCK_CONTRACT
            result["audio_generation_mode"] = self.audio_generation_mode
            if self.audio_generation_mode == "source_locked":
                result["source_audio_lock_contract"] = SOURCE_AUDIO_LOCK_CONTRACT
        return result

    @property
    def audio_generation_mode(self):
        return self.project.get("audio_generation_mode", "guided")

    def guide_active(self, index):
        if self.audio_generation_mode == "generated":
            return False
        if self.audio_generation_mode == "source_locked":
            return True
        if self.driving_audio is not None:
            rate = int(self.driving_audio["sample_rate"])
            return self.driving_audio["waveform"].shape[-1] > round(
                self.windows[index]["range"][0] * rate / 24
            )
        return bool(self.windows[index]["guide"])

    def audio_source(self, audio_vae):
        if self.audio_generation_mode == "generated":
            return None
        if self.audio_generation_mode == "source_locked":
            return self._locked_audio_source(audio_vae)
        if self.driving_audio is None and not any(w["guide"] for w in self.windows):
            return None
        from ..driving_audio import prepare_driving_audio_source

        if audio_vae is None:
            raise ValueError("Timeline audio guidance requires the H3 Audio VAE")
        rate = int(getattr(audio_vae, "audio_sample_rate", 32000))
        self.guide_rate = rate
        a = self.offset
        b = a + self.job["range"][1] - self.job["range"][0] + 34
        self.guide_mix_range = [a, b]
        if self.driving_audio is not None:
            rate = int(self.driving_audio["sample_rate"])
            audio = {
                "waveform": self.driving_audio["waveform"][
                    ..., round(a * rate / 24) : round(b * rate / 24)
                ],
                "sample_rate": rate,
            }
        else:
            pcm = mix_audio(self.project, self.store.assets, a, b, rate, "guide")
            audio = {"waveform": torch.from_numpy(pcm)[None], "sample_rate": rate}
        if audio["waveform"].shape[-1] == 0:
            return None
        source = prepare_driving_audio_source(
            audio, audio_vae, target_frames=b - a, fps=24
        )
        self.direct_audio_contract = (
            source.contract if self.driving_audio is not None else None
        )
        return source

    def _locked_audio_source(self, audio_vae):
        from ..source_audio_lock import prepare_locked_source

        # Include real source PCM for the previous job's continuation prefix.
        # No generated-audio tail is authoritative in this mode.
        a = max(0, self.offset - 39)
        end = self.offset + self.job["range"][1] - self.job["range"][0]
        b = end + 34
        self.lock_source_origin = a
        self.guide_mix_range = [a, b]
        rate = int(getattr(audio_vae, "audio_sample_rate", 32000))
        self.guide_rate = rate
        if self.driving_audio is not None:
            source = prepare_locked_source(self.driving_audio, audio_vae,
                start_frame=a, end_frame=b, required_end_frame=min(end, self.project["duration_frames"], self.project["selection"][1]))
        else:
            if not any(w["guide"] for w in self.windows):
                raise ValueError("Source Audio Locked requires a connected Driving Audio input or an active Timeline audio guide")
            pcm = mix_audio(self.project, self.store.assets, a, b, rate, "guide")
            # prepare_locked_source uses the movie clock; this bounded mix has
            # its own origin, so validate and fingerprint in that local clock.
            source = prepare_locked_source(
                {"waveform": torch.from_numpy(pcm)[None], "sample_rate": rate}, audio_vae,
                start_frame=0, end_frame=b-a, required_end_frame=end-a)
        self.direct_audio_contract = source.contract
        return source

    def locked_audio_window(self, source, audio_vae, *, retained_frames, trim_frames, total_frames):
        from ..source_audio_lock import encode_locked_window
        return encode_locked_window(source, audio_vae,
            start_frame=self.offset + retained_frames - trim_frames - self.lock_source_origin,
            total_frames=total_frames, origin_frame=self.lock_source_origin)

    def final_audio(self, start_frame, end_frame, sample_rate):
        """Return original/authored final PCM only when it covers the job.

        Partial audio lanes retain the historical generated-audio assembly;
        silently replacing missing spans with zeros would erase speech. Direct
        sampler Driving Audio has separate, higher-priority handling.
        """
        from .model import active_clips

        if not start_frame < end_frame:
            return None
        available = sorted(
            (max(start_frame, c["start"]), min(end_frame, c["end"]))
            for track, c in active_clips(
                self.project, start_frame, end_frame, {"retained", "audio", "video_guide"}
            )
            if c["audio_role"] in {"final", "both"} and not c.get("audio_fallback")
        )
        covered = start_frame
        for a, b in available:
            if a > covered:
                break
            covered = max(covered, b)
            if covered >= end_frame:
                break
        if covered < end_frame:
            return None
        # Final source preservation must not read a previously accepted take.
        source_project = {**self.project, "tracks": [
            {**track, "clips": [c for c in track["clips"] if not c.get("audio_fallback")]}
            for track in self.project["tracks"]
            if track["role"] in {"retained", "audio", "video_guide"}
        ]}
        pcm = mix_audio(
            source_project, self.store.assets, start_frame, end_frame,
            sample_rate, "final",
        )
        return {
            "waveform": torch.from_numpy(pcm.copy())[None],
            "sample_rate": sample_rate,
        }

    def final_output_audio(self, start_frame, end_frame, sample_rate):
        """Preserve contiguous source PCM across natural multi-job boundaries.

        Native overhang inside the selected scene belongs to the next movie
        timestamps. Only frames beyond that scene's endpoint receive silence.
        """
        from .model import sample_boundary
        import torch.nn.functional as F

        source_end = min(
            end_frame, self.project["selection"][1], self.project["duration_frames"]
        )
        audio = self.final_audio(start_frame, source_end, sample_rate)
        if audio is None:
            return None
        padding = sample_boundary(end_frame, sample_rate) - sample_boundary(source_end, sample_rate)
        if padding > 0:
            audio["waveform"] = F.pad(audio["waveform"], (0, padding))
        return audio

    def invocation_plan(self, original, index):
        """Native Timeline source for a window with timed prompt regions.

        Each window is an outer section on its exact job-clock chunk boundary;
        timed regions are a strict inner partition of it. The physical
        compiler then re-bases them onto each invocation's own clock, including
        native context, without importing adjacent windows.
        """
        if not self.windows[index].get("timed_prompt"):
            return original
        if self.timeline_plan is None:
            from ..v2.prompts import make_prompt_plan
            from .model import seconds_text

            origin = self.job["range"][0]
            rows = []
            for w in self.windows:
                a, b = (x - origin for x in w["range"])
                rows.append(f"[{seconds_text(a)}-{seconds_text(b)}s]")
                if w.get("segments"):
                    for segment in w["segments"]:
                        x, y = (v - origin for v in segment["range"])
                        rows.append(f"{seconds_text(x)}-{seconds_text(y)}s:")
                        rows.append(segment["text"])
                else:
                    # Untimed windows use the nominal List plan; this body only
                    # keeps the routing partition well formed.
                    rows.append(w["prompt"] or "(no prompt)")
            self.timeline_plan = make_prompt_plan(
                mode="Timeline",
                script="\n".join(rows),
                chunks=len(self.windows),
                chunk_seconds=self.project["chunk_frames"] / 24,
            )
        return self.timeline_plan

    def materialize(self, index, *, video_vae, audio_vae, width, height):
        from ..reference import prepare_reference_assets, encode_reference_latents
        from ..reference_video import (
            prepare_reference_video_source,
            encode_reference_video,
        )
        from ..reference_audio import (
            prepare_reference_audio_source,
            encode_reference_audio,
        )
        from ..temporal import align_frame_count_up

        w = self.windows[index]
        a, b = w["range"]
        from ..reference import (
            ReferenceAssets,
            _canonical_hash,
            REFERENCE_CONTRACT_VERSION,
        )

        prepared = []
        for item in w["images"]:
            key = (item["asset_id"], width, height, self.reference_size)
            cached = self.reference_cache.pop(key, None)
            if cached is None:
                from PIL import Image
                import numpy as np

                with Image.open(self.store.assets.verify(item["asset_id"])) as im:
                    image = (
                        torch.from_numpy(np.asarray(im.convert("RGB")).copy()).float()[
                            None
                        ]
                        / 255
                    )
                single = prepare_reference_assets(
                    reference_image_1=image,
                    reference_image_2=None,
                    output_width=width,
                    output_height=height,
                    size_mode=self.reference_size,
                )
                single = encode_reference_latents(video_vae, single)
                single = replace(
                    single,
                    latents=tuple(
                        t.detach().cpu().contiguous() for t in single.latents
                    ),
                )
                size = sum(
                    t.numel() * t.element_size()
                    for t in (*single.images, *single.latents)
                )
                cached = (single, size)
                self.reference_cache_bytes += size
            self.reference_cache[key] = cached
            prepared.append(cached[0])
            while (
                self.reference_cache_bytes > 128 * 1024 * 1024 and self.reference_cache
            ):
                _, (_, size) = self.reference_cache.popitem(last=False)
                self.reference_cache_bytes -= size
        refs = None
        if prepared:
            hashes = tuple(a.image_hashes[0] for a in prepared)
            refs = ReferenceAssets(
                tuple(a.images[0] for a in prepared),
                tuple(a.latents[0] for a in prepared),
                hashes,
                _canonical_hash(
                    {
                        "reference_contract_version": REFERENCE_CONTRACT_VERSION,
                        "size_mode": self.reference_size,
                        "image_hashes": list(hashes),
                    }
                ),
                self.reference_size,
            )
        video_source = video_assets = None
        if w["video"]:
            # Timeline video lanes compose by explicit top-track precedence.
            from .compose import render_frames
            from ..reference_video import _resolved_size

            rw, rh = _resolved_size(
                width,
                height,
                output_width=width,
                output_height=height,
                size_mode=self.video_reference_size,
            )
            frames = render_frames(
                self.project,
                self.store.assets,
                a,
                b,
                rw,
                rh,
                roles={"video_guide"},
                gap="hold",
            )
            frames = torch.from_numpy(frames).float() / 255
            physical = align_frame_count_up(b - a)
            if physical > b - a:
                frames = torch.cat(
                    [frames, frames[-1:].expand(physical - (b - a), -1, -1, -1)]
                )
            video_source = prepare_reference_video_source(
                frames,
                target_frames=physical,
                output_width=width,
                output_height=height,
                size_mode=self.video_reference_size,
            )
            video_assets = encode_reference_video(video_vae, video_source)
        voice_source = voice_assets = None
        if w["voice"]:
            if audio_vae is None:
                raise ValueError("Timeline voice references require the Audio VAE")
            rate = int(getattr(audio_vae, "audio_sample_rate", 32000))
            pcm = mix_audio(self.project, self.store.assets, a, b, rate, "reference")
            voice_source = prepare_reference_audio_source(
                {"waveform": torch.from_numpy(pcm)[None], "sample_rate": rate},
                audio_vae,
            )
            voice_assets = encode_reference_audio(audio_vae, voice_source)
        return refs, video_source, video_assets, voice_source, voice_assets

    def can_prefetch(self, index):
        return not (self.windows[index]["video"] or self.windows[index]["voice"])

    def video_routing_receipt(self, index, source, assets, origin):
        """Describe the selected guide without copying or hashing media again."""
        return {
            "chunk": index + 1,
            "logical_range": self.windows[index]["range"],
            "origin": origin,
            "source_windows": self.windows[index]["video"]
            if origin == "timeline_window" else [],
            "input_shape": list(source.source_shape) if source is not None else None,
            "encoded_frames": source.frame_count if source is not None else 0,
            "source_sha256": source.source_sha256 if source is not None else None,
            "reference_hash": source.combined_hash if source is not None else None,
            "latent_shape": list(assets.block["latent"].shape)
            if assets is not None else None,
        }

    def annotate(self, descriptor, index, *, record=True):
        w = self.windows[index]
        presentation = dict(descriptor.presentation_contract)
        presentation["timeline_studio"] = {
            "contract": 1,
            "generation_hash": w["generation_hash"],
            "logical_range": w["range"],
            "job_offset": self.offset,
            "guide_source_windows": w["video"],
            "audio_guide": w["guide"],
            "guide_padding": "hold nearest guide frame across uncovered logical frames and to the native 17k+5 grid",
            "physical_output_range": [
                descriptor.retained_before + self.offset,
                descriptor.retained_before
                + descriptor.total_frames
                - descriptor.context_frames
                + self.offset,
            ],
        }
        if hasattr(self, "guide_rate"):
            from .model import sample_boundary, active_clips, clip_input

            start = max(0, descriptor.retained_before - descriptor.context_frames)
            presentation["timeline_studio"]["audio_guide"] = {
                "active": self.guide_active(index),
                "input": "driving_audio"
                if self.driving_audio is not None
                else "timeline_guide_mix",
                "direct_source_contract": self.direct_audio_contract,
                "sample_rate": self.guide_rate,
                "encoder_mix_global_samples": [
                    sample_boundary(f, self.guide_rate) for f in self.guide_mix_range
                ],
                "requested_global_samples": [
                    sample_boundary(self.offset + start, self.guide_rate),
                    sample_boundary(
                        self.offset + start + descriptor.total_frames, self.guide_rate
                    ),
                ],
                "latent_slice_ticks_40hz": [
                    round(start * 40 / 24),
                    round((start + descriptor.total_frames) * 40 / 24),
                ],
                "contributing_source_clips": []
                if self.driving_audio is not None
                else [
                    clip_input(
                        self.project,
                        t,
                        c,
                        self.offset + start,
                        self.offset + start + descriptor.total_frames,
                    )
                    for t, c in active_clips(
                        self.project,
                        self.offset + start,
                        self.offset + start + descriptor.total_frames,
                    )
                    if c["audio_role"] in {"guide", "both"}
                ],
                "gap_policy": "silence where no active guide clip; final mix is independent",
            }
        # Reconstruct through the native descriptor factory to recalculate digest.
        from ..v2.physical_prompts import make_physical_sample_descriptor

        d = make_physical_sample_descriptor(
            group_id=descriptor.group_id,
            logical_indices=descriptor.logical_indices,
            retained_before=descriptor.retained_before,
            context_frames=descriptor.context_frames,
            total_frames=descriptor.total_frames,
            target_duration_frames=descriptor.target_duration_frames,
            continuation_method=descriptor.continuation_method,
            initial_state_origin=descriptor.initial_state_origin,
            include_first=descriptor.include_first,
            include_last=descriptor.include_last,
            presentation_contract=presentation,
            exact_protected=bool(descriptor.exact_protected_interval),
            guided_overlap=bool(descriptor.guided_overlap_interval),
        )
        if record:
            self.manifests.append(
                {"descriptor": d.semantic_dict(), "window": copy.deepcopy(w)}
            )
        return d

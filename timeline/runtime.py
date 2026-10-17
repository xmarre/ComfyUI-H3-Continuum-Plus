"""Request-local media provider for Continuum's resolved physical invocations."""
from __future__ import annotations

from dataclasses import replace
import copy
import torch

from .media import check_cancel, mix_audio, video_frames
from .model import digest, rational, source_time


class TimelineInputs:
    def __init__(self, project, job, store):
        self.project = copy.deepcopy(project)
        self.job = copy.deepcopy(job)
        self.store = store
        self.windows = self.job["windows"]
        self.manifests = []
        self.offset = job["range"][0]

    @property
    def hashes(self):
        return [w["generation_hash"] for w in self.windows]

    def guide_active(self, index):
        return bool(self.windows[index]["guide"])

    def audio_source(self, audio_vae):
        if not any(w["guide"] for w in self.windows):
            return None
        from ..driving_audio import prepare_driving_audio_source
        if audio_vae is None:
            raise ValueError("Timeline audio guidance requires the H3 Audio VAE")
        rate = int(getattr(audio_vae, "audio_sample_rate", 32000))
        a, b = self.job["range"]
        pcm = mix_audio(self.project, self.store.assets, a, b, rate, "guide")
        audio = {"waveform": torch.from_numpy(pcm)[None], "sample_rate": rate}
        return prepare_driving_audio_source(audio, audio_vae, target_frames=b-a, fps=24)

    def materialize(self, index, *, video_vae, audio_vae, width, height):
        from ..reference import prepare_reference_assets, encode_reference_latents, bundle_reference_images
        from ..reference_video import prepare_reference_video_source, encode_reference_video
        from ..reference_audio import prepare_reference_audio_source, encode_reference_audio
        from ..temporal import align_frame_count_up
        w = self.windows[index]
        a, b = w["range"]
        images = []
        for item in w["images"]:
            path = self.store.assets.path(item["asset_id"])
            # Source image geometry is retained before the established H3 resize.
            from PIL import Image
            import numpy as np
            with Image.open(path) as im:
                images.append(torch.from_numpy(np.asarray(im.convert("RGB")).copy()).float()[None] / 255)
        refs = prepare_reference_assets(reference_image_1=None, reference_image_2=None,
                    reference_image_3=bundle_reference_images(*images), output_width=width, output_height=height, size_mode="Match Output")
        if refs is not None:
            refs = encode_reference_latents(video_vae, refs)
        video_source = video_assets = None
        if w["video"]:
            # Timeline video lanes compose by explicit top-track precedence.
            from .compose import render_frames
            frames = render_frames(self.project, self.store.assets, a, b, width, height, roles={"video_guide"}, gap="error")
            frames = torch.from_numpy(frames).float() / 255
            physical = align_frame_count_up(b-a)
            if physical > b-a:
                frames = torch.cat([frames, frames[-1:].expand(physical-(b-a),-1,-1,-1)])
            video_source = prepare_reference_video_source(frames, target_frames=physical, output_width=width, output_height=height)
            video_assets = encode_reference_video(video_vae, video_source)
        voice_source = voice_assets = None
        if w["voice"]:
            if audio_vae is None:
                raise ValueError("Timeline voice references require the Audio VAE")
            rate = int(getattr(audio_vae, "audio_sample_rate", 32000))
            pcm = mix_audio(self.project, self.store.assets, a, b, rate, "reference")
            voice_source = prepare_reference_audio_source({"waveform": torch.from_numpy(pcm)[None], "sample_rate": rate}, audio_vae)
            voice_assets = encode_reference_audio(audio_vae, voice_source)
        return refs, video_source, video_assets, voice_source, voice_assets

    def annotate(self, descriptor, index):
        w = self.windows[index]
        presentation = dict(descriptor.presentation_contract)
        presentation["timeline_studio"] = {"contract": 1, "generation_hash": w["generation_hash"],
                "logical_range": w["range"], "job_offset": self.offset,
                "guide_source_windows": w["video"], "audio_guide": w["guide"],
                "guide_padding": "hold last logical guide frame to native 17k+5 grid",
                "physical_output_range": [descriptor.retained_before + self.offset,
                                          descriptor.retained_before + descriptor.total_frames - descriptor.context_frames + self.offset]}
        # Reconstruct through the native descriptor factory to recalculate digest.
        from ..v2.physical_prompts import make_physical_sample_descriptor
        d = make_physical_sample_descriptor(group_id=descriptor.group_id, logical_indices=descriptor.logical_indices,
            retained_before=descriptor.retained_before, context_frames=descriptor.context_frames,
            total_frames=descriptor.total_frames, target_duration_frames=descriptor.target_duration_frames,
            continuation_method=descriptor.continuation_method, initial_state_origin=descriptor.initial_state_origin,
            include_first=descriptor.include_first, include_last=descriptor.include_last,
            presentation_contract=presentation,
            exact_protected=bool(descriptor.exact_protected_interval), guided_overlap=bool(descriptor.guided_overlap_interval))
        self.manifests.append({"descriptor": d.semantic_dict(), "window": copy.deepcopy(w)})
        return d

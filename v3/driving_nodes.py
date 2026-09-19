"""Public V3.4 Driving Audio sampler and assembler nodes."""

from __future__ import annotations

import torch

from ..constants import FPS, V2_CONTINUITY_OPTIONS
from ..driving_audio import prepare_driving_audio_source
from ..masked_continuation import (
    CONTINUATION_METHODS,
    CONTINUATION_NATIVE_MASKED,
    continuation_method_scope,
    validate_native_masked_request,
)
from ..reference import bundle_reference_images
from ..reference_video import (
    REFERENCE_VIDEO_SIZE_EFFICIENT,
    REFERENCE_VIDEO_SIZE_OPTIONS,
)
from ..v2.decoder import enforce_total_frames
from .assembly import (
    H3ContinuumAssembleSeamExperimental,
    finalize_assembled_timeline,
)
from .nodes import CATEGORY as CONTINUUM_CATEGORY, H3ContinuumSamplerProduction


_DRIVING_AUDIO_PLAN_KEY = "_h3_continuum_driving_audio_v1"
REFINE_STATE_API = 1
V34_CONTINUITY_STRONG = "Strong — 39 frames"
V34_CONTINUITY_OPTIONS = (
    V2_CONTINUITY_OPTIONS[0],
    V2_CONTINUITY_OPTIONS[1],
    V2_CONTINUITY_OPTIONS[2],
    V34_CONTINUITY_STRONG,
    V2_CONTINUITY_OPTIONS[3],
)
V34_TIMELINE_EXACT = "Exact requested duration (Recommended)"
V34_TIMELINE_NATURAL = "Natural retained timeline (Refinement)"
V34_TIMELINE_MODES = (V34_TIMELINE_EXACT, V34_TIMELINE_NATURAL)


def _normalize_v34_continuity(value: str) -> str:
    """Map the V3.4 display value onto the inherited sequence wire contract."""
    value = str(value)
    if value == V34_CONTINUITY_STRONG:
        return V2_CONTINUITY_OPTIONS[3]
    return value


def _unwrap_single_audio_value(value):
    while isinstance(value, list):
        if len(value) != 1:
            return None
        value = value[0]
    return value


def _copy_audio(value):
    value = _unwrap_single_audio_value(value)
    if not isinstance(value, dict):
        return None
    waveform = value.get("waveform")
    sample_rate = value.get("sample_rate")
    if not isinstance(waveform, torch.Tensor) or sample_rate is None:
        return None
    return {
        "waveform": waveform.detach().to("cpu").contiguous().clone(),
        "sample_rate": int(sample_rate),
    }


def _driving_audio_from_plan(args, kwargs):
    value = kwargs.get("assembly_plan")
    if value is None and len(args) >= 3:
        value = args[2]
    value = _unwrap_single_audio_value(value)
    if not isinstance(value, dict):
        return None
    return _copy_audio(value.get(_DRIVING_AUDIO_PLAN_KEY))


def _validate_refine_alignment(*, enabled, captured, video_latents):
    if not enabled:
        return
    if not isinstance(video_latents, list):
        raise ValueError("Continuum video_latents output must be a chunk list")
    expected = len(video_latents)
    actual = len(captured)
    if actual != expected:
        reused = max(0, expected - actual)
        detail = (
            f"{reused} of {expected} output chunk(s) were reused without sampling in this execution"
            if reused
            else f"captured {actual} refinement state object(s) for {expected} output chunk(s)"
        )
        raise ValueError(
            "Exact Refine State cannot be emitted because "
            f"{detail}. Continuum intentionally does not persist raw sampler-boundary state in Run Storage. "
            "Set Run Storage = Off, or use Regenerate From = Chunk 1 for this run. "
            "Refusing to pair a generated state suffix with the full latent list."
        )


def _copy_mask_tensor(value):
    if value is None:
        return None
    if not torch.is_tensor(value):
        raise TypeError(f"Continuum denoise mask member must be torch.Tensor, got {type(value).__name__}")
    return value.detach().to("cpu").contiguous().clone()


def _split_noise_mask(value):
    if value is None:
        return None, None
    if torch.is_tensor(value):
        return value, None
    unbind = getattr(value, "unbind", None)
    if not callable(unbind):
        raise TypeError(f"Continuum joint denoise mask must be NestedTensor-like, got {type(value).__name__}")
    members = list(unbind())
    if len(members) != 2:
        raise ValueError(
            "Continuum MiniMax H3 joint denoise mask must contain [video_mask, audio_mask]"
        )
    return members[0], members[1]


def _attach_refine_masks(*, enabled, captured, video_latents, audio_latents):
    """Restore sampler-1 denoise masks onto V3.4's intentionally split LATENT outputs."""
    if not enabled:
        return video_latents, audio_latents
    _validate_refine_alignment(
        enabled=True,
        captured=captured,
        video_latents=video_latents,
    )
    if not isinstance(audio_latents, list) or len(audio_latents) != len(video_latents):
        raise ValueError("Continuum audio_latents output is not aligned with video_latents")

    video_out = []
    audio_out = []
    for index, record in enumerate(captured):
        video_item = dict(video_latents[index])
        audio_item = dict(audio_latents[index])
        video_mask, audio_mask = _split_noise_mask(record.get("noise_mask"))
        if video_mask is not None:
            video_item["noise_mask"] = _copy_mask_tensor(video_mask)
        if audio_mask is not None:
            audio_item["noise_mask"] = _copy_mask_tensor(audio_mask)
        video_out.append(video_item)
        audio_out.append(audio_item)
    return video_out, audio_out


def _fresh_refine_model(sampled_model, *, debug: bool):
    """Clone sampler-1's exact patched MODEL state but install a fresh Continuum wrapper closure."""
    from ..model_patch import configure_continuum_model

    if sampled_model is None or not hasattr(sampled_model, "clone"):
        raise TypeError("Captured Continuum chunk MODEL is not cloneable")
    fresh = sampled_model.clone()
    return configure_continuum_model(
        fresh,
        strict=False,
        debug=bool(debug),
    )


def _refine_state_output(*, enabled, captured, video_latents, debug: bool):
    """Build one exact, fresh MODEL+CONDITIONING refinement contract per chunk."""
    if not enabled:
        return []
    _validate_refine_alignment(
        enabled=True,
        captured=captured,
        video_latents=video_latents,
    )
    output = []
    for record in captured:
        positive = record.get("positive")
        if positive is None:
            raise ValueError("Captured Continuum refine state is missing positive conditioning")
        output.append(
            {
                "api": REFINE_STATE_API,
                "model": _fresh_refine_model(record.get("model"), debug=bool(debug)),
                "positive": positive,
            }
        )
    return output


class H3ContinuumSamplerV34(H3ContinuumSamplerProduction):
    """V3.4 sampler with native masked continuation and Driving Audio."""

    DEPRECATED = False
    CATEGORY = CONTINUUM_CATEGORY
    DESCRIPTION = (
        "H3 Continuum V3.4 with native masked AV continuation by default, optional "
        "Guide / Motion Context continuation, hybrid First/Last + Reference Image "
        "conditioning, Driving Audio, persistent Video Reference, and opt-in exact "
        "per-chunk refinement state for learned latent upscaling."
    )
    SEARCH_ALIASES = [
        "H3 Continuum Sampler V3.4",
        "MiniMax H3 Driving Audio",
        "MiniMax H3 masked continuation",
    ]
    RETURN_TYPES = (
        "LATENT",
        "LATENT",
        "H3_CONTINUUM_ASSEMBLY_PLAN",
        "STRING",
        "AUDIO",
        "H3_CONTINUUM_REFINE_STATE",
    )
    RETURN_NAMES = (
        "video_latents",
        "audio_latents",
        "assembly_plan",
        "status",
        "driving_audio",
        "refine_state",
    )
    OUTPUT_IS_LIST = (True, True, False, False, False, True)

    @classmethod
    def INPUT_TYPES(cls):
        schema = super().INPUT_TYPES()
        required = dict(schema.get("required", {}))
        required["continuity"] = (
            V34_CONTINUITY_OPTIONS,
            {
                "default": V34_CONTINUITY_STRONG,
                "tooltip": (
                    "Protected previous-chunk context. Native Masked generated-audio "
                    "continuation requires the exact 39-frame AV boundary; video-only "
                    "continuation and Guide / Motion Context also support 5 or 22 frames."
                ),
            },
        )
        required["video_reference_size"] = (
            REFERENCE_VIDEO_SIZE_OPTIONS,
            {
                "default": REFERENCE_VIDEO_SIZE_EFFICIENT,
                "display_name": "Video Reference Size",
                "tooltip": (
                    "Efficient limits Video Reference to about 0.4 MP; Balanced uses "
                    "about 0.6 MP; Match Output uses the output pixel area. Source "
                    "aspect ratio is preserved and smaller sources are not enlarged."
                ),
            },
        )
        required["continuation_method"] = (
            CONTINUATION_METHODS,
            {
                "default": CONTINUATION_NATIVE_MASKED,
                "display_name": "Continuation Method",
                "tooltip": (
                    "Native Masked preserves the previous generated H3 latent directly "
                    "inside the next target and is recommended for exact same-shot continuation. "
                    "Guide / Motion Context keeps the previous clip as softer H3 guide context."
                ),
            },
        )
        # Appended after all existing V3.4 widgets to preserve serialized positions.
        # The frontend keeps it hidden and drives it from the refine_state output link.
        required["emit_refine_conditioning"] = (
            "BOOLEAN",
            {
                "default": False,
                "advanced": True,
                "display_name": "Emit Refine State",
                "tooltip": (
                    "Expose exact per-chunk refinement state: a fresh Continuum MODEL wrapper, "
                    "the exact positive CONDITIONING passed to sampler 1, and the matching native "
                    "denoise masks on video/audio LATENT outputs. Enable only for downstream "
                    "learned-latent refinement. If Run Storage reused chunks, regenerate from "
                    "Chunk 1 or disable Run Storage so every state remains exactly aligned."
                ),
            },
        )
        optional = dict(schema.get("optional", {}))
        optional.pop("reference_audio_1", None)
        optional.pop("reference_audio_vae", None)
        # The managed prompt sidecar was added to the inherited V3 schema after
        # V3.4 already had serialized dynamic Reference Image 4..8 sockets.
        # Re-append it only after every pre-existing V3.4 optional input so saved
        # workflows keep the established positional socket topology.
        managed_prompt_source = optional.pop("managed_prompt_source_json", None)
        for index in range(4, 9):
            optional[f"reference_image_{index}"] = ("IMAGE",)
        optional["reference_video_1"] = (
            "IMAGE",
            {
                "display_name": "Video Reference",
                "tooltip": (
                    "Optional persistent video reference. Connect an IMAGE frame batch; "
                    "frames are interpreted at 24 fps and applied to every chunk."
                ),
            },
        )
        optional["driving_audio"] = (
            "AUDIO",
            {
                "tooltip": (
                    "Optional original audio timeline. It is used as native H3 guide "
                    "conditioning and selected unchanged for final output."
                )
            },
        )
        optional["audio_vae"] = (
            "VAE",
            {
                "tooltip": (
                    "Required only when Driving Audio is connected. Uses the same "
                    "Audio VAE encode path as ComfyUI Core MiniMax H3 Add Guide."
                )
            },
        )
        if managed_prompt_source is not None:
            optional["managed_prompt_source_json"] = managed_prompt_source
        schema["required"] = required
        schema["optional"] = optional
        return schema

    def run(
        self,
        driving_audio=None,
        audio_vae=None,
        reference_video_1=None,
        video_reference_size=REFERENCE_VIDEO_SIZE_EFFICIENT,
        continuation_method=CONTINUATION_NATIVE_MASKED,
        **kwargs,
    ):
        from ..reference_video import prepare_reference_video_source
        from ..temporal import align_frame_count_up
        from ..v2.sampling import capture_chunk_refine_state

        emit_refine_state = bool(kwargs.pop("emit_refine_conditioning", False))
        debug = bool(kwargs.get("debug", False))

        continuity = _normalize_v34_continuity(
            kwargs.get("continuity", V34_CONTINUITY_STRONG)
        )
        chunks = int(kwargs.get("chunks", 1))
        initial_terminal_flf = (
            chunks == 2
            and abs(float(kwargs.get("chunk_seconds", 0.0)) - 5.0) <= 1e-6
            and kwargs.get("first_frame") is not None
            and kwargs.get("last_frame") is not None
        )
        validate_native_masked_request(
            method=continuation_method,
            continuity=continuity,
            audio_continuity=bool(kwargs.get("audio_continuity", True)),
            driving_audio_active=driving_audio is not None,
            # A 2x5 FL2VA terminal request is one physical initial sample and has
            # no protected inter-sample AV boundary to validate.
            chunks=1 if initial_terminal_flf else chunks,
        )
        kwargs["continuity"] = continuity

        extra_references = [
            kwargs.pop(f"reference_image_{index}", None)
            for index in range(4, 9)
        ]
        if any(image is not None for image in extra_references):
            kwargs["reference_image_3"] = bundle_reference_images(
                kwargs.get("reference_image_3"),
                *extra_references,
            )

        target_frames = round(
            int(kwargs["chunks"]) * float(kwargs["chunk_seconds"]) * FPS
        )
        source = prepare_driving_audio_source(
            driving_audio,
            audio_vae,
            target_frames=target_frames,
            fps=FPS,
        )
        reference_video_source = prepare_reference_video_source(
            reference_video_1,
            target_frames=align_frame_count_up(
                int(round(float(kwargs["chunk_seconds"]) * FPS))
            ),
            output_width=int(kwargs["width"]),
            output_height=int(kwargs["height"]),
            size_mode=str(video_reference_size),
        )

        def run_sampler():
            with continuation_method_scope(continuation_method):
                return super(H3ContinuumSamplerV34, self).run(
                    reference_audio_1=None,
                    reference_audio_vae=None,
                    driving_audio_source=source,
                    driving_audio_vae=audio_vae,
                    reference_video_source=reference_video_source,
                    **kwargs,
                )

        if emit_refine_state:
            with capture_chunk_refine_state() as captured_refine_state:
                outputs = run_sampler()
        else:
            captured_refine_state = []
            outputs = run_sampler()

        video_latents, audio_latents = _attach_refine_masks(
            enabled=emit_refine_state,
            captured=captured_refine_state,
            video_latents=outputs[0],
            audio_latents=outputs[1],
        )
        outputs = (video_latents, audio_latents, *outputs[2:])
        refine_state = _refine_state_output(
            enabled=emit_refine_state,
            captured=captured_refine_state,
            video_latents=video_latents,
            debug=debug,
        )

        selected_audio = _copy_audio(source.source_audio) if source is not None else None
        if selected_audio is not None and len(outputs) >= 3 and isinstance(outputs[2], dict):
            assembly_plan = dict(outputs[2])
            assembly_plan[_DRIVING_AUDIO_PLAN_KEY] = _copy_audio(selected_audio)
            outputs = (*outputs[:2], assembly_plan, *outputs[3:])
        return (*outputs, selected_audio, refine_state)


class H3ContinuumAssembleSeamV34(H3ContinuumAssembleSeamExperimental):
    """Assemble decoded chunks and select original Driving Audio when connected."""

    DEPRECATED = False
    CATEGORY = CONTINUUM_CATEGORY
    DESCRIPTION = (
        "V3.4 decoded-chunk assembler. Driving Audio bypasses generated audio "
        "and Audio Seam while video seam correction remains available."
    )

    @classmethod
    def INPUT_TYPES(cls):
        schema = super().INPUT_TYPES()
        required = dict(schema["required"])
        required["timeline_mode"] = (
            V34_TIMELINE_MODES,
            {
                "default": V34_TIMELINE_EXACT,
                "display_name": "Timeline Output",
                "tooltip": (
                    "Exact requested duration keeps the normal V3.4 output. Natural retained "
                    "timeline preserves every physical-group frame for downstream refinement; "
                    "finish that branch with H3 Continuum Finalize Duration V3.4."
                ),
            },
        )
        schema["required"] = required
        optional = dict(schema.get("optional", {}))
        optional["driving_audio"] = (
            "AUDIO",
            {
                "tooltip": (
                    "Connect the sampler Driving Audio output. When present, generated "
                    "audio and Audio Seam are bypassed."
                )
            },
        )
        schema["optional"] = optional
        return schema

    def assemble(self, *args, driving_audio=None, timeline_mode=None, **kwargs):
        if timeline_mode is not None:
            timeline_mode = _unwrap_single_audio_value(timeline_mode)
            if timeline_mode not in V34_TIMELINE_MODES:
                raise ValueError(f"unknown V3.4 Timeline Output mode: {timeline_mode!r}")
            exact_value = timeline_mode == V34_TIMELINE_EXACT
            if len(args) >= 4:
                args = (*args[:3], exact_value, *args[4:])
            else:
                kwargs["exact_total_duration"] = exact_value
        preserved_audio = _driving_audio_from_plan(args, kwargs)
        images, audio, report = super().assemble(*args, **kwargs)
        selected = preserved_audio or _copy_audio(driving_audio)
        if selected is None:
            return images, audio, report

        plan_value = kwargs.get("assembly_plan")
        if plan_value is None and len(args) >= 3:
            plan_value = args[2]
        while isinstance(plan_value, list):
            if len(plan_value) != 1:
                raise ValueError("assembly_plan must contain exactly one value")
            plan_value = plan_value[0]
        exact = kwargs.get("exact_total_duration")
        if exact is None and len(args) >= 4:
            exact = args[3]
        while isinstance(exact, list):
            if len(exact) != 1:
                raise ValueError("exact_total_duration must contain exactly one value")
            exact = exact[0]
        if bool(exact):
            _, selected, _ = enforce_total_frames(
                images,
                selected,
                target_frames=int(plan_value["target_frames"]),
                preserve_final_frame=bool(plan_value.get("preserve_final_frame", False)),
            )
        source_name = "assembly plan" if preserved_audio is not None else "direct input"
        samples = int(selected["waveform"].shape[-1])
        report = str(report) + (
            f"\nDriving Audio: preserved source selected from {source_name}; "
            f"sample_rate={selected['sample_rate']}, samples={samples}; "
            "generated audio and Audio Seam bypassed."
        )
        return images, selected, report


class H3ContinuumFinalizeDurationV34:
    """Finalize a natural post-refinement timeline with Continuum's exact policy."""

    DEPRECATED = False
    CATEGORY = CONTINUUM_CATEGORY
    DESCRIPTION = (
        "Apply the assembly plan's exact target duration after stitch-back, including "
        "final-frame preservation and sample-aligned audio trim/pad semantics."
    )
    SEARCH_ALIASES = ["H3 post stitch duration", "H3 refinement finalizer"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "audio": ("AUDIO",),
                "assembly_plan": ("H3_CONTINUUM_ASSEMBLY_PLAN",),
            }
        }

    RETURN_TYPES = ("IMAGE", "AUDIO", "STRING")
    RETURN_NAMES = ("images", "audio", "report")
    FUNCTION = "finalize"

    def finalize(self, images, audio, assembly_plan):
        return finalize_assembled_timeline(
            images=images,
            audio=audio,
            assembly_plan=assembly_plan,
        )


NODE_CLASS_MAPPINGS = {
    "H3ContinuumSamplerV34": H3ContinuumSamplerV34,
    "H3ContinuumAssembleSeamV34": H3ContinuumAssembleSeamV34,
    "H3ContinuumFinalizeDurationV34": H3ContinuumFinalizeDurationV34,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "H3ContinuumSamplerV34": "H3 Continuum Sampler V3.4",
    "H3ContinuumAssembleSeamV34": "H3 Continuum Assemble + Seam V3.4",
    "H3ContinuumFinalizeDurationV34": "H3 Continuum Finalize Duration V3.4",
}

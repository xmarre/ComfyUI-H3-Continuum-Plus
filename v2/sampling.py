"""ComfyUI-native chunk sampling helpers."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import torch

from ..state import extract_av_streams
from .determinism_witness import emit_sampler_witness


class SamplingRuntimeError(RuntimeError):
    pass


_CONDITIONING_CAPTURE: ContextVar[list[list] | None] = ContextVar(
    "h3_continuum_conditioning_capture",
    default=None,
)
_REFINE_STATE_CAPTURE: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "h3_continuum_refine_state_capture",
    default=None,
)


@contextmanager
def capture_chunk_conditioning():
    """Capture exact CONDITIONING objects passed to sampled chunks in this context."""
    captured: list[list] = []
    token = _CONDITIONING_CAPTURE.set(captured)
    try:
        yield captured
    finally:
        _CONDITIONING_CAPTURE.reset(token)


@contextmanager
def capture_chunk_refine_state():
    """Capture exact transient state required to refine each sampled chunk later."""
    captured: list[dict[str, Any]] = []
    token = _REFINE_STATE_CAPTURE.set(captured)
    try:
        yield captured
    finally:
        _REFINE_STATE_CAPTURE.reset(token)


def _record_chunk_conditioning(conditioning: list) -> None:
    """Record one sampler conditioning object when conditioning capture is active."""
    target = _CONDITIONING_CAPTURE.get()
    if target is not None:
        target.append(conditioning)


def _record_chunk_refine_state(*, model: Any, conditioning: list, noise_mask: Any) -> None:
    """Record exact sampler-boundary MODEL/conditioning/mask without reconstruction."""
    target = _REFINE_STATE_CAPTURE.get()
    if target is not None:
        target.append(
            {
                "model": model,
                "positive": conditioning,
                "noise_mask": noise_mask,
            }
        )


def _make_basic_guider(model: Any, conditioning: list):
    try:
        import comfy.samplers
    except Exception as exc:  # pragma: no cover
        raise SamplingRuntimeError(f"ComfyUI sampler API unavailable: {exc}") from exc

    class _BasicGuider(comfy.samplers.CFGGuider):
        def set_positive(self, positive):
            self.inner_set_conds({"positive": positive})

    guider = _BasicGuider(model)
    guider.set_positive(conditioning)
    return guider


def _prepare_noise(latent: dict[str, Any], seed: int):
    try:
        import comfy.sample
    except Exception as exc:  # pragma: no cover
        raise SamplingRuntimeError(f"ComfyUI noise API unavailable: {exc}") from exc
    batch_inds = latent.get("batch_index")
    return comfy.sample.prepare_noise(latent["samples"], int(seed), batch_inds)


def sample_chunk(
    *,
    model: Any,
    conditioning: list,
    latent: dict[str, Any],
    sampler: Any,
    sigmas: torch.Tensor,
    seed: int,
    enable_preview: bool = True,
) -> dict[str, Any]:
    """Run one H3 chunk using the same path as SamplerCustomAdvanced."""

    try:
        import comfy.model_management
        import comfy.sample
        import comfy.utils
        import latent_preview
    except Exception as exc:  # pragma: no cover
        raise SamplingRuntimeError(f"ComfyUI sampling runtime unavailable: {exc}") from exc

    if not isinstance(latent, dict) or "samples" not in latent:
        raise SamplingRuntimeError("latent must be a ComfyUI LATENT dictionary")
    if not torch.is_tensor(sigmas) or sigmas.ndim != 1 or sigmas.numel() < 2:
        raise SamplingRuntimeError("sigmas must contain at least two values")

    working = latent.copy()
    latent_image = working["samples"]
    latent_image = comfy.sample.fix_empty_latent_channels(
        model,
        latent_image,
        working.get("downscale_ratio_spacial"),
        working.get("downscale_ratio_temporal"),
    )
    working["samples"] = latent_image
    noise_mask = working.get("noise_mask")
    _record_chunk_conditioning(conditioning)
    _record_chunk_refine_state(
        model=model,
        conditioning=conditioning,
        noise_mask=noise_mask,
    )
    guider = _make_basic_guider(model, conditioning)
    noise = _prepare_noise(working, seed)
    emit_sampler_witness(
        "before_sample",
        seed=seed,
        conditioning=conditioning,
        latent_image=latent_image,
        noise=noise,
        noise_mask=noise_mask,
        sampler_name=f"{type(sampler).__module__}.{type(sampler).__qualname__}",
        sigmas=sigmas,
    )

    x0_output: dict[str, Any] = {}
    callback = None
    if enable_preview:
        callback = latent_preview.prepare_callback(model, int(sigmas.shape[-1]) - 1, x0_output)
    disable_pbar = not comfy.utils.PROGRESS_BAR_ENABLED
    samples = guider.sample(
        noise,
        latent_image,
        sampler,
        sigmas,
        denoise_mask=noise_mask,
        callback=callback,
        disable_pbar=disable_pbar,
        seed=int(seed),
    )
    samples = samples.to(comfy.model_management.intermediate_device())
    emit_sampler_witness("after_sample", seed=seed, samples=samples)
    output = working.copy()
    output.pop("downscale_ratio_spacial", None)
    output.pop("downscale_ratio_temporal", None)
    output["samples"] = samples
    # Validate the nested AV shape immediately so a bad sampler output cannot be
    # committed into a session and fail much later during continuation.
    extract_av_streams(output)
    return output


def latent_to_cpu(latent: dict[str, Any]) -> tuple[torch.Tensor, torch.Tensor]:
    video, audio = extract_av_streams(latent)
    video_cpu = video.detach().to("cpu").contiguous().clone()
    audio_cpu = audio.detach().to("cpu").contiguous().clone()
    if not bool(torch.isfinite(video_cpu.float()).all().item()):
        raise SamplingRuntimeError("sampled video latent contains NaN or Inf")
    if not bool(torch.isfinite(audio_cpu.float()).all().item()):
        raise SamplingRuntimeError("sampled audio latent contains NaN or Inf")
    return video_cpu, audio_cpu


def latent_from_cpu(video: torch.Tensor, audio: torch.Tensor) -> dict[str, Any]:
    try:
        import comfy.nested_tensor
    except Exception as exc:  # pragma: no cover
        raise SamplingRuntimeError(f"ComfyUI NestedTensor API unavailable: {exc}") from exc
    return {
        "samples": comfy.nested_tensor.NestedTensor(
            (video.contiguous(), audio.contiguous())
        )
    }

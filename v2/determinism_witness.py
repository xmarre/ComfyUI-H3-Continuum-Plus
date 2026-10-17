"""Opt-in read-only fingerprint of the tensors actually handed to each H3 sampler.

This is a repeatability *witness*, not a deterministic execution guarantee.
No tensor is changed, no RNG is consumed, and no hashes are computed by default.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

import torch

_LOG = logging.getLogger("h3_continuum_join")
_ENV = "H3C_DETERMINISM_WITNESS"
_POLICY = "sampler_tensor_identity_v1"


def enabled() -> bool:
    return os.environ.get(_ENV) == "1"


def _tensor_sha256(tensor: torch.Tensor) -> str:
    """Hash full tensor content plus shape and dtype, without changing its storage."""
    sample = tensor.detach().to("cpu").contiguous()
    digest = hashlib.sha256()
    digest.update(str(tuple(int(n) for n in sample.shape)).encode("ascii"))
    digest.update(str(sample.dtype).encode("ascii"))
    digest.update(sample.view(torch.uint8).numpy().tobytes(order="C"))
    return digest.hexdigest()


def _collect(value: Any, path: str, out: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("determinism witness exceeds nesting depth")
    if getattr(value, "is_nested", False):
        # ComfyUI AV and PyTorch nested tensors may both be tensor subclasses.
        for index, child in enumerate(value.unbind()):
            _collect(child, f"{path}[{index}]", out, depth + 1)
    elif torch.is_tensor(value):
        out.append({
            "path": path, "type": "tensor", "shape": list(value.shape),
            "dtype": str(value.dtype), "sha256": _tensor_sha256(value),
        })
    elif type(value) in (list, tuple):
        for index, child in enumerate(value):
            _collect(child, f"{path}[{index}]", out, depth + 1)
    elif type(value) is dict:
        for key in sorted(value, key=lambda item: str(item)):
            _collect(value[key], f"{path}[{key!s}]", out, depth + 1)
    elif value is None or type(value) in (str, int, float, bool):
        # Never write raw prompt strings or scalar metadata to the log.
        token = f"{type(value).__name__}:{value!r}"
        out.append({
            "path": path, "type": "scalar",
            "sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        })
    else:
        # Python object identities and reprs are process-dependent; do not hash them.
        out.append({
            "path": path, "type": "opaque",
            "class": f"{type(value).__module__}.{type(value).__qualname__}",
        })


def fingerprint(value: Any) -> dict[str, Any]:
    leaves: list[dict[str, Any]] = []
    _collect(value, "value", leaves)
    canonical = json.dumps(leaves, sort_keys=True, separators=(",", ":"))
    return {
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "tensors": [leaf for leaf in leaves if leaf["type"] == "tensor"],
        "other_count": sum(leaf["type"] != "tensor" for leaf in leaves),
    }


def emit_sampler_witness(stage: str, *, seed: int, **values: Any) -> None:
    """Best-effort observation only: any witness error must preserve sampling."""
    if not enabled():
        return
    try:
        if stage not in ("before_sample", "after_sample"):
            raise ValueError("invalid sampler witness stage")
        receipt = {
            "policy": _POLICY,
            "stage": stage,
            "resolved_seed": int(seed),
            "values": {
                key: fingerprint(value)
                for key, value in sorted(values.items())
            },
            "limits": "Model weights, CUDA kernel determinism and runtime cache state are not verified.",
        }
        _LOG.info("H3C-PT233 sampler-determinism receipt %s",
                  json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    except Exception:
        _LOG.exception("H3C-PT233 sampler-determinism receipt failed at %s", stage)

"""H3 Continuum — native MiniMax H3 chunk continuation."""

from __future__ import annotations

import logging
import os

WEB_DIRECTORY = "./web"

# PR #22 is itself the opt-in surface when applied through ComfyUI-Patcher.
# Do not require a second, invisible shell-level activation step: default the
# physical prompt compiler on for this PR while retaining an explicit env
# override for bisect/recovery (for example H3_CONTINUUM_PHYSICAL_PROMPTS=0).
_PHYSICAL_PROMPT_ENV = "H3_CONTINUUM_PHYSICAL_PROMPTS"

# ComfyUI loads custom-node folders as packages. Standalone test collection may
# import this file as a top-level module; keep that path inert because ComfyUI is
# intentionally not a unit-test dependency.
if __package__:
    os.environ.setdefault(_PHYSICAL_PROMPT_ENV, "1")

    from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
    from .temporal import run_temporal_self_test
    from .version import PACKAGE_VERSION

    try:
        run_temporal_self_test()
    except Exception as exc:
        raise RuntimeError(f"H3 Continuum Join self-test failed: {exc}") from exc
    logging.getLogger("h3_continuum_join").info(
        "H3 Continuum %s loaded (V2 integrated sampler + hidden legacy workflow compatibility; PR22 physical prompts default=%s)",
        PACKAGE_VERSION,
        os.environ.get(_PHYSICAL_PROMPT_ENV, ""),
    )
else:  # pragma: no cover
    NODE_CLASS_MAPPINGS = {}
    NODE_DISPLAY_NAME_MAPPINGS = {}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

"""H3 Continuum — native MiniMax H3 chunk continuation."""

from __future__ import annotations

import logging
import os

WEB_DIRECTORY = "./web"

_PHYSICAL_PROMPT_ENV = "H3_CONTINUUM_PHYSICAL_PROMPTS"
_PHYSICAL_TIMELINE_VIDEO_ENV = "H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO"


def _physical_timeline_video_disabled() -> bool:
    """Physical Timeline Video transport is not part of this production release."""

    return False


# ComfyUI loads custom-node folders as packages. Standalone test collection may
# import this file as a top-level module; keep that path inert because ComfyUI is
# intentionally not a unit-test dependency.
if __package__:
    # Physical text transport is the supported runtime path. Keep an explicit
    # disable for emergency bisect/recovery while avoiding a second hidden
    # activation step in normal ComfyUI use.
    os.environ.setdefault(_PHYSICAL_PROMPT_ENV, "1")

    # Descriptor-aware physical Timeline Video extraction remains parked. Force
    # the old experimental environment knob off and replace every imported gate
    # with a stable false policy so changing the environment after package load
    # cannot reactivate a partially split implementation.
    os.environ[_PHYSICAL_TIMELINE_VIDEO_ENV] = "0"

    from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
    from .temporal import run_temporal_self_test
    from .version import PACKAGE_VERSION
    from .v2 import physical_prompts as _physical_prompts
    from .v2 import physical_sequence as _physical_sequence
    from .v2 import sequence as _sequence

    _physical_prompts.physical_timeline_video_enabled = _physical_timeline_video_disabled
    _physical_sequence.physical_timeline_video_enabled = _physical_timeline_video_disabled
    _sequence.physical_timeline_video_enabled = _physical_timeline_video_disabled

    try:
        run_temporal_self_test()
    except Exception as exc:
        raise RuntimeError(f"H3 Continuum Join self-test failed: {exc}") from exc
    logging.getLogger("h3_continuum_join").info(
        "H3 Continuum %s loaded (V2 integrated sampler + hidden legacy workflow compatibility; physical text transport=%s; physical Timeline Video transport=disabled)",
        PACKAGE_VERSION,
        os.environ.get(_PHYSICAL_PROMPT_ENV, ""),
    )
else:  # pragma: no cover
    NODE_CLASS_MAPPINGS = {}
    NODE_DISPLAY_NAME_MAPPINGS = {}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

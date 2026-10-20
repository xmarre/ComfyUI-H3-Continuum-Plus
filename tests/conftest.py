from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# Unit fixtures use the standalone legacy default. Tests of physical transport
# opt in explicitly, independently of the checkout directory's import name.
os.environ.setdefault("H3_CONTINUUM_PHYSICAL_PROMPTS", "0")

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "ComfyUI_H3_Continuum_Join"
if PACKAGE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[PACKAGE_NAME] = module

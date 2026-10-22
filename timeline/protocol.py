"""No-scan Timeline Studio transport contract, independent of aiohttp/ComfyUI."""

from pathlib import Path
from urllib.parse import quote

NATIVE_PATH_CONTRACT = "h3-timeline-smart-references-v30"


def native_runtime_manifest(extension_dirs, web_root):
    """Describe the exact registered frontend without traversing source drives."""
    expected = Path(web_root).resolve()
    matches = [
        name for name, directory in extension_dirs.items()
        if Path(directory).resolve() == expected
    ]
    if len(matches) != 1:
        raise ValueError(
            "Timeline Studio frontend registration is missing or ambiguous"
        )
    prefix = "/extensions/" + quote(matches[0], safe="") + "/"
    return {
        "contract": NATIVE_PATH_CONTRACT,
        "transport": "native-selected-path",
        "drive_scan": False,
        "browser_movie_upload": False,
        "editor_url": prefix + "timeline/editor.js",
        "entry_url": prefix + "timeline_studio.js",
    }

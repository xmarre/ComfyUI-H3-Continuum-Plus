"""Resolve browser-dropped media to original files on WSL-mounted drives.

Only the declared basename/length and cryptographic chunks are accepted from
the browser. No browser-provided paths are interpreted and no bytes are copied.
The second pass checks *every* byte before an asset identity is published.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import time

CHUNK_BYTES = 16 * 1024 * 1024
MAX_BYTES = 128 * 1024**3
MAX_SEARCH_SECONDS = 600


def wsl_drive_roots():
    """Mounted Windows drive letters only; never scan the entire WSL root."""
    mount_root = Path("/mnt")
    if not mount_root.is_dir():
        return ()
    return tuple(
        sorted(
            (
                path.resolve()
                for path in mount_root.iterdir()
                if re.fullmatch(r"[a-zA-Z]", path.name)
                and path.is_dir()
                and os.path.ismount(path)
            ),
            key=str,
        )
    )


def validate_fingerprint(value):
    if not isinstance(value, dict):
        raise ValueError("Dropped file fingerprint must be an object")
    name, size = value.get("name"), value.get("size")
    digests = value.get("chunks")
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 255
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 < size <= MAX_BYTES
        or value.get("chunk_size") != CHUNK_BYTES
        or not isinstance(digests, list)
        or len(digests) != (size + CHUNK_BYTES - 1) // CHUNK_BYTES
        or any(
            not isinstance(h, str) or re.fullmatch("[0-9a-f]{64}", h) is None
            for h in digests
        )
    ):
        raise ValueError("Invalid dropped file fingerprint")
    return name, size, tuple(digests)


def _quick_match(path, size, hashes, cancel):
    """Discard same-named, same-length files without hashing each entirely."""
    indices = sorted({0, len(hashes) // 2, len(hashes) - 1})
    with path.open("rb") as source:
        for index in indices:
            cancel()
            source.seek(index * CHUNK_BYTES)
            block = source.read(min(CHUNK_BYTES, size - index * CHUNK_BYTES))
            if hashlib.sha256(block).hexdigest() != hashes[index]:
                return False
    return True


def find_original(fingerprint, roots, *, cancel=lambda: None):
    """Find exactly one matching accessible original; never guess a path.

    First compare filesize/name, then widely spaced chunk digests. AssetStore
    independently verifies the complete chunk sequence before publishing.
    """
    name, size, hashes = validate_fingerprint(fingerprint)
    roots = tuple(dict.fromkeys(Path(root).resolve() for root in roots))
    if not roots:
        raise ValueError(
            "No server-accessible media drives found. Check WSL drive mounts."
        )
    deadline = time.monotonic() + MAX_SEARCH_SECONDS
    matching = []
    seen_files = set()
    for root in roots:
        if not root.is_dir():
            continue

        def onerror(_error):
            # Windows system directories may be unreadable by an unprivileged
            # ComfyUI server. Only accessible candidates can be linked.
            cancel()

        for directory, dirs, filenames in os.walk(
            root, topdown=True, followlinks=False, onerror=onerror
        ):
            cancel()
            if time.monotonic() > deadline:
                raise ValueError(
                    "Timed out locating the original movie on mounted drives; "
                    "no movie was uploaded."
                )
            # Never recurse into directory symlinks or another mounted volume.
            dirs[:] = [
                d for d in dirs
                if not (Path(directory) / d).is_symlink()
            ]
            for filename in filenames:
                if filename.casefold() != name.casefold():
                    continue
                path = Path(directory) / filename
                try:
                    identity = path.stat(follow_symlinks=False)
                    if (
                        not stat.S_ISREG(identity.st_mode)
                        or identity.st_size != size
                        or (identity.st_dev, identity.st_ino) in seen_files
                    ):
                        continue
                    seen_files.add((identity.st_dev, identity.st_ino))
                    resolved = path.resolve(strict=True)
                    if not resolved.is_relative_to(root):
                        continue
                    if _quick_match(resolved, size, hashes, cancel):
                        matching.append(resolved)
                        if len(matching) > 1:
                            raise ValueError(
                                "Multiple identical candidates found on mounted "
                                "drives; original location is ambiguous. "
                                "No movie was uploaded."
                            )
                except OSError:
                    continue
    if not matching:
        raise ValueError(
            "Original movie not found on WSL-mounted Windows drives. "
            "No movie was uploaded or copied."
        )
    return matching[0], hashes

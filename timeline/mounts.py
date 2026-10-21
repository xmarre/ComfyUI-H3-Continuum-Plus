"""Enumerate mounted Windows drive letters; never inspect their contents."""
from __future__ import annotations
import os
from pathlib import Path
import re


def wsl_drive_roots(mount_root=None):
    mount_root = Path("/mnt") if mount_root is None else Path(mount_root)
    if not mount_root.is_dir():
        return ()
    return tuple(
        sorted(
            (
                entry.resolve()
                for entry in mount_root.iterdir()
                if re.fullmatch(r"[a-zA-Z]", entry.name)
                and entry.is_dir()
                and os.path.ismount(entry)
            ),
            key=lambda entry: entry.name.lower(),
        )
    )

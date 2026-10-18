"""Resolve browser-dropped media to original files on WSL-mounted drives.

Only the declared basename/length and cryptographic chunks are accepted from
the browser. No browser-provided paths are interpreted and no bytes are copied.
The second pass checks *every* byte before an asset identity is published.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import subprocess
import time

CHUNK_BYTES = 16 * 1024 * 1024
MAX_BYTES = 128 * 1024**3
MAX_SEARCH_SECONDS = 55
INDEX_TIMEOUT_SECONDS = 8
# Windows filesystem traversal through /mnt/<drive> is considerably more
# expensive than Windows' native indexed search. Never blindly spend ten
# minutes crawling C: on every drag.
SKIP_SYSTEM_DIRS = frozenset({
    "windows", "program files", "program files (x86)", "programdata",
    "system volume information", "$recycle.bin", "$windows.~bt",
    "recovery", "msocache",
})
MEDIA_DIRS = (
    "Videos", "Downloads", "Desktop", "Documents", "Movies", "Media",
    "Films", "Filme", "Video", "Clips",
)


def wsl_drive_roots(mount_root=None):
    """Mounted Windows drive letters only; never scan the entire WSL root."""
    mount_root = Path("/mnt") if mount_root is None else Path(mount_root)
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
            # Data drives are usually smaller to traverse than Windows C:.
            key=lambda path: (path.name.lower() == "c", path.name.lower()),
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


def _index_script(name, size):
    """Windows Search SQL, not a filesystem walk; input never executes as code."""
    escaped_name = name.replace("'", "''")
    sql = (
        "SELECT TOP 256 System.ItemPathDisplay FROM SYSTEMINDEX "
        f"WHERE System.FileName = '{escaped_name}' AND System.Size = {size}"
    )
    encoded_sql = base64.b64encode(sql.encode("utf-8")).decode("ascii")
    return (
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$db = $null; $rs = $null\n"
        "try {\n"
        "  $db = New-Object -ComObject ADODB.Connection\n"
        "  $db.Open(\"Provider=Search.CollatorDSO;Extended Properties='Application=Windows';\")\n"
        f"  $sql = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_sql}'))\n"
        "  $rs = $db.Execute($sql)\n"
        "  while (-not $rs.EOF) {\n"
        "    [Console]::WriteLine([string]$rs.Fields.Item('System.ItemPathDisplay').Value)\n"
        "    $rs.MoveNext()\n"
        "  }\n"
        "} catch { } finally {\n"
        "  if ($rs -ne $null) { $rs.Close() }\n"
        "  if ($db -ne $null) { $db.Close() }\n"
        "}\n"
    )


def windows_index_candidates(name, size, roots, *, cancel=lambda: None):
    """Best-effort Windows Search index, accessed through built-in WSL interop.

    On a system without indexed drives, disabled Windows Search or blocked
    PowerShell interop, return no candidates and use bounded directory lookup.
    A fast empty index is never interpreted as proof that a file is absent.
    """
    exe = shutil.which("powershell.exe")
    if not exe:
        return ()
    script = _index_script(name, size)
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        process = subprocess.Popen(
            [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
    except OSError:
        return ()
    try:
        deadline = time.monotonic() + INDEX_TIMEOUT_SECONDS
        while True:
            cancel()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ()
            try:
                output, _ = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode or len(output) > 1024 * 1024:
            return ()
        root_map = {
            root.name.casefold(): root
            for root in roots
            if re.fullmatch(r"[a-zA-Z]", root.name)
        }
        results = []
        for value in output.decode("utf-8-sig", errors="replace").splitlines():
            # Accept drive-qualified paths only. Never use a UNC, relative,
            # device or arbitrary browser-controlled path from the index.
            path = PureWindowsPath(value)
            drive = path.drive
            if not re.fullmatch(r"[a-zA-Z]:", drive):
                continue
            root = root_map.get(drive[0].casefold())
            if root:
                results.append(root.joinpath(*path.parts[1:]))
        return tuple(results)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def _candidate(path, *, roots, name, size, hashes, seen, cancel):
    """A matching source must be a regular file inside one approved root."""
    try:
        st = path.stat(follow_symlinks=False)
        identity = (st.st_dev, st.st_ino) if st.st_ino else str(path)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_size != size
            or path.name.casefold() != name.casefold()
            or identity in seen
        ):
            return None
        seen.add(identity)
        real = path.resolve(strict=True)
        if not any(real.is_relative_to(root) for root in roots):
            return None
        return real if _quick_match(real, size, hashes, cancel) else None
    except OSError:
        return None


def _priority_dirs(roots):
    """Nearby media paths are tested before expensive general recursion."""
    for root in roots:
        yield root
        for directory in MEDIA_DIRS:
            yield root / directory
        # Windows profile directories are common landing spots for Explorer
        # downloads. This is a shallow scan, not recursive traversal.
        profiles = root / "Users"
        if profiles.is_dir():
            try:
                with os.scandir(profiles) as entries:
                    for item in entries:
                        if not item.is_dir(follow_symlinks=False):
                            continue
                        for directory in MEDIA_DIRS:
                            yield Path(item.path) / directory
            except OSError:
                pass


def find_original(fingerprint, roots, *, cancel=lambda: None,
                  indexed_candidates=windows_index_candidates, progress=None):
    """Find a matching original promptly without exhausting mounted drives.

    Query the native Windows Search index first, then check likely media
    directories, then attempt a bounded background traversal. The *first*
    cryptographically sampled candidate is returned immediately; AssetStore
    verifies every supplied chunk before committing a path pointer. Other
    identical copies are interchangeable by content identity but their actual
    path is not knowable from the standard browser File API.
    """
    name, size, hashes = validate_fingerprint(fingerprint)
    roots = tuple(dict.fromkeys(Path(root).resolve() for root in roots))
    if not roots:
        raise ValueError("No server-accessible media drives found")
    seen = set()
    deadline = time.monotonic() + MAX_SEARCH_SECONDS

    def report(message):
        if progress is not None:
            progress(message)

    report("Querying Windows Search index")
    for path in indexed_candidates(name, size, roots, cancel=cancel):
        cancel()
        match = _candidate(Path(path), roots=roots, name=name, size=size,
                           hashes=hashes, seen=seen, cancel=cancel)
        if match:
            return match, hashes

    # Search direct common folders without walking system trees first.
    report("Checking common movie and download folders")
    for directory in _priority_dirs(roots):
        cancel()
        if time.monotonic() >= deadline:
            break
        try:
            path = directory / name
            # NTFS is case-insensitive, but Python/DrvFS case sensitivity
            # varies. Probe direct path and local filename entries.
            if path.is_file():
                match = _candidate(path, roots=roots, name=name, size=size,
                                   hashes=hashes, seen=seen, cancel=cancel)
                if match:
                    return match, hashes
            if directory.is_dir():
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.name.casefold() != name.casefold():
                            continue
                        match = _candidate(Path(entry.path), roots=roots,
                                           name=name, size=size, hashes=hashes,
                                           seen=seen, cancel=cancel)
                        if match:
                            return match, hashes
        except OSError:
            continue

    for root in roots:
        if not root.is_dir():
            continue
        folders_scanned = 0
        last_reported = 0
        drive_label = (
            root.name.upper() + ":" if re.fullmatch(r"[a-zA-Z]", root.name)
            else "ComfyUI input"
        )
        report(f"Searching unindexed {drive_label} folders")

        def onerror(_error):
            cancel()

        for directory, dirs, filenames in os.walk(
            root, topdown=True, followlinks=False, onerror=onerror
        ):
            cancel()
            folders_scanned += 1
            now = time.monotonic()
            if now >= deadline:
                raise ValueError(
                    "Source lookup exceeded 55 seconds on unindexed Windows "
                    "drives. The movie was not copied or uploaded."
                )
            if now - last_reported >= 2:
                report(f"Searching {drive_label}: {folders_scanned:,} folders checked")
                last_reported = now
            # Do not recursively enter Windows installations, caches or
            # symlinked folders. They contain large numbers of irrelevant
            # files and can be very slow over WSL DrvFS.
            dirs[:] = [
                d for d in dirs
                if not (Path(directory) / d).is_symlink()
                and not (
                    root.name.casefold() == "c"
                    and d.casefold() in SKIP_SYSTEM_DIRS
                )
            ]
            for filename in filenames:
                if filename.casefold() != name.casefold():
                    continue
                match = _candidate(Path(directory) / filename, roots=roots,
                                   name=name, size=size, hashes=hashes,
                                   seen=seen, cancel=cancel)
                if match:
                    return match, hashes

    raise ValueError(
        "Original movie not found in the Windows Search index or "
        "mounted drives; nothing was uploaded or copied."
    )

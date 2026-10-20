"""Import a prompt manifest, pre-cut pictures and original soundtrack from ZIP.

Archive paths identify members only; they are never filesystem destinations.
The returned project is published by the editor as one undoable scene edit.
"""

from __future__ import annotations

from fractions import Fraction
import hashlib
import json
from pathlib import PurePosixPath
import subprocess
import stat
import uuid
import zipfile

from .model import blank_project, rational, validate

MAX_ARCHIVE_BYTES = 16 * 1024**3
MAX_UNPACKED_BYTES = 32 * 1024**3
MAX_MANIFEST_BYTES = 4 * 1024**2
MAX_MEMBERS = 8192


def member_path(name):
    if not isinstance(name, str):
        raise ValueError("Clip-pack filenames must be text")
    path = PurePosixPath(name)
    if (not isinstance(name, str) or not name or "\\" in name
            or path.is_absolute() or ".." in path.parts
            or ":" in name or str(path) != name):
        raise ValueError("Clip-pack filenames must be relative archive paths")
    return path


def read_manifest(archive):
    """Locate exactly one global_prefix/segments manifest, ignoring notes."""
    members = archive.infolist()
    if len(members) > MAX_MEMBERS:
        raise ValueError("Clip pack contains too many files")
    names = [m.filename for m in members]
    if len(set(names)) != len(names):
        raise ValueError("Clip pack contains duplicate member names")
    for member in members:
        member_path(member.filename.rstrip("/") if member.is_dir() else member.filename)
        if stat.S_ISLNK(member.external_attr >> 16):
            raise ValueError("Clip packs do not support symbolic-link members")
    if sum(m.file_size for m in members) > MAX_UNPACKED_BYTES:
        raise ValueError("Clip pack exceeds the unpacked size limit")
    found = []
    for member in members:
        if not member.filename.lower().endswith(".json"):
            continue
        if member.file_size > MAX_MANIFEST_BYTES:
            raise ValueError("Clip-pack JSON exceeds the manifest size limit")
        try:
            value = json.loads(archive.read(member).decode("utf-8-sig"))
        except (UnicodeError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get("segments"), list):
            found.append((member_path(member.filename), value))
    if len(found) != 1:
        raise ValueError("Clip pack requires one JSON manifest with segments")
    return found[0]


def import_bundle(path, assets, current, *, cancel=lambda: None):
    """Register referenced bytes and build one source-clock scene at 24 fps."""
    try:
        with zipfile.ZipFile(path) as archive:
            manifest_path, manifest = read_manifest(archive)
            base = manifest_path.parent
            segments = manifest["segments"]
            if not segments or len(segments) > 4096:
                raise ValueError("Clip-pack segments must contain 1–4096 entries")
            prefix = manifest.get("global_prefix", "")
            if not isinstance(prefix, str):
                raise ValueError("Clip-pack global_prefix must be text")
            prepared = []
            notes = []
            end = Fraction(0)
            for index, segment in enumerate(segments):
                if not isinstance(segment, dict):
                    raise ValueError("Clip-pack segments must be objects")
                start = rational(segment["start_seconds"])
                stop = rational(segment["end_seconds"])
                if start != end or stop <= start:
                    raise ValueError("Clip-pack segments must be contiguous from zero")
                if "duration_seconds" in segment and rational(segment["duration_seconds"]) != stop-start:
                    raise ValueError("Clip-pack duration_seconds disagrees with its interval")
                if not isinstance(segment.get("prompt"), str):
                    raise ValueError("Clip-pack segment prompt must be text")
                name = str(base / member_path(segment["filename"]))
                member = archive.getinfo(name)
                if member.is_dir() or member.flag_bits & 1:
                    raise ValueError("Clip pack requires readable picture files")
                a, b = round(start * 24), round(stop * 24)
                if Fraction(a, 24) != start or Fraction(b, 24) != stop:
                    notes.append(
                        f"Segment {index + 1}: {float(start):.6f}–{float(stop):.6f}s "
                        f"maps to frames [{a}, {b}) at 24 fps "
                        f"({a/24:.6f}–{b/24:.6f}s); playback speed is unchanged."
                    )
                if b <= a:
                    raise ValueError("Clip-pack segment is shorter than one output frame")
                prepared.append((segment, name, a, b))
                end = stop
            size = prepared[0][3]
            # The native sampler has one logical chunk length; only the last
            # picture may be shorter. Absolute boundaries prevent roundoff drift.
            if (not 96 <= size <= 360
                    or any(a != i*size or (b-a != size and i != len(prepared)-1)
                           or b-a > size for i, (_, _, a, b) in enumerate(prepared))):
                raise ValueError("Clip pack needs uniform 4–15 second chunks and an optional shorter last chunk")
            audio_name = manifest.get("audio_filename")
            if audio_name is not None:
                audio_name = str(base / member_path(audio_name))
                archive.getinfo(audio_name)
            else:
                candidates = [m.filename for m in archive.infolist()
                              if PurePosixPath(m.filename).parent == base
                              and PurePosixPath(m.filename).suffix.lower()
                              in {".m4a", ".wav", ".flac", ".mp3", ".aac", ".ogg", ".opus"}]
                if len(candidates) > 1:
                    raise ValueError("Multiple soundtracks: specify audio_filename in the manifest")
                audio_name = candidates[0] if candidates else None

            registered = {}

            def register(name):
                if name in registered:
                    return registered[name]
                temporary = assets.upload_path()
                checksum = hashlib.sha256()
                try:
                    with archive.open(name) as source, temporary.open("xb") as output:
                        while block := source.read(1024**2):
                            cancel()
                            output.write(block)
                            checksum.update(block)
                    cancel()
                    asset = assets.adopt(temporary, checksum.hexdigest(), PurePosixPath(name).name, cancel=cancel)
                    asset["storage"] = "managed"
                    registered[name] = asset
                    return asset
                finally:
                    temporary.unlink(missing_ok=True)

            p = blank_project()
            for key in ("project_id", "revision", "width", "height"):
                if key in current:
                    p[key] = current[key]
            p.update(name=manifest_path.stem, duration_frames=prepared[-1][3],
                     chunk_frames=size, operation="recreate", selection=[0, prepared[-1][3]])
            p["prompts"]["global"] = prefix
            movie = {"id": uuid.uuid4().hex, "role": "retained", "name": "Imported source pictures", "clips": []}
            prompts = {"id": uuid.uuid4().hex, "role": "prompt", "name": "Imported chunk prompts", "clips": []}
            p["tracks"] = [movie, prompts]
            for i, (segment, name, a, b) in enumerate(prepared):
                asset = register(name)
                if not asset.get("video"):
                    raise ValueError(f"Clip-pack picture has no video stream: {name}")
                fps = asset["video"].get("avg_frame_rate")
                fps = rational(fps) if fps and fps != "0/0" else Fraction(24)
                tolerance = Fraction(1, 24) + 1 / fps
                if abs(rational(asset["duration"]) - (rational(segment["end_seconds"])-rational(segment["start_seconds"]))) > tolerance:
                    raise ValueError(f"Clip-pack picture duration disagrees with manifest: {name}")
                movie["clips"].append({
                    "id": uuid.uuid4().hex, "asset_id": asset["id"],
                    "start": a, "end": b,
                    "source_in": str(rational(asset["video"].get("start_time") or "0")),
                    "speed": "1", "padding": "hold", "audio_padding": "silence",
                    "audio_role": "neither" if audio_name else "both" if asset.get("audio") else "neither",
                })
                prompts["clips"].append({
                    "id": uuid.uuid4().hex, "start": a, "end": b,
                    "chunk_prompt": True, "chunk_index": i, "override": True,
                    "text": segment["prompt"], "audio_role": "neither",
                })
            if audio_name:
                audio = register(audio_name)
                if not audio.get("audio"):
                    raise ValueError("Clip-pack soundtrack has no audio stream")
                if rational(audio["duration"]) < Fraction(p["duration_frames"], 24):
                    notes.append(
                        "Original soundtrack ends before the picture timeline; "
                        "the uncovered final interval uses silence, not generated H3 audio."
                    )
                p["audio_sample_rate"] = int(audio["audio"]["sample_rate"])
                p["tracks"].append({"id": uuid.uuid4().hex, "role": "audio",
                    "name": "Original soundtrack · H3 driving + final", "clips": [{
                        "id": uuid.uuid4().hex, "asset_id": audio["id"], "start": 0,
                        "end": p["duration_frames"], "source_in": str(rational(audio["audio"].get("start_time") or "0")),
                        "speed": "1", "padding": "silence", "audio_role": "both",
                    }]})
            p["assets"] = list({a["id"]: a for a in registered.values()}.values())
            p["import_notes"] = notes
            return validate(p)
    except (zipfile.BadZipFile, KeyError, TypeError, ZeroDivisionError,
            subprocess.CalledProcessError, RuntimeError) as exc:
        raise ValueError(f"Unreadable clip pack: {exc}") from exc

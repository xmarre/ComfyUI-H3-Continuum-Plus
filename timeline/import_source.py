"""Bounded import staging and detection shared by every import source."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from .bundle import (
    MAX_ARCHIVE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_MEMBERS,
    MAX_UNPACKED_BYTES,
    member_path,
)
from .import_project import apply_components
from .model import rational
from .prompt_import import parse_prompt_source

TEXT_EXT = {".json", ".txt", ".md"}
VIDEO_EXT = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
    ".m4v",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
}
AUDIO_EXT = {".m4a", ".wav", ".flac", ".mp3", ".aac", ".ogg", ".opus"}


def natural(value):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", value)]


class ImportSources:
    def __init__(self, store):
        self.store = store
        self.root = store.root / "imports"
        self.root.mkdir(parents=True, exist_ok=True)
        for entry in self.root.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < time.time() - 24 * 3600:
                    entry.unlink()
            except OSError:
                pass

    def stage_path(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ValueError("Invalid import source identifier")
        path = self.root / token
        if not path.is_file():
            raise ValueError("Import source has expired; select the file again")
        return path

    def detect(self, path, name, *, cancel=lambda: None):
        path = Path(path)
        if path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Import exceeds the 16 GiB upload limit")
        entries = []
        documents = []
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if (
                    len(members) > MAX_MEMBERS
                    or sum(m.file_size for m in members) > MAX_UNPACKED_BYTES
                ):
                    raise ValueError(
                        "Archive exceeds the member-count or unpacked-size limit"
                    )
                if len({m.filename for m in members}) != len(members):
                    raise ValueError("Archive contains duplicate member names")
                for m in members:
                    cancel()
                    member_path(m.filename.rstrip("/") if m.is_dir() else m.filename)
                    if stat.S_ISLNK(m.external_attr >> 16):
                        raise ValueError("Archive symbolic links are not supported")
                    if m.is_dir():
                        continue
                    suffix = PurePosixPath(m.filename).suffix.lower()
                    if suffix in TEXT_EXT:
                        if m.file_size > MAX_MANIFEST_BYTES:
                            raise ValueError(
                                f"Prompt document exceeds 4 MiB: {m.filename}"
                            )
                        try:
                            text = archive.read(m).decode("utf-8-sig")
                        except (UnicodeError, RuntimeError, zipfile.BadZipFile) as exc:
                            raise ValueError(
                                f"Cannot read prompt document: {m.filename}"
                            ) from exc
                        documents.append({"name": m.filename, "text": text})
                        if (
                            sum(len(d["text"].encode("utf-8")) for d in documents)
                            > 32 * 1024**2
                        ):
                            raise ValueError(
                                "Archive prompt documents exceed the 32 MiB text limit"
                            )
                    elif suffix in VIDEO_EXT | AUDIO_EXT:
                        entries.append(
                            {
                                "name": m.filename,
                                "component": "soundtrack"
                                if suffix in AUDIO_EXT
                                else "video",
                            }
                        )
            kind = "zip"
        elif Path(name).suffix.lower() in TEXT_EXT:
            if path.stat().st_size > MAX_MANIFEST_BYTES:
                raise ValueError("Prompt document exceeds 4 MiB")
            try:
                text = path.read_text(encoding="utf-8-sig")
            except UnicodeError as exc:
                raise ValueError("Prompt documents must use UTF-8 text") from exc
            documents = [{"name": name, "text": text}]
            kind = "text"
        elif Path(name).suffix.lower() in VIDEO_EXT | AUDIO_EXT:
            entries = [
                {
                    "name": name,
                    "component": "soundtrack"
                    if Path(name).suffix.lower() in AUDIO_EXT
                    else "video",
                }
            ]
            kind = "media"
        else:
            raise ValueError(
                "Choose a ZIP, JSON, TXT, Markdown, video, image or audio file"
            )
        grouped = {}
        for document in documents:
            file = PurePosixPath(document["name"])
            match = re.fullmatch(
                r"(?:chunk|clip)[ _-]*(\d+)\.(?:txt|md)", file.name, re.IGNORECASE
            )
            if match:
                grouped.setdefault(file.parent, []).append(
                    {"chunk": int(match[1]), "text": document["text"].strip()}
                )
        for directory, chunks in grouped.items():
            global_document = next(
                (
                    d
                    for d in documents
                    if PurePosixPath(d["name"]).parent == directory
                    and re.fullmatch(
                        r"global(?:[_ -](?:prompt|prefix))?\.(?:txt|md)",
                        PurePosixPath(d["name"]).name,
                        re.IGNORECASE,
                    )
                ),
                None,
            )
            combined = {"chunks": sorted(chunks, key=lambda c: c["chunk"])}
            if global_document:
                combined["global"] = global_document["text"].strip()
            documents.append(
                {
                    "name": str(directory / "(combined chunk text files).json"),
                    "text": json.dumps(combined, ensure_ascii=False),
                }
            )

        # Prefer the full manifest over its per-chunk text copies and audit notes.
        def rank(d):
            try:
                v = json.loads(d["text"])
            except ValueError:
                v = None
            return (
                0
                if isinstance(v, dict) and "segments" in v
                else 1
                if isinstance(v, dict) and "chunks" in v
                else 2
                if isinstance(v, list)
                else 3
                if any(
                    re.match(r"^\s*(?:\[|#{1,6}\s*Chunk|Chunk\s+\d)", l, re.IGNORECASE)
                    for l in d["text"].splitlines()
                )
                else 4
                if PurePosixPath(d["name"]).name.lower().startswith("global")
                else 5,
                natural(d["name"]),
            )

        documents.sort(key=rank)
        entries.sort(key=lambda e: natural(e["name"]))
        return {"name": name, "kind": kind, "documents": documents, "media": entries}

    def register_source(self, path, name, *, cancel=lambda: None):
        detected = self.detect(path, name, cancel=cancel)
        token = uuid.uuid4().hex
        target = self.root / token
        Path(path).replace(target)
        self.store.atomic_json(self.root / (token + ".json"), detected)
        return self.describe(token, detected)

    def remove(self, token):
        path = self.stage_path(token)
        path.unlink(missing_ok=True)
        path.with_name(token + ".json").unlink(missing_ok=True)

    def describe(self, token, source):
        return {
            "token": token,
            "name": source["name"],
            "kind": source["kind"],
            "documents": [{"name": d["name"]} for d in source["documents"]],
            "media": source["media"],
        }

    def preview(
        self,
        current,
        *,
        token=None,
        text=None,
        document_index=0,
        selected=None,
        target="merge",
        provenance=None,
        soundtrack_mode="replace",
        time_origin="auto",
        cancel=lambda: None,
    ):
        source = None
        path = None
        prompt_error = None
        if selected is not None and not isinstance(selected, dict):
            raise ValueError("Import component selection must be an object")

        def parse(value):
            nonlocal prompt_error
            try:
                return parse_prompt_source(value)
            except ValueError as exc:
                if selected is None or selected.get("global") or selected.get("chunks"):
                    raise
                prompt_error = str(exc)
                return {
                    "global": "",
                    "global_present": False,
                    "entries": [],
                    "format": "invalid-prompt-document",
                }

        if token:
            path = self.stage_path(token)
            path.touch()
            source = json.loads(
                (self.root / (token + ".json")).read_text(encoding="utf-8")
            )
            documents = source["documents"]
            if documents:
                if type(document_index) is not int or not 0 <= document_index < len(
                    documents
                ):
                    raise ValueError("Choose an available prompt document")
                doc = parse(documents[document_index]["text"])
                base = PurePosixPath(documents[document_index]["name"]).parent
            else:
                doc = {
                    "global": "",
                    "global_present": False,
                    "entries": [],
                    "format": "media-only",
                }
                base = PurePosixPath(".")
        else:
            if (
                not isinstance(text, str)
                or len(text.encode("utf-8")) > MAX_MANIFEST_BYTES
            ):
                raise ValueError("Pasted prompt text must be at most 4 MiB")
            doc = parse(text)
            base = PurePosixPath(".")
        inventory = source["media"] if source else []
        referenced = {
            str(base / member_path(e["filename"]))
            for e in doc["entries"]
            if e.get("filename")
        }
        chosen_inventory = [
            m
            for m in inventory
            if m["component"] != "video" or not referenced or m["name"] in referenced
        ]
        available = {
            "global": doc["global_present"] or bool(prompt_error),
            "chunks": bool(doc["entries"]) or bool(prompt_error),
            "video": any(m["component"] == "video" for m in chosen_inventory),
            "soundtrack": any(
                m["component"] in {"soundtrack", "video"} for m in chosen_inventory
            ),
        }
        selected = {
            k: bool(v)
            for k, v in (selected if selected is not None else available).items()
            if k in available
        }
        selected = {k: selected.get(k, False) and available[k] for k in available}
        media = []
        notes = []
        if prompt_error:
            notes.append("Prompt document was not imported: " + prompt_error)
        registered = {}
        members = {m["name"]: m for m in inventory}
        timing = {}
        for e in doc["entries"]:
            if e.get("filename"):
                member = str(base / member_path(e["filename"]))
                if member not in members:
                    notes.append(
                        f"Missing media: {member}; its prompt is still available to import."
                    )
                elif "start_seconds" in e:
                    timing[member] = (
                        round(rational(e["start_seconds"]) * 24),
                        round(rational(e["end_seconds"]) * 24),
                    )
        # A manifest can select one original soundtrack explicitly; other audio
        # files remain detected and visible but are not silently mixed into it.
        audio_name = doc.get("audio_filename")
        if audio_name is not None:
            audio_name = str(base / member_path(audio_name))
            if audio_name not in members:
                notes.append(
                    f"Missing soundtrack: {audio_name}; prompt import remains available."
                )
        if selected["video"] or selected["soundtrack"]:
            if path is None:
                raise ValueError("This text source contains no uploaded media")
            archive = zipfile.ZipFile(path) if source["kind"] == "zip" else None
            try:
                cursor = 0
                for item in chosen_inventory:
                    component = item["component"]
                    # Embedded soundtrack needs the video asset even when
                    # video placement is disabled.
                    needed = (
                        selected["video"]
                        if component == "video"
                        else selected["soundtrack"]
                    )
                    if (
                        component == "video"
                        and selected["soundtrack"]
                        and not any(m["component"] == "soundtrack" for m in inventory)
                    ):
                        needed = True
                    if not needed or (
                        component == "soundtrack"
                        and audio_name
                        and item["name"] != audio_name
                    ):
                        continue
                    cancel()
                    temporary = self.store.assets.upload_path()
                    checksum = hashlib.sha256()
                    try:
                        with (
                            (
                                archive.open(item["name"])
                                if archive
                                else path.open("rb")
                            ) as input_file,
                            temporary.open("xb") as output,
                        ):
                            while block := input_file.read(1024**2):
                                cancel()
                                output.write(block)
                                checksum.update(block)
                        asset = self.store.assets.adopt(
                            temporary,
                            checksum.hexdigest(),
                            PurePosixPath(item["name"]).name,
                            cancel=cancel,
                        )
                        asset["storage"] = "managed"
                        if PurePosixPath(item["name"]).suffix.lower() in {
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".webp",
                            ".bmp",
                        }:
                            asset["kind"] = "image"
                        registered[item["name"]] = asset
                    finally:
                        temporary.unlink(missing_ok=True)
                    if component == "video" and not asset.get("video"):
                        raise ValueError(
                            f"Picture file has no video/image stream: {item['name']}"
                        )
                    if component == "soundtrack" and not asset.get("audio"):
                        raise ValueError(
                            f"Soundtrack file has no audio stream: {item['name']}"
                        )
                    duration = (
                        current["chunk_frames"]
                        if asset.get("kind") == "image"
                        else max(1, round(rational(asset["duration"]) * 24))
                    )
                    a, b = (
                        timing.get(item["name"], (cursor, cursor + duration))
                        if component == "video"
                        else (0, duration)
                    )
                    stream = (
                        asset.get("video")
                        if component == "video"
                        else asset.get("audio")
                    )
                    media.append(
                        {
                            "component": component,
                            "asset": asset,
                            "start": a,
                            "end": b,
                            "source_in": str(rational(stream.get("start_time") or "0")),
                        }
                    )
                    if component == "video":
                        cursor = b
                if (
                    selected["soundtrack"]
                    and not any(m["component"] == "soundtrack" for m in media)
                    and not selected["video"]
                ):
                    media = [
                        {
                            **m,
                            "component": "soundtrack",
                            "source_in": str(
                                rational(m["asset"]["audio"].get("start_time") or "0")
                            ),
                        }
                        for m in media
                        if m["asset"].get("audio")
                    ]
            except (
                subprocess.CalledProcessError,
                zipfile.BadZipFile,
                RuntimeError,
            ) as exc:
                raise ValueError(f"Cannot read selected import media: {exc}") from exc
            finally:
                if archive:
                    archive.close()
        candidate, mapping = apply_components(
            current,
            doc,
            media,
            selected,
            target,
            provenance,
            soundtrack_mode,
            time_origin,
        )
        notes += mapping["notes"]
        if selected["soundtrack"] and not any(m["asset"].get("audio") for m in media):
            notes.append(
                "No selected file has an audio stream; existing soundtrack is retained."
            )
        candidate["import_notes"] = notes
        return {
            "project": candidate,
            "mapping": mapping,
            "global": doc["global"],
            "available": available,
            "selected": selected,
            "format": doc["format"],
            "media": [
                {
                    "name": m["asset"]["name"],
                    "component": m["component"],
                    "range": [m["start"], m["end"]],
                }
                for m in media
            ],
            "detected_media": inventory,
            "notes": notes,
            "target": target,
            "prompt_error": prompt_error,
            "source_ownership": "Studio prompts are used; the connected sequence_prompt is ignored until explicitly re-linked."
            if selected["global"] or selected["chunks"]
            else "Existing prompt source is unchanged.",
        }

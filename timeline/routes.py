"""Same-origin Timeline Studio API. No caller-controlled filesystem paths."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import os
import re
import subprocess
import threading
import time
import uuid

from aiohttp import web

from .media import MEDIA_FORMATS, preview_audio_window, run_process, video_frames
from .protocol import NATIVE_PATH_CONTRACT, native_runtime_manifest
from .model import (
    compile_project, selected_export_range, validate,
    verify_export_clip_selection,
)
from .nodes import project_store, stale_take, take_path, takes

_PREFIX = "/h3_continuum/timeline"


class MediaRequestCancelled(InterruptedError):
    """An obsolete UI request whose socket disconnected during decoding."""

_tasks = {}
_media_slots = asyncio.Semaphore(2)
# Low-latency playback must not queue behind thumbnail/waveform jobs.
_preview_slots = asyncio.Semaphore(1)
# Full viewer stills must not sit behind a backlog of 240px filmstrips.
_still_slots = asyncio.Semaphore(1)
# Parallel filmstrips can request the same source frame from several tracks.
# Coalesce each cold thumbnail, then serve its immutable derived JPEG from disk.
_thumbnail_locks = {}
# Registered media is content addressed, so derived views never change.
_IMMUTABLE = {"Cache-Control": "private, max-age=31536000, immutable"}


def _prune(root, pattern, limit_bytes, keep):
    """Keep a derived-media directory under ``limit_bytes``, oldest first."""
    import re

    entries = []
    for path in root.glob(pattern):
        if re.search(r"-[0-9a-f]{32}$", path.stem):
            continue  # An in-progress render owned by another request.
        try:
            st = path.stat()
        except OSError:
            continue
        entries.append((st.st_mtime_ns, st.st_size, path))
    total = sum(size for _, size, _ in entries)
    for _, size, path in sorted(entries):
        if total <= limit_bytes:
            break
        if path == keep:
            continue
        try:
            path.unlink()
            total -= size
        except OSError:
            continue


def _request_cancel():
    """Route media work never polls ComfyUI's execution interrupt flag.

    ``throw_exception_if_processing_interrupted`` clears the flag it reads, so
    an HTTP thread would otherwise swallow a sampling cancellation.
    """


async def _media_cpu(fn, *args, **kwargs):
    async with _media_slots:
        task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise


async def _media_cpu_cancellable(request, fn, *args, slots=None, **kwargs):
    """Bounded UI media work must stop after a seek, scroll or page close."""
    stopped = threading.Event()

    def cancel():
        if stopped.is_set():
            raise MediaRequestCancelled("Media request cancelled")

    async with (slots if slots is not None else _media_slots):
        task = asyncio.create_task(
            asyncio.to_thread(fn, *args, cancel=cancel, **kwargs)
        )

        async def watch_disconnect():
            while not task.done():
                transport = request.transport
                if transport is None or transport.is_closing():
                    stopped.set()
                    return
                await asyncio.sleep(0.15)

        watcher = asyncio.create_task(watch_disconnect())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stopped.set()
            try:
                await task
            except InterruptedError:
                pass
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


def register_routes():
    try:
        from server import PromptServer
    except ModuleNotFoundError as exc:
        if exc.name == "server":
            return  # Standalone compiler/media tooling has no ComfyUI server.
        raise
    routes = PromptServer.instance.routes

    def endpoint(method, path):
        def decorate(fn):
            async def safe(request):
                try:
                    return await fn(request)
                except MediaRequestCancelled:
                    # Expected during rapid scrolling, seeking or closing.
                    # Do not log an aiohttp server error for abandoned work.
                    return web.Response(status=499)
                except (ValueError, KeyError, IndexError, FileNotFoundError) as exc:
                    return web.json_response({"error": str(exc)}, status=400)

            getattr(routes, method)(_PREFIX + path)(safe)
            return fn

        return decorate

    @endpoint("get", "/runtime")
    async def runtime_compatibility(request):
        """Expose the actually loaded server/frontend code, not GitHub PR metadata.

        This guards against an older ComfyUI Patcher overlay, a second installed
        copy of the extension, or an Electron/browser session using stale code.
        No file search, media access, or arbitrary filesystem path is involved.
        """
        import nodes

        expected = Path(__file__).resolve().parent.parent / "web"
        try:
            manifest = native_runtime_manifest(
                nodes.EXTENSION_WEB_DIRS, expected
            )
        except ValueError as exc:
            return web.json_response(
                {"error": str(exc), "contract": NATIVE_PATH_CONTRACT},
                status=503, headers={"Cache-Control": "no-store"},
            )
        return web.json_response(
            manifest, headers={"Cache-Control": "no-store"},
        )

    @endpoint("post", "/compile")
    async def compile_route(request):
        data = await request.json()
        return web.json_response(await asyncio.to_thread(compile_project, data))

    @endpoint("post", "/project")
    async def save(request):
        data = await request.json()
        store = project_store()
        saved = await asyncio.to_thread(
            store.save, data["project"], data["expected_revision"]
        )
        return web.json_response(saved)

    @endpoint("get", "/project/{project_id}")
    async def load(request):
        return web.json_response(
            await asyncio.to_thread(
                project_store().read, request.match_info["project_id"]
            )
        )

    @endpoint("post", "/link")
    async def link_server_media(request):
        # No browser-supplied arbitrary read: AssetStore checks each real path
        # against the server operator's approved roots before accessing bytes.
        data = await request.json()
        source = data.get("path")
        if not isinstance(source, str) or not 0 < len(source) <= 4096:
            raise ValueError("A server-local media path is required")
        store = project_store()
        stop = threading.Event()

        def cancel_link():
            if stop.is_set():
                raise InterruptedError("Native media link cancelled")

        task = asyncio.create_task(
            asyncio.to_thread(store.assets.link, source, cancel=cancel_link)
        )

        async def watch_disconnect():
            while not task.done():
                transport = request.transport
                if transport is None or transport.is_closing():
                    stop.set()
                    return
                await asyncio.sleep(0.25)

        watcher = asyncio.create_task(watch_disconnect())
        try:
            asset = await asyncio.shield(task)
        except asyncio.CancelledError:
            stop.set()
            try:
                await task
            except InterruptedError:
                pass
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if asset.get("video") and (
            asset["video"].get("nb_frames") == "1"
            or asset["name"].lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".bmp")
            )
        ):
            asset["kind"] = "image"
        return web.json_response(asset)

    @endpoint("post", "/import")
    async def ingest(request):
        # Stream straight into the store: one copy on disk, hashed as it
        # arrives, never buffered whole in RAM or on a separate temp volume.
        store = project_store()
        temporary = store.assets.upload_path()
        digest = hashlib.sha256()
        try:
            with temporary.open("xb") as file:
                async for chunk in request.content.iter_chunked(1024 * 1024):
                    digest.update(chunk)
                    file.write(chunk)
                file.flush()
                os.fsync(file.fileno())
            try:
                asset = await _media_cpu(
                    store.assets.adopt,
                    temporary,
                    digest.hexdigest(),
                    request.query.get("name", "Media"),
                    cancel=_request_cancel,
                )
            except subprocess.CalledProcessError as exc:
                raise ValueError("Unsupported or unreadable media file") from exc
        finally:
            temporary.unlink(missing_ok=True)
        asset["storage"] = "managed"
        if asset.get("video") and (
            asset["video"].get("nb_frames") == "1"
            or asset["name"]
            .lower()
            .endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
        ):
            asset["kind"] = "image"
        return web.json_response(asset)

    @endpoint("post", "/import-pack")
    async def ingest_pack(request):
        """Explicit ZIP upload; ordinary movie imports retain native linking."""
        from .bundle import MAX_ARCHIVE_BYTES, import_bundle

        store = project_store()
        temporary = store.assets.upload_path()
        size = 0
        try:
            with temporary.open("xb") as file:
                async for chunk in request.content.iter_chunked(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise ValueError("Clip pack exceeds the 16 GiB upload limit")
                    file.write(chunk)
            scene = await _media_cpu_cancellable(
                request, import_bundle, temporary, store.assets, {},
            )
            return web.json_response(scene)
        finally:
            temporary.unlink(missing_ok=True)

    @endpoint("get", "/media/{asset_id}")
    async def media(request):
        file = await asyncio.to_thread(
            project_store().assets.verify, request.match_info["asset_id"],
            cancel=_request_cancel,
        )
        response = web.FileResponse(file)
        response.content_type = (
            request.query.get("type", "video/mp4")
            if request.query.get("type")
            in ("video/mp4", "video/webm", "audio/wav", "image/png", "image/jpeg")
            else "application/octet-stream"
        )
        return response

    @endpoint("get", "/proxy/{asset_id}")
    async def proxy(request):
        store = project_store()
        asset_id = request.match_info["asset_id"]
        file = await asyncio.to_thread(
            store.assets.verify, asset_id, cancel=_request_cancel
        )
        root = store.root / "proxies"
        root.mkdir(exist_ok=True)
        # A bounded *window* proxy. Never transcode an entire two-hour import.
        start = max(0, float(request.query.get("start", "0")))
        duration = min(30, max(0.1, float(request.query.get("duration", "10"))))
        key = f"{asset_id}-{start:.3f}-{duration:.3f}"
        output = root / (key + ".mp4")
        if not output.exists():
            temporary = root / (key + "-" + uuid.uuid4().hex + ".mp4")
            try:
                await _media_cpu_cancellable(
                    request, run_process,
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-nostdin",
                        "-threads",
                        "1",
                        "-ss",
                        str(start),
                        "-protocol_whitelist",
                        "file,pipe",
                        "-format_whitelist",
                        MEDIA_FORMATS,
                        "-i",
                        str(file),
                        "-t",
                        str(duration),
                        "-vf",
                        "scale=640:-2",
                        "-c:v",
                        "libx264",
                        "-threads:v",
                        "1",
                        "-preset",
                        "ultrafast",
                        "-crf",
                        "24",
                        "-c:a",
                        "aac",
                        "-movflags",
                        "+faststart",
                        str(temporary),
                    ],
                )
                temporary.replace(output)
            finally:
                temporary.unlink(missing_ok=True)
            _prune(root, "*.mp4", 4 * 1024**3, output)
        return web.FileResponse(output, headers={"Content-Type": "video/mp4"})

    @endpoint("get", "/thumbnail/{asset_id}")
    async def thumbnail(request):
        from .model import rational
        from PIL import Image
        import io

        store = project_store()
        asset_id = request.match_info["asset_id"]
        # Validate even on a cache hit: linked files remain verified by stat
        # and are never served after modification under the original identity.
        path = await asyncio.to_thread(
            store.assets.verify, asset_id, cancel=_request_cancel,
        )
        stamp = str(rational(request.query.get("pts", "0")))
        width = int(request.query.get("w", "240"))
        height = int(request.query.get("h", "136"))
        if not (16 <= width <= 2048 and 16 <= height <= 1152
                and width * height <= 2048 * 1152):
            raise ValueError("Invalid still-image size")
        key = asset_id + "-" + hashlib.sha256(
            f"{stamp}:{width}:{height}".encode("ascii")
        ).hexdigest()
        root = store.root / "thumbnails"
        root.mkdir(exist_ok=True)
        output = root / (key + ".jpg")
        if output.is_file():
            return web.FileResponse(output, headers={**_IMMUTABLE, "Content-Type": "image/jpeg"})
        lock = _thumbnail_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                if not output.is_file():
                    high_detail = width > 240 or height > 136
                    pixels = await _media_cpu_cancellable(
                        request, video_frames,
                        path, [stamp], width, height, padding="hold",
                        slots=_still_slots if high_detail else _media_slots,
                    )
                    # Serialize a tiny derived JPEG, not the source movie.
                    data = io.BytesIO()
                    Image.fromarray(pixels[0]).save(
                        data, format="JPEG",
                        **({"quality": 90, "subsampling": 0} if high_detail else {}),
                    )
                    temporary = output.with_name(
                        output.stem + "-" + uuid.uuid4().hex + ".jpg"
                    )
                    try:
                        temporary.write_bytes(data.getvalue())
                        temporary.replace(output)
                    finally:
                        temporary.unlink(missing_ok=True)
                    _prune(root, "*.jpg", 512 * 1024**2, output)
        finally:
            # New callers can create a new lock once the file exists. Existing
            # waiters retain their lock object until they complete.
            if _thumbnail_locks.get(key) is lock:
                _thumbnail_locks.pop(key, None)
        return web.FileResponse(output, headers={**_IMMUTABLE, "Content-Type": "image/jpeg"})

    @endpoint("get", "/waveform/{asset_id}")
    async def waveform(request):
        import numpy as np

        assets = project_store().assets
        path = await asyncio.to_thread(
            assets.verify, request.match_info["asset_id"], cancel=_request_cancel
        )
        start = max(0, float(request.query.get("start", "0")))
        end = min(start + 60, float(request.query.get("end", str(start + 30))))
        # Timeline waveforms are visual approximations, not generation audio.
        # Random PTS seeking avoids decoding an hour-long prefix for every tile.
        pcm = await _media_cpu_cancellable(
            request, preview_audio_window,
            path, str(start), str(end), 1000,
        )
        peaks = np.max(np.abs(pcm), axis=0)
        return web.json_response(
            {
                "start": start,
                "end": end,
                "peaks": [
                    float(np.max(x)) if x.size else 0
                    for x in np.array_split(peaks, 512)
                ],
            },
            headers=_IMMUTABLE,
        )

    @endpoint("get", "/takes/{project_id}")
    async def list_takes(request):
        store = project_store()
        pid = request.match_info["project_id"]
        records = takes(store, pid)
        try:
            p = store.read(pid)
        except FileNotFoundError:
            p = None
        result = []
        for r in records:
            render = store.project_dir(pid) / ("render-" + r["id"] + ".json")
            result.append(
                {
                    **{k: v for k, v in r.items() if k != "project_snapshot"},
                    "stale": stale_take(p, r, records) if p else None,
                    "rendition": json.loads(render.read_text(encoding="utf-8"))
                    if render.exists()
                    else None,
                }
            )
        return web.json_response(result)

    @endpoint("post", "/take/select")
    async def select_take(request):
        data = await request.json()
        store = project_store()
        p = store.read(data["project_id"])
        record = json.loads(
            take_path(store, p["project_id"], data["take_id"]).read_text(
                encoding="utf-8"
            )
        )
        all_records = takes(store, p["project_id"])
        if data["action"] == "reject":
            p.setdefault("rejected", []).append(record["id"])
            accepted = p.setdefault("accepted", {})
            invalid = []
            if accepted.get(str(record["indices"][0])) == record["id"]:
                for key in list(accepted):
                    if int(key) >= record["indices"][0]:
                        invalid.append(accepted.pop(key))
                for track in p["tracks"]:
                    if track["role"] == "generated":
                        track["clips"] = [
                            c for c in track["clips"] if c.get("take_id") not in invalid
                        ]
        elif data["action"] == "accept":
            if stale_take(p, record, all_records):
                raise ValueError(
                    "Take inputs or causal parent changed; render a new take"
                )
            render = json.loads(
                (
                    store.project_dir(p["project_id"])
                    / ("render-" + record["id"] + ".json")
                ).read_text(encoding="utf-8")
            )
            asset = render["asset"]
            # A graph-connected dimension is resolved at execution, after the
            # project snapshot was frozen. Adopt it only while the canvas still
            # matches that snapshot; a later explicit canvas edit takes precedence.
            if record.get("source_canvas") == [p["width"], p["height"]]:
                p["width"], p["height"] = record["generation_size"]
            if asset["id"] not in {a["id"] for a in p["assets"]}:
                p["assets"].append(asset)
            selected = p.setdefault("accepted", {})
            # Selecting a different upstream take invalidates selected suffixes.
            old = selected.get(str(record["indices"][0]))
            invalidated = []
            if old != record["id"]:
                for k in list(selected):
                    if int(k) > record["indices"][0]:
                        invalidated.append(selected.pop(k))
            selected[str(record["indices"][0])] = record["id"]
            p["rejected"] = [r for r in p.get("rejected", []) if r != record["id"]]
            lane = next((t for t in p["tracks"] if t["id"] == "accepted-takes"), None)
            if lane is None:
                lane = {
                    "id": "accepted-takes",
                    "role": "generated",
                    "name": "Accepted takes",
                    "clips": [],
                }
                p["tracks"].append(lane)
            lane["clips"] = [
                c for c in lane["clips"] if c.get("take_id") not in invalidated
            ]
            a, b = record.get("output_range", record["range"])
            sa, sb = record["selection"]
            a, b = max(a, sa), min(b, sb)
            # Remove overlapping old generated clips, retaining their unedited sides.
            remaining = []
            for c in lane["clips"]:
                if c["end"] <= a or c["start"] >= b:
                    remaining.append(c)
                    continue
                if c["start"] < a:
                    remaining.append({**c, "id": uuid.uuid4().hex, "end": a})
                if c["end"] > b:
                    from .model import source_time

                    remaining.append(
                        {
                            **c,
                            "id": uuid.uuid4().hex,
                            "start": b,
                            "source_in": str(source_time(c, b)),
                        }
                    )
            remaining.append(
                {
                    "id": record["id"],
                    "take_id": record["id"],
                    "asset_id": asset["id"],
                    "start": a,
                    "end": b,
                    "source_in": str(
                        (a - record.get("output_range", record["range"])[0]) / 24
                    ),
                    "speed": "1",
                    "padding": "error",
                    # Generated audio plays only where no authored final audio
                    # covers the accepted span; previously accepted takes do
                    # not count, they are replaced on this lane.
                    "audio_role": "final",
                    "audio_fallback": True,
                }
            )
            lane["clips"] = remaining
        else:
            raise ValueError("Unknown take action")
        accepted_ids = set(p.get("accepted", {}).values())
        p["accepted_windows"] = [
            {
                "take_id": r["id"],
                "first_chunk": r["indices"][0],
                "stop_chunk": r["indices"][-1] + 1,
                "parent_take": r.get("parent_take"),
            }
            for r in all_records
            if r["id"] in accepted_ids
        ]
        return web.json_response(store.save(p, data["expected_revision"]))

    @endpoint("post", "/preview")
    async def composition_preview(request):
        """Generate a tiny seek-based playback segment; never a 30s export."""
        from .model import digest
        from .preview import MAX_SEGMENT_FRAMES, render_preview_segment, simple_source

        started_at = time.perf_counter()
        data = await request.json()
        p = validate(data["project"])
        start = max(0, min(p["duration_frames"] - 1, int(data.get("start", 0))))
        length = int(data.get("length_frames", 48))
        if not 1 <= length <= MAX_SEGMENT_FRAMES:
            raise ValueError("Playback segment must be 1 to 192 frames")
        stop = min(p["duration_frames"], start + length)
        mode = data.get("mode", "composed")
        if mode not in {"composed", "source", "generated"}:
            raise ValueError("Unknown playback mode")
        if mode == "source":
            p["tracks"] = [t for t in p["tracks"] if t["role"] != "generated"]
        elif mode == "generated":
            p["tracks"] = [t for t in p["tracks"] if t["role"] != "retained"]
        # Preview quality follows the timeline canvas, never a hidden 480px
        # longest-edge cap. Speed comes from bounded temporal work/seek,
        # not from quietly degrading the movie to 160p or 270p.
        strategy = "direct-source" if simple_source(p, start, stop) else "composed"
        key = digest([
            {k: v for k, v in p.items() if k != "ui"},
            start, stop, "low-latency-play-v1",
        ])
        store = project_store()
        root = store.root / "composition_previews"
        root.mkdir(exist_ok=True)
        output = root / (key + ".mp4")
        cache_hit = output.is_file()
        if not cache_hit:
            stop_requested = threading.Event()

            def cancel():
                if stop_requested.is_set():
                    raise MediaRequestCancelled("Playback preview cancelled")

            async with _preview_slots:
                # Another request can have published the same segment while
                # this one was queued for the short preview worker.
                if not output.is_file():
                    task = asyncio.create_task(asyncio.to_thread(
                        render_preview_segment, p, store, output, start, stop,
                        cancel=cancel,
                    ))

                    async def watch_disconnection():
                        while not task.done():
                            transport = request.transport
                            if transport is None or transport.is_closing():
                                stop_requested.set()
                                return
                            await asyncio.sleep(0.15)

                    watcher = asyncio.create_task(watch_disconnection())
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        stop_requested.set()
                        try:
                            await task
                        except InterruptedError:
                            pass
                        raise
                    finally:
                        watcher.cancel()
                        await asyncio.gather(watcher, return_exceptions=True)
            _prune(root, "*.mp4", 512 * 1024**2, output)
        return web.json_response(
            {"url": _PREFIX + "/composition/" + key,
             "start": start, "end": stop,
             "cache_hit": cache_hit,
             "strategy": strategy,
             "prepare_ms": round((time.perf_counter() - started_at) * 1000)}
        )

    @endpoint("get", "/composition/{asset_id}")
    async def read_composition(request):
        import re

        key = request.match_info["asset_id"]
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("Invalid composition preview identifier")
        path = project_store().root / "composition_previews" / (key + ".mp4")
        if not path.is_file():
            raise ValueError("Composition preview expired; play the timeline again")
        return web.FileResponse(path, headers={"Content-Type": "video/mp4"})

    @endpoint("post", "/export")
    async def export(request):
        from .compose import export_project, export_dimensions
        import folder_paths

        data = await request.json()
        p = validate(data["project"])
        codec = data.get("codec", "lossless")
        if codec not in ("lossless", "h264"):
            raise ValueError("Unknown export codec")
        start, stop = selected_export_range(p, data.get("render_range"))
        clip_ids = data.get("selected_clip_ids", [])
        verify_export_clip_selection(p, clip_ids, start, stop)
        resolution = data.get("resolution", "timeline")
        # A multi-hour source may need first-use SHA verification. Keep this
        # work off aiohttp's event loop and out of ComfyUI sampling cancellation.
        width, height = await asyncio.to_thread(
            export_dimensions, p, project_store(), start, stop,
            resolution, clip_ids, cancel=lambda: None,
        )
        if codec == "h264" and (width % 2 or height % 2):
            raise ValueError("H.264 requires even dimensions; use FFV1 or Timeline canvas")
        frames = stop - start
        active = sum(t["status"] == "running" for t in _tasks.values())
        if active >= 2:
            raise ValueError("Two exports are already running")
        for finished in [k for k, v in _tasks.items() if v["status"] != "running"][
            :-16
        ]:
            _tasks.pop(finished, None)
        job = uuid.uuid4().hex
        event = threading.Event()
        output = Path(folder_paths.get_output_directory()) / (
            "Timeline_" + job + (".mkv" if codec == "lossless" else ".mp4")
        )
        started = time.monotonic()
        item = {
            "status": "running", "stage": "queued",
            "cancel": event, "filename": output.name,
            "frames_done": 0, "total_frames": frames,
            "render_range": [start, stop],
            "resolution": resolution, "width": width, "height": height,
            "elapsed_seconds": 0, "started_at": started,
        }
        _tasks[job] = item

        def cancel():
            if event.is_set():
                raise InterruptedError("Export cancelled")

        def progress(**updates):
            item.update(updates)
            item["elapsed_seconds"] = round(time.monotonic() - started, 1)

        async def work():
            try:
                await asyncio.to_thread(
                    export_project, p, project_store(), output,
                    codec=codec, cancel=cancel, on_progress=progress,
                    render_range=(start, stop),
                    resolution=resolution, selected_clip_ids=clip_ids,
                )
                progress(stage="complete", frames_done=frames)
                item["status"] = "complete"
            except Exception as exc:
                item.update(
                    status="cancelled" if event.is_set() else "failed",
                    error=str(exc),
                    stage="cancelled" if event.is_set() else "failed",
                    elapsed_seconds=round(time.monotonic() - started, 1),
                )

        item["task"] = asyncio.create_task(work())
        return web.json_response({
            "id": job, "status": "running",
            "render_range": [start, stop], "total_frames": frames,
            "resolution": resolution, "width": width, "height": height,
        })

    @endpoint("get", "/export/{job_id}")
    async def status(request):
        job = request.match_info["job_id"]
        if not re.fullmatch(r"[0-9a-f]{32}", job) or job not in _tasks:
            raise web.HTTPNotFound(text="Unknown export job")
        item = _tasks[job]
        if item["status"] == "running":
            item["elapsed_seconds"] = round(
                time.monotonic() - item.get("started_at", time.monotonic()), 1
            )
        return web.json_response(
            {k: v for k, v in item.items() if k not in ("cancel", "task", "started_at")}
        )

    @endpoint("get", "/export/{job_id}/file")
    async def download_export(request):
        job = request.match_info["job_id"]
        if not re.fullmatch(r"[0-9a-f]{32}", job):
            raise web.HTTPNotFound()
        item = _tasks.get(job)
        if not item or item["status"] != "complete":
            raise web.HTTPNotFound(text="Export is not complete")
        import folder_paths

        filename = "Timeline_" + job + (
            ".mkv" if item["filename"].endswith(".mkv") else ".mp4"
        )
        file = Path(folder_paths.get_output_directory()) / filename
        if not file.is_file():
            raise web.HTTPNotFound(text="Completed export file is missing")
        return web.FileResponse(
            file, headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "private, no-store",
            },
        )

    @endpoint("post", "/export/{job_id}/cancel")
    async def cancel_export(request):
        _tasks[request.match_info["job_id"]]["cancel"].set()
        return web.json_response({"cancelled": True})

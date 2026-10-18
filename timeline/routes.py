"""Same-origin Timeline Studio API. No caller-controlled filesystem paths."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import os
import subprocess
import threading
import time
import uuid

from aiohttp import web

from .media import MEDIA_FORMATS, audio_window, run_process, video_frames
from .protocol import NATIVE_PATH_CONTRACT, native_runtime_manifest
from .model import compile_project, validate
from .nodes import project_store, stale_take, take_path, takes

_PREFIX = "/h3_continuum/timeline"
_tasks = {}
_media_slots = asyncio.Semaphore(2)
# Low-latency playback must not queue behind thumbnail/waveform jobs.
_preview_slots = asyncio.Semaphore(1)
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
                await _media_cpu(
                    run_process,
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
                    cancel=_request_cancel,
                )
                temporary.replace(output)
            finally:
                temporary.unlink(missing_ok=True)
            _prune(root, "*.mp4", 4 * 1024**3, output)
        return web.FileResponse(output, headers={"Content-Type": "video/mp4"})

    @endpoint("get", "/thumbnail/{asset_id}")
    async def thumbnail(request):
        from PIL import Image
        import io

        path = await asyncio.to_thread(
            project_store().assets.verify, request.match_info["asset_id"],
            cancel=_request_cancel,
        )
        stamp = request.query.get("pts", "0")
        pixels = await _media_cpu(
            video_frames,
            path,
            [stamp],
            240,
            136,
            padding="hold",
            cancel=_request_cancel,
        )
        data = io.BytesIO()
        Image.fromarray(pixels[0]).save(data, format="JPEG")
        return web.Response(
            body=data.getvalue(), content_type="image/jpeg", headers=_IMMUTABLE
        )

    @endpoint("get", "/waveform/{asset_id}")
    async def waveform(request):
        import numpy as np

        assets = project_store().assets
        path = await asyncio.to_thread(
            assets.verify, request.match_info["asset_id"], cancel=_request_cancel
        )
        start = max(0, float(request.query.get("start", "0")))
        end = min(start + 60, float(request.query.get("end", str(start + 30))))
        pcm = await _media_cpu(
            audio_window,
            path,
            str(start),
            str(end),
            1000,
            cancel=_request_cancel,
            cache=assets.pcm_cache,
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
        from .preview import MAX_SEGMENT_FRAMES, render_preview_segment

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
        scale = min(1, 480 / max(p["width"], p["height"]))
        p["width"] = max(32, round(p["width"] * scale / 32) * 32)
        p["height"] = max(32, round(p["height"] * scale / 32) * 32)
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
                    raise InterruptedError("Playback preview cancelled")

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
        from .compose import export_project
        import folder_paths

        data = await request.json()
        p = validate(data["project"])
        codec = data.get("codec", "lossless")
        if codec not in ("lossless", "h264"):
            raise ValueError("Unknown export codec")
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
        item = {"status": "running", "cancel": event, "filename": output.name}
        _tasks[job] = item

        def cancel():
            if event.is_set():
                raise ValueError("Export cancelled")

        async def work():
            try:
                await asyncio.to_thread(
                    export_project,
                    p,
                    project_store(),
                    output,
                    codec=codec,
                    cancel=cancel,
                )
                item["status"] = "complete"
            except Exception as exc:
                item.update(status="failed", error=str(exc))

        item["task"] = asyncio.create_task(work())
        return web.json_response({"id": job, "status": "running"})

    @endpoint("get", "/export/{job_id}")
    async def status(request):
        job = request.match_info["job_id"]
        item = _tasks[job]
        return web.json_response(
            {k: v for k, v in item.items() if k not in ("cancel", "task")}
        )

    @endpoint("post", "/export/{job_id}/cancel")
    async def cancel_export(request):
        _tasks[request.match_info["job_id"]]["cancel"].set()
        return web.json_response({"cancelled": True})

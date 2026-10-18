"""Launch real ComfyUI on CPU and exercise Timeline Studio's HTTP/node contracts.

Usage: python tools/check_timeline_comfy.py --comfy-root /path/to/ComfyUI
Optional browser test: --browser-test tests/timeline_browser.cjs
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--comfy-root", type=Path, required=True)
    parser.add_argument("--browser-test", type=Path)
    parser.add_argument(
        "--front-end-root",
        type=Path,
        help="Serve this frontend build instead of the installed package",
    )
    args = parser.parse_args()
    args.comfy_root = args.comfy_root.resolve()
    base = "http://127.0.0.1:8199"
    # Honor local direct access even when the host's outbound proxy is enabled.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, data=None, raw=None, headers=None):
        body = (
            raw
            if raw is not None
            else json.dumps(data).encode()
            if data is not None
            else None
        )
        req = urllib.request.Request(
            base + path,
            data=body,
            headers=headers
            or ({"Content-Type": "application/json"} if body is not None else {}),
        )
        with opener.open(req, timeout=60) as r:
            content = r.read()
            return (
                json.loads(content)
                if "json" in r.headers.get("Content-Type", "")
                else content
            )

    # Older ComfyUI releases have no --offline switch.
    help_text = subprocess.run(
        [sys.executable, str(args.comfy_root / "main.py"), "--help"],
        cwd=args.comfy_root,
        capture_output=True,
        text=True,
    ).stdout
    extra = ["--offline"] if "--offline" in help_text else []
    if args.front_end_root:
        extra += ["--front-end-root", str(args.front_end_root.resolve())]
    with tempfile.TemporaryDirectory(prefix="h3-comfy-check-") as td:
        directory = Path(td)
        for name in ("input", "output", "user"):
            (directory / name).mkdir()
        log = (directory / "server.log").open("w+")
        proc = subprocess.Popen(
            [
                sys.executable,
                str(args.comfy_root / "main.py"),
                "--cpu",
                *extra,
                "--disable-all-custom-nodes",
                "--whitelist-custom-nodes",
                "ComfyUI-H3-Continuum-Plus",
                "--port",
                "8199",
                "--input-directory",
                str(directory / "input"),
                "--output-directory",
                str(directory / "output"),
                "--user-directory",
                str(directory / "user"),
            ],
            cwd=args.comfy_root,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(120):
                if proc.poll() is not None:
                    raise RuntimeError("ComfyUI exited before startup")
                try:
                    info = request("/object_info/H3ContinuumTimelineStudio")
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.25)
            else:
                raise RuntimeError("ComfyUI startup timed out")
            assert "H3ContinuumTimelineStudio" in info
            for node in (
                "H3ContinuumTimelineSampler",
                "H3ContinuumTimelineTake",
                "H3ContinuumTimelineExport",
            ):
                registered = request("/object_info/" + node)
                assert node in registered
                if node == "H3ContinuumTimelineSampler":
                    schema = registered[node]["input"]
                    assert all(
                        f"reference_image_{i}" in schema["optional"]
                        for i in range(1, 9)
                    )
                    assert "driving_audio" in schema["optional"]
                    assert schema["optional"]["width"][0] == "INT"
                    assert schema["optional"]["height"][0] == "INT"
                    assert schema["required"]["base_seed"][1]["control_after_generate"]
                    assert "driving_audio" in registered[node]["output_name"]
            assert any("timeline_studio.js" in u for u in request("/extensions"))
            movie = directory / "source.mkv"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=s=64x64:r=25:d=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=44100:duration=2",
                    "-c:v",
                    "ffv1",
                    "-c:a",
                    "pcm_s16le",
                    "-threads",
                    "1",
                    str(movie),
                ],
                check=True,
            )
            asset = request(
                "/h3_continuum/timeline/import?name=source.mkv",
                raw=movie.read_bytes(),
                headers={"Content-Type": "application/octet-stream"},
            )
            # Verify the alternative no-copy route against a real ComfyUI
            # server: link an allowed source already on the server, and ensure
            # the asset registry contains only a path pointer, not movie bytes.
            linked_movie = directory / "input" / "link_source.mkv"
            subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-i", str(movie),
                    "-map", "0", "-c", "copy", "-metadata",
                    "title=link-smoke", str(linked_movie),
                ],
                check=True,
            )
            linked = request(
                "/h3_continuum/timeline/link", {"path": str(linked_movie)}
            )
            assert linked["storage"] == "linked"
            assert not (
                directory / "input" / "h3_timeline_studio" / "assets" / linked["id"]
            ).exists()
            assert (
                directory / "input" / "h3_timeline_studio" / "assets"
                / (linked["id"] + ".link.json")
            ).is_file()
            assert len(
                request(
                    "/h3_continuum/timeline/thumbnail/" + linked["id"] + "?pts=1"
                )
            ) > 100
            # Browser metadata-only drag resolves the same original file.
            import hashlib
            from ComfyUI_H3_Continuum_Join.timeline.discovery import CHUNK_BYTES

            data = linked_movie.read_bytes()
            dropped = request(
                "/h3_continuum/timeline/resolve-dropped",
                {
                    "name": linked_movie.name,
                    "size": len(data),
                    "chunk_size": CHUNK_BYTES,
                    "chunks": [
                        hashlib.sha256(data[i:i + CHUNK_BYTES]).hexdigest()
                        for i in range(0, len(data), CHUNK_BYTES)
                    ],
                },
            )
            assert dropped["id"] == linked["id"]
            assert dropped["storage"] == "linked"
            assert not (directory / "input" / "h3_timeline_studio" / "assets" / linked["id"]).exists()
            try:
                request("/h3_continuum/timeline/link", {"path": str(movie)})
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
            else:
                raise AssertionError("Link outside configured roots was accepted")

            project = json.loads(
                info["H3ContinuumTimelineStudio"]["input"]["required"]["project_json"][
                    1
                ]["default"]
            )
            project.update(
                width=64,
                height=64,
                duration_frames=48,
                selection=[0, 48],
                assets=[asset],
            )
            clip = {
                "id": "source",
                "asset_id": asset["id"],
                "start": 0,
                "end": 48,
                "source_in": "0",
                "speed": "1",
                "padding": "error",
                "audio_role": "both",
            }
            project["tracks"] = [{"id": "v", "role": "retained", "clips": [clip]}]
            project = request(
                "/h3_continuum/timeline/project",
                {"project": project, "expected_revision": 0},
            )
            assert (
                request("/h3_continuum/timeline/project/" + project["project_id"])
                == project
            )
            compiled = request("/h3_continuum/timeline/compile", project)
            assert compiled["jobs"][0]["windows"][0]["video"] == []
            assert (
                len(
                    request(
                        "/h3_continuum/timeline/thumbnail/" + asset["id"] + "?pts=1"
                    )
                )
                > 100
            )
            assert (
                len(
                    request(
                        "/h3_continuum/timeline/waveform/"
                        + asset["id"]
                        + "?start=0&end=2"
                    )["peaks"]
                )
                == 512
            )
            job = request(
                "/h3_continuum/timeline/export",
                {"project": project, "codec": "lossless"},
            )
            for _ in range(100):
                status = request("/h3_continuum/timeline/export/" + job["id"])
                if status["status"] != "running":
                    break
                time.sleep(0.1)
            assert status["status"] == "complete", status
            preview = request(
                "/h3_continuum/timeline/preview",
                {"project": project, "start": 12, "mode": "composed"},
            )
            assert preview["start"] == 12 and preview["end"] == 48
            assert len(request(preview["url"])) > 100
            # Validate and execute real Comfy graph serialization with no weights.
            prompt = {
                "1": {
                    "class_type": "H3ContinuumTimelineStudio",
                    "inputs": {"project_json": json.dumps(project)},
                },
                "2": {
                    "class_type": "H3ContinuumTimelineExport",
                    "inputs": {"timeline": ["1", 0], "format": "lossless"},
                },
            }
            queued = request(
                "/prompt", {"prompt": prompt, "client_id": "timeline-acceptance"}
            )
            for _ in range(100):
                history = request("/history/" + queued["prompt_id"])
                if history:
                    break
                time.sleep(0.1)
            assert history[queued["prompt_id"]]["status"]["status_str"] == "success", (
                history
            )
            if args.browser_test:
                env = dict(os.environ)
                env["H3_TIMELINE_TEST_URL"] = base
                env["H3_TIMELINE_TEST_PROJECT"] = json.dumps(project)
                subprocess.run(
                    ["node", str(args.browser_test.resolve())], env=env, check=True
                )
            # Rendered take metadata fixtures exercise the real review API.
            # They do not stand in for trained-model sampling acceptance.
            current = request("/h3_continuum/timeline/project/" + project["project_id"])
            compiled = request("/h3_continuum/timeline/compile", current)
            take_id = "f" * 32
            project_dir = (
                directory
                / "input"
                / "h3_timeline_studio"
                / "projects"
                / project["project_id"]
            )
            record = {
                "id": take_id,
                "indices": [0],
                "range": [0, 48],
                "output_range": [0, 48],
                "selection": [0, 48],
                "parent_take": None,
                "status": "sampled",
                "guide_scope": compiled["jobs"][0]["guide_scope"],
                "input_hashes": [compiled["jobs"][0]["windows"][0]["generation_hash"]],
                "source_canvas": [64, 64],
                "generation_size": [96, 64],
            }
            (project_dir / f"take-{take_id}.json").write_text(
                json.dumps(record), encoding="utf-8"
            )
            (project_dir / f"render-{take_id}.json").write_text(
                json.dumps({"asset": asset}), encoding="utf-8"
            )
            for action in ("accept", "reject", "accept"):
                current = request(
                    "/h3_continuum/timeline/project/" + project["project_id"]
                )
                reviewed = request(
                    "/h3_continuum/timeline/take/select",
                    {
                        "project_id": project["project_id"],
                        "take_id": take_id,
                        "action": action,
                        "expected_revision": current["revision"],
                    },
                )
                if action == "accept":
                    assert [reviewed["width"], reviewed["height"]] == [96, 64]
                    assert take_id not in reviewed.get("rejected", [])
                    generated = next(
                        t for t in reviewed["tracks"] if t["role"] == "generated"
                    )
                    assert generated["clips"][0]["audio_fallback"] is True
                    assert generated["clips"][0]["audio_role"] == "final"
                else:
                    assert take_id in reviewed["rejected"]
            # A canvas edit made after the frozen sampling request is retained.
            reviewed["width"] = 128
            edited = request(
                "/h3_continuum/timeline/project",
                {"project": reviewed, "expected_revision": reviewed["revision"]},
            )
            preserved = request(
                "/h3_continuum/timeline/take/select",
                {
                    "project_id": project["project_id"],
                    "take_id": take_id,
                    "action": "accept",
                    "expected_revision": edited["revision"],
                },
            )
            assert preserved["width"] == 128
            print(
                json.dumps(
                    {
                        "comfy_nodes": 4,
                        "http": "upload/metadata-only-drag/link/blocked-outside-root/save/reopen/compile/thumbnail/waveform/export/accept-reject-restore passed",
                        "graph": "real CPU queue execution passed",
                        "gpu_sampling": "not exercised",
                    },
                    indent=2,
                )
            )
        except Exception:
            log.flush()
            print((directory / "server.log").read_text()[-7000:], file=sys.stderr)
            raise
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            log.close()


if __name__ == "__main__":
    main()

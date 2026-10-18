# Continuum Timeline Desktop (Windows)

This is the native-path companion for Timeline Studio. It hosts **your existing
ComfyUI frontend** in a hardened Electron window. It is not a second ComfyUI
server, media store, editor implementation or upload service.

### Why this is necessary

Ordinary Chrome does not expose a dropped Explorer file's absolute path.
Electron's supported `webUtils.getPathForFile` does, inside the isolated
preload. The bridge maps `D:\\Folder\\movie.mkv` to `/mnt/d/Folder/movie.mkv`
and sends **only that exact server-readable path** to the existing
`/h3_continuum/timeline/link` route. It does **not** scan any directory,
upload movie bytes, copy media or disclose original paths to page JavaScript.

The ComfyUI backend verifies and hashes the selected file and stores a small
persistent content-addressed pointer. ComfyUI must run in WSL with the selected
drive mounted. It can still create bounded derived previews, generated takes
and final exports, never an extra full source movie on import.

### Running

Build the Windows portable executable from the workflow artifact, or run
`npm install` then `npm start` in this directory on Windows.

Launch `Continuum Timeline Desktop` instead of an ordinary Chrome tab.
With ComfyUI already running at `http://127.0.0.1:8188`, drag the video from
Explorer onto Timeline Studio, or use Import media. No paths or scan settings.
For a different loopback port, launch with `--comfy-url=http://127.0.0.1:PORT/`.

A one-time Windows desktop application is **required**. ComfyUI Patcher alone
cannot grant Chrome access to native drop paths. This app has not yet been
verified through physical Windows Explorer drops or packaged/signed release;
do not claim ordinary Chrome provides zero-copy path access.

### Security / deployment

The desktop window only loads a local loopback HTTP ComfyUI origin. Its preload
isolates Electron APIs; JavaScript receives only registered asset metadata,
never the raw Windows path. UNC, device, remote host and relative paths are
rejected. The server independently restricts each resolved path to allowed
roots and rejects escaping symlinks. Run ComfyUI as a trusted local service;
installed custom nodes execute code with its filesystem permissions.

Electron's sandbox, context isolation and web security stay enabled. External
navigation/window creation is disallowed. The portable build is unsigned; no
installer or Registry release is claimed. Verify the artifact and its publisher
before running.

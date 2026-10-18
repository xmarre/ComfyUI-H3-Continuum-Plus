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
The application checks that ComfyUI is reachable using its `/system_stats`
endpoint before opening the frontend. It automatically tries the default
loopback ports `8188`, `8189` and `8190` using `127.0.0.1` or `localhost`,
and remembers the last successful URL. These are HTTP connection checks,
**not filesystem scans**.

If ComfyUI is unavailable or the frontend navigation fails, the desktop app
shows a dark connection/diagnostics screen **instead of a blank white window**.
Enter the exact local ComfyUI URL you use in the normal browser, including its
port, then select Connect. ComfyUI must already be running; the wrapper never
starts the backend. A custom initial URL may also be provided through
`--comfy-url=http://127.0.0.1:PORT/`.

After it loads, drag the movie from Explorer into Timeline Studio or choose
Import media. The native path is registered via a main-process HTTP request to
the verified backend. The preload does not perform ad-hoc relative network
requests; it communicates over a strictly validated Electron IPC channel.
No movie bytes are sent, copied, or used in drive searches.

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

**Startup troubleshooting:** If the desktop connection screen appears, confirm
that ComfyUI is running and copy the *local* URL shown in your browser address
bar into the application's connection field. Only loopback addresses are
supported; an arbitrary LAN/remote ComfyUI origin is refused so it cannot
obtain native Windows paths. The app now reports HTTP refusal, timeout,
unexpected service responses and page load failures instead of rendering an
empty screen. The last successful URL is saved under the desktop app's
configuration directory; it stores **only the server address**, never media
bytes or Explorer source paths.
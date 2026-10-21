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
### Windows build acceptance

GitHub Actions now executes the **packaged portable executable** against a
minimal, local mock ComfyUI HTTP server. It checks that the window actually
renders a page and that a disk-backed File travels through Electron's native
preload to the backend as a WSL path, without a film upload. It also tests
failed and non-ComfyUI HTTP responses, local port selection, IPC origin
restrictions and native cancellation. The test does not reproduce a physical
Explorer mouse drag or run against your WSL instance. Further backend
sampling/export qualification remains required.

### Stale Timeline Studio detection

If your desktop app shows the obsolete status
`Locating original on mounted Windows drives…`, the running frontend is
**not** the current no-scan implementation. This is a deployment/version
mismatch; Electron alone does not update custom-node Python or frontend files
in the running ComfyUI process.

The desktop now checks **both** the actual Python backend route
`/h3_continuum/timeline/runtime` and the installed JavaScript modules
advertised by ComfyUI's registered extension directory **before loading
the editor**. It rejects an old backend or old served module with a clear
`Timeline Studio installation is outdated` screen. It refuses the obsolete
scanner instead of allowing a new movie import, and discards Electron's
HTTP/module caches when loading a compatible version.

Apply the current PR #51 through your Patcher integration, then **fully
stop and restart the ComfyUI Python server**, and reopen the corrected
desktop executable. Loading a new executable by itself cannot replace the
code in the old ComfyUI process. If the incompatibility screen persists, the
desktop's strict check may have found an older backend, an older served
module, a duplicate registration, or an internal frontend/backend contract
mismatch. Inspect the exact failing version and actual served modules before
concluding that the installation is stale. The screen shows the local server
address being checked. This validation performs no recursive filesystem
search or movie upload.

Use **Import…** for selective ZIP, JSON, text, pasted prompts, media and read-only State Manager sources. Preview components and timing before applying in one Undo step. **Link original media** retains ordinary native-path movie linking. See the Timeline user guide for source ownership and merge/replace behavior. Install the desktop artifact matching the frontend/backend v24 contract (`h3-timeline-audio-lock-v24`). The Windows CI acceptance test uses the actual checked-in frontend modules, not a synthetic version-matched substitute.

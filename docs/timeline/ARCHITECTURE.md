# Timeline Studio developer contract

## Ownership and schema

`timeline/model.py` validates `h3_continuum_timeline_project_v1` and compiles logical windows without loading models or decoding media. `Timeline Studio` emits a request-local deep copy as `H3_CONTINUUM_TIMELINE_PROJECT`. `timeline/runtime.py` supplies invocation-local conditions to `v2/sequence.py` after its native geometry is resolved. All latent shapes, masks, context extraction, RoPE, model cloning and CPU chunk retention remain in Continuum.

The JSON root contains `schema`, `project_id`, rational `fps` (currently 24), integer `duration_frames`, `chunk_frames`, output dimensions, operation, assets, tracks, prompts, selection, markers and a legacy editor-state field (`ui`) that compilation ignores; current editors keep view state in node properties. Assets use registered SHA256 IDs. Clips contain `[start,end)` output frames, absolute rational `source_in`, rational speed, padding and independent audio role. Source time is `source_in + (frame-start)*speed/24`. Integer sample boundaries derive from absolute rational project time, never repeated per-chunk rounded durations.

Track roles are retained, generated, video_guide, image_reference, audio and prompt. Track order is semantic for overlapping composition; lock/visibility/name are not sampling inputs. Audio roles are neither, guide, final, both and reference. New schemas require explicit migration; unknown versions do not guess at temporal semantics.

`accepted_windows` records selected take IDs, logical chunk spans and parents. It allows a sub-selection to include an authenticated accepted prefix for reuse. The selected output interval remains unchanged. Immutable take records carry input hashes, native revision/session references, physical output intervals, chunk file identities, compiled model-input manifests and the project snapshot. Renditions are separate immutable registered media. Accepted choices belong to the revisioned project. Normal sampler queueing after the latest matching take is rejected requests a fresh native nonce. Rejected complete and interrupted revisions cannot supply an automatic nonce-resume candidate. Normal queueing of a selected rerolled take restores its stored nonce boundary and effective nonce; partial records also capture that seed contract.

## Identities and lifetime

Generation identities include effective compiled text and its timed segments, ordered active images, selected video intervals and guide/reference audio inputs. Final audio has a separate identity. Actual reference hashes and presentation mappings enter the physical descriptor; the legacy encoder cache also includes that presentation for Timeline Studio requests. No UI/layout metadata is salted into sampling hashes.

Native Run Storage provides model, encoder, VAE, sampler, sigma and per-chunk authentication, atomic chunk commits, locks and interruption recovery. Timeline's optional selected revision pins the causal alternative; rejected revisions are excluded from both exact and fallback reuse, while existing callers retain ordinary longest-valid-prefix selection. The full guide-encode identity remains global within a bounded native job, as in V3.4. Each job hashes the guide clips over the exact encoded range resolved from its actual causal output offset, including the 34-frame right context, and records that scope on its takes; staleness is recomputed over the recorded scope, and records without one keep the project-wide rule. Guide-audio scope identities are versioned; takes with the older margin-based scope regenerate. A narrower Audio VAE dependency/phase contract is unproven. Final-only audio is excluded. Scheduled visual references remain per-window.

Accepted-prefix comparisons include stored native chunk file identities. A different take may share an exact reusable prefix but have an incompatible suffix. Export checks only the causally relevant portion of a retained generated clip and its ancestors. It does not conflate a valid selectable alternative with validity under the currently accepted continuation lineage.

Reference media is materialized at the actual invocation, and conditioning caches are invocation-local. A bounded CPU image cache avoids re-encoding unchanged still references. Completed chunk latents remain CPU-resident. The timeline UI never loads or patches a GPU model. Exact refinement capture is opt-in and aligned with all sampled chunks.

## Media and server boundary

The ordinary web File API cannot yield a real Windows source path. The
`desktop/timeline-native` Electron wrapper is a separate loopback-only
Windows host for the existing frontend, not another media backend. Its
sandboxed/context-isolated preload calls `webUtils.getPathForFile` for an
Explorer-selected File, validates and maps the drive-qualified path to
`/mnt/<letter>/...`, and POSTs only that path to the existing allowlisted
`/link` route. The page sees asset metadata, not the filesystem path.
Invalid Windows paths, UNC shares and traversal sequences fail closed. The
same UI running in ordinary Chrome rejects drop/picker/relink with a clear
native-desktop requirement. No browser byte hashing or upload fallback.

`timeline/mounts.py` enumerates immediate `/mnt` drive-letter mounts solely
to configure the server allowlist; it never searches drive contents.
`timeline/media.py` resolves and validates the precise native-selected path,
streams a SHA-256 hash without copying, probes the file and stores a persistent
SHA-256-named JSON pointer. Mutated, missing, symlink-escaping or disallowed
sources are rejected. Browser drag/drop never calls the legacy `/import`
endpoint, and the removed `/resolve-dropped` search endpoint cannot run.
The legacy upload endpoint remains for external API compatibility only. The
`/link` request watches disconnection and cancels hashing between blocks;
long ffprobe subprocess operations may remain uninterruptible until return.

Media windows use FFmpeg/PyAV PTS-based frame selection, exact sample-grid PCM
decoding, normalized final composition and bounded previews. Full-stream PCM
disk caching is disabled by default, with explicit opt-in available. Repeated
late audio seeks may therefore decode stream prefixes repeatedly; this and
Windows/WSL actual Explorer drop behavior still require runtime qualification.
Existing content-addressed adoption, repair, locks, source change checks,
project persistence, audio routing and all V3.4 sampler controls are preserved.

`timeline/compose.py` performs nearest-PTS video normalization and retained /
accepted generated composition in one-second blocks, using FFV1/float PCM for
lossless output or H.264/AAC for delivery. These derived outputs are not source
movie copies and are separate from import.

`timeline/routes.py` registers same-origin project, legacy import, native
allowlisted link, thumbnail, waveform, bounded proxy, compilation, take
selection, composition-preview and cancellable export endpoints. The native
wrapper's privileged path bridge is not exposed to third-party origins.

## Frontend

`web/timeline/core.js` contains pure editing operations and undo history. `editor.js` provides the resizable editor. The ruler, clip filmstrips and waveform requests are limited to visible windows. `timeline_studio.js` registers the compact node and its lifecycle. Shortcuts are editor-scoped, typing is excluded, editor key events stop at the dialog, workflow switching disposes editors, cloning and repeated pastes fork identity, and graph serialization carries the project widget. Opening prunes completed queued IDs against a captured ownership set, preserving jobs queued while the response was pending; a disposed editor cannot persist a delayed response. Editor view state is stored in node properties rather than the project widget, so it is never part of the prompt or its cache signature.

Each queue action has an immutable batch identity. Ordered jobs choose the predecessor from that batch, so interleaved projects or batches do not borrow the latest unrelated take. Native storage namespaces use parent chunk payload identity, allowing an equivalent reused parent to retain downstream caches.

Editor rerolls request native nonce allocation under the run lock, including a fresh alternative after an interrupted reroll; direct facade inputs still support explicit nonce replay. A queue action freezes the complete API graph once and clones that snapshot per bounded job, appending jobs to the queue in order (front-of-queue submission would run later jobs first). It does not repeatedly serialize a changing live graph. Save responses preserve newer local edits rather than restoring an older submitted snapshot.

## Prior-art audit and licensing

The implementation is original MIT code. No code from the following GPL projects was copied: Sonder Editor, comfyui-obvpm-timeline, MiniMaxH3 Director, LTXDirector or VideoHelperSuite. Their timeline lanes, take lineage, preview, trim and audio handling were inspected as interaction/contract precedents. NKD Preview Tools is MIT; Capricorncd Timeline and OpenTimelineIO are Apache-2.0. Their source selection, waveform, rational-time and clip/parent-range conventions were inspected; no implementation was imported.

Primary sources: [Sonder](https://github.com/SonderSaid/ComfyUI-Sonder-Editor), [Capricorncd](https://github.com/capricorncd/ComfyUI-Capricorncd-Timeline), [obvpm](https://github.com/chanon/comfyui-obvpm-timeline), [NKD](https://github.com/Nekodificador/ComfyUI-NKD-Preview-Tools), [H3 Director](https://github.com/seesee75-commits/ComfyUI-MiniMaxH3-Director), [LTXDirector](https://github.com/comfy-nodes/ComfyUI-LTXDirector), [VHS](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite), [OTIO](https://github.com/AcademySoftwareFoundation/OpenTimelineIO).

Flow's `H3_CONTINUUM_REFINE_STATE` and assembly-plan interfaces remain available. Sol, Spectrum, VDN and LoRA loaders continue to own their model/attention implementations. Timeline Studio does not mutate global ComfyUI classes or accelerator layout tensors.

## Sampler parity and workflow migration

The Timeline facade copies native V3.4 control definitions instead of maintaining reduced substitutes. Studio stores the selection, chunk timing and scheduled prompts; sampler width/height inputs determine generation dimensions; optional connected Sequence Prompt text uses native format/fallback/provenance handling before routing on the project clock. Persistent image sockets precede scoped references, and compiler alias numbering records that offset for take staleness. Scoped video overrides persistent video only in its active windows. Connected Driving Audio overrides guide lanes and carries its unchanged project-clock slice to native assembly and the appended output.

Audio continuity, diagnostics, preview, debug and request-local Qwen lookahead reach the native loop. Timeline lookahead uses the same bounded CPU snapshot, planned geometry and actual descriptor/key verification as V3.4. It may speculate still-image conditioning; scoped video/voice stays sequential. Speculative descriptors do not enter actual invocation manifests. Image/video size modes enter a versioned Timeline storage contract, preventing cache reuse across sizing changes.

Run Storage Off bypasses raw chunk caching while retaining immutable take/session persistence. Exact refinement capture forces all chunks to sample, including when a cached compatible take exists. Seed-control widget callbacks run once around a frozen editor batch, preserving one base seed across jobs and advancing controls for the next submission. A seven-widget workflow migration inserts the standard Core seed-control value before positional assignment; existing six output indices are stable.

## Sampler layout and shared project controls

Sampler widgets follow V3.4’s order. Width and height are optional native INT inputs for compatibility with existing API graphs; omission inherits the project canvas. Runtime overrides affect the request-local project, native cache contract, reference preparation and latent dimensions. Take records retain source and generation canvas sizes. Acceptance adopts the resolved canvas only if the project canvas still matches the source snapshot. A continuation parent must have the requested generation dimensions; it is never silently resized.

Chunk count and chunk seconds are project-bound canvas widgets, excluded from sampler parameter serialization. Their callbacks edit the connected Studio project and its open editor history, so compilation, frozen queue snapshots, timing display and undo share one source. Width/height widget edits also update the unlinked project canvas. Graph-connected INT inputs remain authoritative at execution.

Visual reordering is separate from saved identity: positional legacy values migrate to names, reference sockets are sorted together, and every connected input updates its LiteGraph target slot without changing the link ID or origin. The editor’s per-job boundary/nonce overrides update both positional and named workflow values.

## Low-latency transport and exact final rendering

The viewer's Play/Pause path is not `export_project(..., render_range=30s)`.
`timeline/preview.py` prepares at most eight seconds; first playback needs
only two seconds, then six-second segments are prefetched with cancellation.
Straightforward retained footage and its unmodified aligned soundtrack use a
single short FFmpeg source seek and fast H.264/AAC encode. Complicated overlays,
reverse/speed edits or mixed audio use the existing compositor but only for a
small segment. Its `preview=True` audio path uses source PTS seeking,
separate from the sample-exact generation/export decoder. Main export and
native H3 audio samples remain unchanged. The editor shows measured prepare
milliseconds and cached status, and ordinary seeking fetches a thumbnail
rather than a background 30-second video transcode. Playback does not preload
an entire two-hour movie or allocate a whole-film image tensor. A server-side
thread event cancels FFmpeg work when the client disconnects; preview CPU
slots are isolated from thumbnail/waveform slots. There can still be segment
switch latency on slower WSL source drives; actual streaming smoothness
requires physical runtime testing.

Thumbnail, waveform and proxy endpoints also watch client disconnects and
propagate a per-request cancellation event into CPU/FFmpeg work. Visual
waveforms are approximate and use bounded random access audio; this is
separate from generation/audio export's exact sample-grid decoder. This
prevents rapid scrubbing or a late waveform tile from initiating repeated
hour-long audio prefix decodes while the original PCM cache is disabled.

## Direct full-resolution export and persistent finished files

The final export no longer constructs a full-film temporary FFV1 silent-video
file, a separate full-film raw PCM file, and a second full-film FFmpeg
transcode. `timeline/export_stream.py` muxes one-second video/audio blocks
directly into a single atomic Matroska (FFV1/float PCM) or MP4 (H.264/AAC)
output, at the explicitly selected delivery geometry (native video source or
H3 canvas) and the fixed 24 fps timeline clock. Geometry resolution is done by
`export_dimensions` using verified video stream metadata. Highlighted visual clips
pick the source geometry; unhighlighted ranges require an unambiguous source size.
Ambiguous or absent source footage fails clearly instead of silently reverting
to the smaller sampling canvas. The compositor remains 8-bit RGB: native-sized
FFV1 export is pixel-lossless after decode/color conversion and temporal sampling,
not a bitstream copy or a 10-bit/HDR preservation guarantee. The output node's
legacy default is H3 canvas; editor exports default to original source pixels. It maintains absolute sample-index PCM mixing using a temporary
per-export `PcmCache` so a two-hour input soundtrack is decoded only once,
not repeatedly from the start for every second. Scratch PCM is on the output
volume, capacity-checked in advance, and removed whether export succeeds,
fails or is cancelled. Only the final encoded file is committed by atomic
rename. The `/export/{job}` status exposes stage, completed frames,
total frames and elapsed time; `/export/{job}/file` streams a completed
file from the configured ComfyUI output directory without accepting
arbitrary user-provided filesystem paths. Export output is kept after the
editor window closes, and in-flight export cancellation is independent of
ComfyUI sampler cancellation. The media preview path remains independently
bounded and uses the **full timeline canvas resolution**, never a hidden
480-pixel longest-edge reduction.


## Bounded source thumbnail cache and external queue persistence

Main preview stills request independent output dimensions based on the visible viewer and source aspect ratio, capped at 2048×1152, and use high-detail JPEG encoding. Tiny filmstrip tiles retain the original 240×136 default. The backend validates dimensions and incorporates them into the immutable cache key. Extremely long clip DOM elements are clipped to the current viewport, retaining the absolute frame-to-pixel transform and showing trim handles only where their true edges are visible.

The timeline's visible filmstrip no longer recreates image elements for the same clip, source-PTS and tile grid after each viewport redraw. The thumbnail API resolves the source's immutable registered SHA, uses canonical rational PTS to form a deterministic disk-cache key, and coalesces concurrent cold requests per key. It atomically publishes derived JPEGs and prunes the cache at 512 MiB. The 240×136 filmstrip JPEG is only an inspection derivative, never fed into H3 sampling or final export. Thumbnail work stays behind the bounded background media workers. Rapid UI seek events coalesce within 80 ms while frame/playhead state updates synchronously.

Continuum Timeline Studio submits immutable snapshot jobs through the normal `api.queuePrompt` integration, including DoRA State Manager's persistent-library write barrier and unsaved managed Text Box protection. It never invokes a raw backend submission to bypass this external owner. Rejection diagnostics report confirmed job count; State Manager owns the explicit `Save connected` action. Queue failure after a network response loss can be ambiguous and is not misreported as proof of nonexecution.

## Explicit export range contract

`Render [in,out)` is the editor's canonical selection. The editor sends
`render_range: [in,out]` on every export. The backend compares it to the
validated saved project selection and rejects omitted, mismatched, invalid or
out-of-bounds ranges instead of defaulting to `[0,duration_frames)`.
The native ComfyUI Timeline Export node also defaults to `p.selection`.
The server status includes `render_range` and `total_frames=out-in`; the UI
shows the exact exported frame range and duration in seconds and immediately
cancels a backend job if its range does not match the editor request.

For a 1h50 film project, selecting 0–336 renders exactly 336 frames/14 seconds
regardless of the project's full duration. Only clips overlapping that window
enter the compositor. The temporary exact PCM cache is sized to the latest
required source sample for the selected clips (not the entire movie's audio
stream when only its first 14 seconds are needed), with no permanent movie
copy. For a late source segment, the sample-exact audio decoder may still
decode its preceding source prefix; that remaining random-access limitation
must not be hidden by the progress bar.

## Selected clip export identity and close teardown

Clip highlight is UI state distinct from the saved project's general
`selection`. On explicit export, exactly one highlighted retained/generated
video (or same-bounds final audio group) determines the output interval;
the editor commits that interval to `p.selection` and POSTs both its
absolute [start,end) and highlighted clip IDs. The backend verifies clip IDs
against output bounds, then the compositor resolves source timestamps with
`source_time(c,frame)`, including nonzero `source_in` and arbitrary clip
placement. If no compatible clip is highlighted, the explicit render in/out
controls determine export. Disjoint highlight groups are not silently
converted into an enormous union.

The modal Close handler first closes/removes the dialog and signals the editor
registry, *then* performs view-only persistence, request cancellation and
best-effort media cleanup. It never invokes the potentially costly sampler
refresh or HTMLMediaElement.load synchronously before detachment. Native
Electron close uses main-process BrowserWindow.destroy followed by app.exit,
so renderer beforeunload handlers cannot indefinitely veto the Windows X.

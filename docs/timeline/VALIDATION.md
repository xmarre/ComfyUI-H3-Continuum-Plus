# Validation and hardware acceptance

## Executed evidence and outstanding hardware acceptance

The rejected browser-fingerprint implementation required recursive drive
searches and is **not** the current architecture.

The replacement native desktop mirror passed GitHub Actions
[Python/JavaScript run #39111496813](https://github.com/xmarre/ComfyUI-H3-Continuum-Plus/actions/runs/39111496813):
**581 Python tests per version on Python 3.10–3.13**, **21 frontend tests**,
and publisher validation. Windows
[run #39111496829](https://github.com/xmarre/ComfyUI-H3-Continuum-Plus/actions/runs/39111496829)
passed **17 native tests** covering local URL selection, connection refusal,
ComfyUI health response checks, startup error display, native-path validation,
main-process IPC restrictions and cancellation.

Unlike the earlier mock-only builds, Windows CI also **ran the packaged portable
Electron executable**, loaded a mock ComfyUI HTTP server, injected a genuine
disk-backed file into Chromium's native file picker, verified the isolated
preload path handoff, and confirmed that **only the original path** reached
the mock `/link` route. This materially tests the packaged application
bootstrap and native handoff, not merely JavaScript mocks.

Additional deployment regression: the Windows desktop verifies a live no-scan runtime manifest, checks the *actual served* editor and entry JavaScript, and refuses a stale Patcher overlay, duplicate registrations or the old scanning UI. The frontend entry imports its editor with an explicit version parameter; the desktop clears its private module caches before loading.

Outstanding production acceptance: physical Windows Explorer drag into the
real ComfyUI frontend; real WSL path and FFprobe; source persistence across
ComfyUI restarts; long-MKV playback, audio seeks, export and later native H3
queued jobs. The packaged smoke's mock server does **not** establish these.

| Layer | Executed check | What it establishes |
| --- | --- | --- |
| Temporal compiler | Exact `[0,7)` / `[7,14)` source windows; absolute 44.1/48 kHz sample boundaries; long prompt lists; exact outer-header isolation; malformed-text fallback | Deterministic logical scheduling and role separation |
| Actual native sequence loop with CPU sampler fixtures | Invocation-local reference changes under both compiler modes; changed chunk 2 preserves chunk 1; equal encoder inputs reuse conditioning while changed references do not | Runtime routing, causal prefix and cache integration; not H3 output quality |
| Real media | FFmpeg/PyAV 25 fps, VFR and nonzero-PTS sources; mono/stereo normalization; exact guide-frame padding and original source selections | Source decoding and normalized time/sample behavior |
| Audio windows | Cached PCM slices equal sequential decoding sample for sample; one cache decode serves repeated windows; oversized streams retain only selected samples; a forced concurrent eviction cannot remove an unopened mapping; cache waits cancel | Bounded selected-window memory and safe cache lifetime; oversized streams use repeated prefix decoding |
| Prompt transport | Timed regions compile through the native physical compiler onto each invocation's clock, including continuation context; no neighboring chunk text | Correct physical-local prompt timing; not H3 prompt adherence |
| Long source | A real two-hour video, with a selected seven-second window, decodes fewer than 64 source frames including seek context | No complete-film decode on the selected video path; does not quantify every codec's memory use |
| Composition | Lossless normalized RGB and float PCM comparisons; replacement interval with unchanged original frames and soundtrack outside it; generated-audio fallback against partial/muted final audio; cancellation cleanup | Working retained/generated composition and normalized export |
| Persistence / no-copy drop | Atomic project saves; incremental drag fingerprints and strict no-upload frontend requests; direct native-selected file linking without any directory scans; path allowlisting; missing/altered source rejection; link persistence across reopen; PCM cache opt-in | Disk/project identity behavior, unsupported-hard-link fault injection, concurrent fallback imports, corrupt-copy repair including coarse filesystem timestamps, and verification cancellation |
| Real ComfyUI server | Import, save/reopen, compile, thumbnails, waveform, bounded composition preview and export endpoints; four registered nodes; actual queued Studio→Export execution; accept/reject/restore through rendered take metadata fixtures | HTTP/node integration without trained weights |
| Real ComfyUI frontend | Open, split, undo/redo, save/reload, typing isolation, view state kept out of the prompt input, A/B transport, composed AV preview, independent cloning and repeated paste, multi-job reroll queued in order with a fresh nonce, workflow disposal, graph serialization, delivered sampler-workflow serialization, 70-job cancellation ownership after reopening and a pending-to-running cancellation race | Browser integration on ComfyUI 0.41.0 / frontend 1.55.18; earlier coverage on frontend 1.55.17 |

Ripple editing tests cover spanning soundtracks, source coordinates, locked tracks, timeline duration, markers and selection. Native storage tests exclude rejected exact, fallback and preferred revisions, preserve rejected complete/interrupted manifests while allocating a fresh nonce, and restore selected reroll seed contracts; take staleness follows rejected ancestors and the actual multi-job audio encode scope.

The editor suite also verifies delayed open-time queue pruning against newly queued and concurrently cancelled IDs. Unsupported hard links are exercised by filesystem-error injection; a physical FAT/exFAT volume was not used.

The browser run reported zero page exceptions. The offline frontend emitted unrelated missing-static-resource and blocked update-check messages. No sampling-quality result is inferred from those browser tests.

## Reproducing automated checks

From the package directory, using the relevant Python/Node environments:

```text
python -m compileall -q .
python -m ruff check . --select E9,F63,F7,F82
python -m pytest -q
node --test tests/timeline_editor.test.mjs
python tools/check_timeline_comfy.py --comfy-root /path/to/ComfyUI
```

The real-server checker expects the package installed in that ComfyUI's custom_nodes directory under `ComfyUI-H3-Continuum-Plus`. It launches an isolated CPU server with temporary input/output/user directories. It never loads trained model weights. Browser acceptance additionally uses Playwright and an installed Chromium:

```text
python tools/check_timeline_comfy.py --comfy-root /path/to/ComfyUI --browser-test tests/timeline_browser.cjs
# optional: --front-end-root /path/to/frontend/static to serve a specific frontend build
```

`H3_TIMELINE_CHROMIUM` can select a Chromium executable. The unit environment explicitly defaults to legacy standalone prompt transport; tests of physical transport opt in independently. Normal ComfyUI package loading retains its production physical-prompt default.

## Trained-H3 gate — not executed

This environment has CPU PyTorch and no CUDA GPU or installed trained H3 weights. Consequently, native H3 sampling, audiovisual phase, perceptual fidelity and accelerator performance remain unqualified. The PR must not be represented as production-qualified from CPU/CI evidence alone.

Start with `examples/timeline/Timeline_Studio_Generate.json`, select installed model/CLIP/VAE files, and keep the example's 24 fps, 768×512 canvas, 168-frame chunks, seed 20261017 and natural assembler timeline. Its baseline is Euler, 19 steps, Simple scheduler, full denoise, Native Masked, 39-frame context. Record any setting changes. The Recreate and Replace workflows use the same actual sampler path; import source media in their editor before testing those operations.

For every run, retain the exact workflow, prompt, registered media identities, model/LoRA/provider versions, sampler/sigma settings, seed, dimensions, logical/physical input manifests, elapsed time and rendered output.

| Case | Acceptance evidence required |
| --- | --- |
| Prompt-only baseline | Compare an identical single-chunk V3.4 job with Timeline Studio; inspect output and report hot-run overhead separately from intended reference encoding |
| Two seven-second guided chunks | Actual H3 sees logical source selections `[0,7)` and `[7,14)`; reference padding and physical context match the recorded descriptor; no neighboring guidance bleed |
| A in chunks 1–2, B in chunk 3 | Inspect actual Qwen picture mappings and encoded reference identities, including first/last-frame combinations |
| Audio-only movie route | No video reference block; guide/final/both/reference routes remain independent; inspect PCM sample alignment and real speech/AV phase at native boundaries |
| Internal retake | Accepted chunk 1 reuses its native payload; reroll chunk 2; stale dependent footage cannot be exported as valid; verify cuts and retained original pixels/PCM |
| Multiple jobs | A selection longer than 16 chunks preserves natural state/output placement and absolute audio phase; inspect the job boundary, not just logical chunk boundaries |
| Context/grouping | Native 39-frame generated AV; supported 5/22-frame guide modes; Guide/Motion Context; scoped no-terminal-merge route; unchanged legacy FL2VA terminal behavior |
| Accelerators | Sage alone, Sage+Sol, Sage+Spectrum and Sage+Sol+Spectrum with identical inputs; external VDN/Flow/refinement contracts when installed; no unexpected model residency or wrapper changes |
| Legacy equivalence | Existing V2 and equivalent V1 3×5-second workflows; old V3.4 serialized graphs; restart/resume and cancellation |
| Frontend coverage | Repeat under enabled Nodes 2.0 renderer variants and any older supported frontend; long-file navigation, concurrent project edits and source relinking during queued work |

A valid input manifest, correct tensor shape or passing CI does not establish visual/audio quality. Do not describe source-guided recreation as pixel reconstruction, assume seamless internal-shot replacement, or infer lip-sync/voice identity guarantees.

## Native sampler parity regression coverage

Fourteen public-facade CPU tests check the native control inventory, storage Off versus compatible resume, size-contract invalidation, persistent/scoped picture numbering, persistent/scoped video precedence, project-clock Driving Audio and final-copy identity, permissive connected prompt formats including project windows after chunk 16, request-local Qwen lookahead order/equivalence, and exact refinement capture after a cached run. The trained encoders and sampler are fixtures; routing, sequence geometry, Run Storage, session files and take records run through the implementation.

The real frontend additionally round-trips audio continuity, both reference sizes, Qwen lookahead and storage Off; migrates a seven-widget sampler without shifting seeds/boundaries/nonces; and verifies Core seed increment occurs once after a two-job Studio batch. The actual server checks all eight image sockets and the appended audio output in object_info.

Sampler layout checks also assert V3.4 widget order, contiguous references, incoming link slot integrity, graph-connected width/height/seed round-trips, and timing-control edits reaching the actual project compiler. Native CPU tests verify requested latent dimensions, cache invalidation on dimension changes and rejection of a different-resolution continuation parent. Real take-review HTTP checks cover adoption of resolved canvas dimensions and preservation of a later explicit canvas edit. A frontend regression check also preserves explicit sampler dimensions when Studio opens without a canvas change.

## Play/Pause regression and runtime qualification

An observed minute-long delay was traced to `/preview` unconditionally
performing a 30-second `export_project` (FFV1 intermediate, audio mix,
then MP4 encode) before calling `video.play()`. Scrubbing also started
30-second Matroska proxy jobs. The replacement uses small seek-based windows,
fast source-only FFmpeg, optional short composition, cancelable workers,
thumbnail-only scrubbing, segment prefetch and immediate Pause. The Python
suite now includes real FFmpeg media-fixture checks for two-second late source
segments, selected-window audio reads and cancellation/partial-file cleanup.
Frontend tests assert synchronous pause during unresolved HTTP, no empty
project encode, bounded request lengths, and request cancellation. Passing CI
cannot establish subsecond playback on multi-hour source MKVs on WSL/DrvFS,
which must be measured on the actual system. Playback audio seek is allowed
to be approximate; generation/final export sample-grid rules remain exact.

The waveform endpoint previously used the exact audio decoder and could
repeatedly decode the source prefix up to late timeline locations when caching
was disabled. Interactive waveform now uses preview-only PTS seeking; detached
thumbnail/waveform/proxy requests are cancellable. These changes do not relax
the generation/export audio phase contract. Long-source WSL performance and
physical native playback still require runtime qualification.

## Image Conveyor per-chunk reference routing (2026-10-19)

Prior chunk cards incorrectly offered imported still-image assets as the primary
reference source, while H3 reference IMAGE tensors actually arrive from existing
Image Conveyor → Timeline Sampler connections. The editor now enumerates real
wired sampler `reference_image_1..8` inputs, inspects the connected Conveyor's
`state_json.reference_slots` shelf (with live `__bil.state` fallback), and
shows its filenames, cached thumbnails, global output-disabled/empty states,
and per-chunk checkboxes. No source image is imported, duplicated or written
to the Timeline project. The optional `conveyor_reference_slots` map stores
one-based selected socket IDs only; missing means inherit every connected
reference (old projects remain unchanged), `[]` means none.

The native sampler preprocesses only the union of sockets required by the
bounded current job, preserves slot-boundary mapping even for V3.4 bundled
references, and filters image/latent tensors for each invocation and speculative
lookahead. Compiled per-window reference selection contributes to the causal
hash. Picture indices are contiguous per chunk and stale-take verification
uses recorded physical socket counts to avoid false aliases after disabling
a Conveyor reference. Changing sampler chunk duration reflows explicit
selections or vetoes collisions before mutating project state. Global Conveyor
output toggles remain upstream-authoritative; an empty/disabled reference
cannot be forced active by checking it within the timeline.

Regression tests exercise inherited vs explicit empty selections, rejected
invalid masks, per-chunk causal hash isolation, tensors selected by physical
socket (including bundle groups), alias offsets, run reuse/staleness, current
Conveyor graph shelf display metadata and timing reflow. CI validates the
code and browser modules, but real Image Conveyor preset switching and GPU
MiniMax-H3 visual behavior still need an installation-site test.

## Chunk-first sampler-input authoring regression (2026-10-19)

The generation editor previously exposed global project prompts and a full
source-film timeline while chunk count/duration existed only as transient
sampler widgets. The new `chunk_workspace.js` authors exact chunk-boundary
clips; `chunk_ui.js` shows explicit per-chunk prompt/image/video/audio slots,
source PTS, the project compiler's per-window summary and native sampler
persistent socket/parameter values. Global output 24 fps timing remains owned
by the same `chunk_frames` and `selection` as native generation. A focused
viewport prevents an imported two-hour source from obscuring the selected
two-/three-chunk preparation workspace. Eight input cards are rendered per
page; large selections use a bounded page preview instead of recompiling
thousands of logical windows on every edit.

Regression coverage checks absolute selection at 14952–15288 output frames,
source PTS inheritance from late retained footage/soundtrack, image/video/
audio role routing, exact chunk-aligned prompt clips, undoable sampler timing
reflow, graph-connected sampler precedence, and the native connected
`sequence_prompt` fallback/explicit chunk override. The latter uses one
`with_external_prompt_source` transformation in physical sampling and
stale-take verification. Existing unmarked/free-form prompt regions keep their
previous external-source precedence. Source-in PTS and speed are unchanged
by output chunk-length edits. Authored clips **outside** the selected render
window are not moved as a side effect. Source PTS edits accept the backend's
rational forms (e.g. `47/2`) and reject nonfinite values. An integration browser assertion verifies a
visible chunk prompt is serialized into the actual Studio project widget.

CI tests and packaged Windows Electron smoke tests establish structural
compatibility. They do not establish real physical Windows Explorer input
quality, real-time 2-hour movie editing throughput, or MiniMax H3 GPU output
quality. Native sampler widgets, persistent audio/reference inputs and
non-Timeline V3.4 workflows remain intact.

## Long-source UI and State Manager queue regression (2026-10-19)

The screenshot's main still previously magnified the **240×136 filmstrip JPEG**, introducing visible softness independent of H3 sampling or final-export quality. The viewer now requests a separately cached display-resolution still, and 90-quality non-subsampled JPEG is used for that path. Filmstrips stay small. The 2-hour clip surface is projected into the visible viewport instead of creating an enormous clip DOM box, preserving true trim-edge visibility and source timing. JS regressions assert clip bounds, viewer pixel density and seek coalescing; HTTP acceptance checks reuse of a cached JPEG and separate 960×540 still sizes. **Actual long-MKV speed improvement remains unmeasured** on the user's Windows/WSL installation.

The queue guard was traced to the connected DoRA State Manager `DSM_UNSAVED_MANAGED_TEXT` safety check, not an H3 model error. Timeline queueing still honors that external guard. Failure messages distinguish confirmed jobs from uncertain submissions; prompt persistence is intentionally not automatic. A JS test checks the diagnostic. The selected persistent State Manager prompt must be saved through **Save connected** before the queue can succeed.

## Source-resolution export and explicit H3 guidance regression (2026-10-18)

The editor originally wrote the selected movie clip through the project canvas
(typically 768×512) even when its registered original was higher resolution.
FFV1 only preserved those downscaled pixels. The source-resolution option now
selects verified source geometry independently of H3's generation canvas. A
CPU/FFmpeg fixture exercises 64×64 source footage with a 32×32 timeline,
checks exact FFV1 output pixels at source size, keeps the existing canvas-size
regression, and rejects ambiguous mixed sources and audio-only native export.
The JS routing test verifies that one retained clip is placed directly onto a
video-guidance track with exact source PTS, idempotently and without an export.
The native-path protocol was bumped to require a correspondingly updated
Windows host, served editor and ComfyUI backend. Real 1080p/HDR clips, desktop
Explorer import and GPU sampling remain unqualified until exercised on target
hardware; native spatial resolution does not preserve >8-bit precision or
source VFR timestamps.

## Full-resolution playback and final export regression (2026-10-18)

An earlier latency fix silently reduced composed playback to at most
480 pixels on its longest edge. That quality regression was unrelated
to the Play/Pause start delay and has been removed. Preview now preserves
`project.width` / `project.height` without changing generation geometry.
Existing short-segment input seeking is responsible for latency reduction.

The original final export encoded an entire silent FFV1 film and raw PCM
scratch audio then ran FFmpeg over the entire film a second time, without
usable progress or an output download action. For long full-film timelines,
that was costly in time and temporary disk space; with the default disabled
PCM cache, repeated source-prefix audio decodes compounded the delay.
The new direct final-container mux has real encoded audio/video roundtrip
tests for FFV1/PCM and H.264/AAC, final-file atomicity/progress, reuse of
exact PCM across output blocks, and a configured-canvas video preview above
480px. The ComfyUI HTTP acceptance script now checks export-status progress and
direct file downloads for both formats; this *script is not included in the
isolated CI matrix*, and needs a real ComfyUI server run before declaring HTTP
integration qualified. The *actual user's two-hour WSL MKV* also remains
untested, and no subsecond runtime claim is made from synthetic fixtures.

## Selected export versus entire movie (2026-10-18)

A user demonstrated that `Render [in,out) = 0–336` still triggered an export
that made only 1% progress after 104 seconds. The backend incorrectly called
`export_project` without `render_range`, causing it to render
`[0,project.duration_frames)` regardless of the selection. The corrected
frontend POST includes an explicit `render_range`; the route enforces exact
agreement with validated `p.selection`, reports the selected range/total
frames, and rejects clients missing the range. The native Export node and
`export_project` default to `p.selection`, not the whole project. Tests
include a 159000-frame project with 0–336 selected and assert a 14-second
output plus a maximum decoded audio prefix of 14 seconds. A nonzero selection
also must yield an output file with timestamps reset to zero. Existing exact
export/generation audio semantics remain unchanged. Media selection from a
late source position still incurs exact PCM prefix decoding up to that source
position. HTTP acceptance script checks that a missing scope is rejected.

## Highlighted clip vs stale render range; unresponsive Close (2026-10-18)

User screenshot: a retained footage clip outlined in yellow has timeline
bounds 17136–17283, its Source in field is 714 seconds, while the separate
Render in/out fields still showed 0–336. The earlier range-only fix exported
the start of the *project*, not the highlighted clip. The export action now
resolves the highlighted actual retained/generated video or final-audio clip,
updates the visible saved in/out interval, and exports those absolute output
frames through the existing clip source mapping. It refuses multiple selected
clips with incompatible output intervals. Backend validation checks the
selected clip IDs and verifies that their output bounds match the committed
export range. A front-end test reproduces 17136–17283/source 714; a real
media fixture test places a clip late on a long output timeline, selects its
middle source, and compares decoded first output/source frames. No-video-clip
selection preserves the manually configured render range.

Closing also previously ran `stopPlayback`, workflow serialization and
sampler/canvas refresh, and called synchronous media `load()` before removing
the modal; any slow call could block Close. The modal now detaches first,
then performs best-effort cleanup and cancels active exports. Native Windows
window-close and Ctrl+Shift+Q bypass renderer beforeunload and exit the
Electron process in the main process; tests exercise both. This does not
establish that a severely overloaded Windows host will respond instantly in
the real runtime, which requires an on-device stress test.

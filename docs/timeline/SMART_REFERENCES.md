# Scene Analysis / Smart References

Smart References analyzes registered original footage independently of generation.
Open **Smart references** in Timeline Studio, choose a source, and use **Analyze
selected scene** or **Analyze full source**. Review the proposed shot intervals,
references and compiled inputs, then **Apply shot plan**. Apply is one ordinary
Timeline History edit. It does not queue a workflow, accept a take, change
soundtrack routing, or rewrite authored prompts. Timeline Undo/Redo also applies.

## Detection and selection

The decoder streams the selected source interval on CPU using PyAV presentation
timestamps. Each frame has an integer PTS, rational timebase and duration in a
resumable SQLite measurement cache. No film-sized RGB tensor or GPU frame bank
is created. Complete full-source measurements can supply selected subranges
without decoding their pixels again. Sensitivity changes reuse measurements.
An interrupted job resumes after its last committed presentation timestamp.

Detection combines spatial/color differences, channel histograms, local adaptive
thresholds, flash-return checks and illumination suppression. When OpenCV is
installed, phase correlation also suppresses coherent camera translation.
Hard cuts, gradual-transition candidates and significant within-shot composition
changes are separate records. Detection uses a two-frame lookahead; source
boundaries retain the measured PTS rather than an average-framerate estimate.

Representative frames come from a bounded set of temporal quality buckets in
 each shot, ranked by sharpness and exposure, with temporal/visual diversity.
OpenCV can add a frontal-face visibility proxy on the shortlisted previews.
This proxy does not identify people or prove that no face is visible. Without
OpenCV, deterministic detection/extraction still runs. Occlusion, gaze, posture,
acting and subject identity are not established by pixel differences.

Camera setup reuse requires similar spatial structure and color distribution;
it does not group shots merely by character. The comparison window contains at
most 256 distinct recent setups. Older returns can be missed and corrected
manually. Global extraction budgeting considers the complete analyzed interval,
spreads its choices across source time and favors clear, repeatedly useful
references. It does not spend a whole film's budget on its first shots.

Motion blur, flashes, whip pans, occluders, grain and subtitles remain empirical
failure modes. The detector excludes the usual subtitle band, which can also
miss important changes confined to that band. Dissolves are explicitly labelled
candidates: prolonged movement can resemble a dissolve. **Sensitivity**,
**Split shot**, **Merge with next**, reference replacement, camera setup/location
identities, visible-subject descriptions and per-shot activation are editable.
Physical location identity starts unverified instead of being guessed from two
unrelated views. Manual corrected intervals survive reanalysis while their source
mapping remains valid. Changed source mappings suppress old conditioning.

## Color, resolution and files

Extraction selects the exact measured source PTS through FFmpeg's original clock.
It retains the following independent representations:

| Representation | Encoding | Purpose |
| --- | --- | --- |
| Native decoded master | Single-frame FFV1 Matroska, native sample depth and color tags | Archival decoded samples |
| Source-size SDR conversion | 16-bit RGB PNG; Rec.709 transfer preserved for SDR, mapped sRGB for HDR | Display-referred comparison |
| H3 reference | Lossless 8-bit RGB PNG, independent MP ceiling | Actual registered model input |
| VLM evidence | Lossless 8-bit RGB PNG, independent MP ceiling | Bounded semantic interpretation |
| Preview | Lossless 8-bit RGB PNG, capped at 1 MP | Editor inspection |

PNG compression is lossless; no hidden JPEG/WebP quality setting applies. Each
card reports original/extracted dimensions and effective MP. The MP limiter
preserves aspect ratio and never enlarges a smaller image to fill the ceiling.
Resizing uses the selected Lanczos, bicubic or spline filter. Source dimensions
below the ceiling remain native. Anamorphic sources use square-pixel display
geometry for derivatives; the native master retains decoded sample geometry.

H3's reference pipeline requires a 32-pixel lattice. Extracted H3 images
are floored directly to that lattice when both dimensions are at least 32 pixels,
avoiding a second resize solely for 32-pixel alignment. The sampler can still
downscale for its output/reference size mode. Images below the native 32-pixel
minimum are omitted from automatic conditioning. Physical manifests record actual
tensor shape, MP, image hash and Picture ordinal.

For ordinary Rec.709 SDR the CPU path converts matrix/range without applying
a BT.1886-to-sRGB gamma transformation, retaining source transfer code values.
HDR PQ/HLG is linearized by zscale at the selected 125-nit SDR reference, converted
to Rec.709 primaries, tone-mapped with Mobius/desaturation and encoded as full-range
sRGB. Out-of-gamut conversion is clipped, not perceptually gamut-mapped. Reported
HDR peak metadata is used when available; otherwise a 1000-nit peak assumption
is recorded. Missing SDR tags produce visible Rec.709/RGB assumption diagnostics.
BT.2020 SDR, HDR10/PQ and HLG have explicit conversion paths. Static metadata
available only on decoded frames may not be exposed by the stream probe; check
peak assumptions on difficult material.

**libplacebo** requests BT.2390 tone mapping and Dolby Vision RPU application on
the original decoded frame, before archival conversion could lose side data.
It requires an FFmpeg build with that filter and a usable Vulkan implementation.
Its SDR target luminance follows the installed libplacebo implementation; the
125-nit CPU-path setting does not certify an identical libplacebo display target.
Failures remain analysis errors and do not fall back silently to SDR decoding.
Dolby Vision profile 7 and compatible profile 8 base layers can use the CPU
fallback with a visible warning that dynamic RPU/enhancement-layer processing is
omitted. Profile 5 and incompatible base layers require libplacebo or a separately
color-managed source. The FFV1 frame is not an RPU/enhancement-layer archive;
the immutable original source remains authoritative for those data.

This is not a claim of MPC Video Renderer equivalence. A matched manual-save
comparison on real HDR/Dolby Vision material is required to qualify perceptual
quality, highlight handling, gamut mapping and renderer-specific differences.
The browser comparison shows the source-size SDR conversion, not untransformed
HDR code values interpreted as sRGB.

## Native reference budgeting and prompts

Smart References uses the existing scoped-image/TimelineInputs contract, asset
registration and native ReferenceAssets encoding. It does not create another
Conveyor shelf or change its dynamic socket selections. Selected persistent
socket images come first, manually authored scoped images retain their slots,
and scene screenshots receive only the remaining capacity. Multiple images
bundled in one persistent socket count as multiple images at runtime.

Continuum's current supported budget is **eight simultaneous images**. Core's
reference node exposes nine image sockets; this feature deliberately preserves
Continuum's established eight-image contract instead of widening it. Reserved
identity slots are a minimum reservation, not invented extra images. All selected
persistent/manual images take precedence even if they exceed that reservation.
Budget omissions are visible. Equivalent assets do not consume duplicate scoped
slots. A chunk can receive no scene screenshots. Unrelated screenshots are never
passed to every chunk.

Compilation assigns `<Picture N>` only to physically selected assets. Scene
instruction text references only those selected pictures; runtime recompilation
uses actual socket group sizes. Studio previews infer socket counts from graph
outputs and identify that uncertainty for bundled/dynamic sources. Recorded
physical manifests are authoritative. Image/reference selections, effective
visual text and native preparation version participate in generation identity;
confidence labels, grouping names and editor view settings do not.

Source screenshots request composition, staging, wardrobe and environment.
Dedicated character images and authored replacement-character text retain
priority. This priority is expressed through the conditioning/prompt contract;
it is not a guarantee that H3 will never copy a source actor's appearance.

The visual layer uses the existing timed physical-prompt compiler. Global text,
dialogue, speaker identifiers, authored timing and imported prompt ownership are
preserved. A visual schedule appears once without splitting or copying authored
dialogue into every shot interval. Per-shot user corrections take precedence over
VLM descriptions.
Automatic descriptions are off until **Use reviewed VLM descriptions in prompts**
is enabled. Dialogue is never supplied to or rewritten by the vision model.
Raw JSON, confidence values and detector diagnostics do not enter the prompt.

Shot intervals are **advisory** during H3 generation. Core supports positioned
keyframe guides, but they do not establish a qualified frame-exact cut actuator
for arbitrary short shots. References remain active throughout each physical
invocation. The compiler does not split 15-frame shots into invalid standalone
H3 samples or alter continuation masks/audio locks. No source-versus-generated
cut-timing result is claimed without a trained-model measurement.

## Prompt Writer / LM Studio

Optional interpretation requires Prompt Writer's **visual_observations_v1** API
(implemented by Prompt Writer Plus PR #22). Connect LM Studio using Prompt
Writer's existing **Settings → API providers → Custom** page. Refresh the model
list here and choose that connected vision model. No keys or provider credentials
are stored in the Timeline project. Missing/offline/incompatible Writer or models
do not prevent deterministic analysis and screenshot extraction.

The versioned interface is:

- `GET /h3studio/scene-analysis/capabilities`: schema, image bound, connected
  vision choices and lifecycle contract.
- `POST /h3studio/scene-analysis`: at most four ordered inline PNG/JPEG images,
  evidence IDs, context budget and request-scoped cancellation token.
- `POST /h3studio/scene-analysis/cancel`: cancels only that token.

The provider reuses Writer's transport, capability metadata, request ownership,
streaming errors and cancellation. It does not call the ordinary H3 `/generate`
mode. Structured observations cover environment, framing, posture, gaze,
gestures, expression, lighting, props, continuity, acting and camera movement.
A single still cannot establish camera movement. Invalid/truncated JSON,
unbound evidence, prompt markup and incompatible schemas are rejected. Schema
validation cannot prove semantic truth; review every visual claim.

Evidence selection is bounded per shot and globally. Completed observations are
cached by source/processing identity, selected images, schema, model and context
budget. Repeated evidence reuses observations. Retry resumes from complete cached
work; cancellation preserves both completed records and active timeline edits.

Timeline Studio's actual `api.queuePrompt` submission waits for its VLM worker
to cancel without mutating global `app.queuePrompt`. The backend starts only
while ComfyUI is idle and watches for externally queued work; another client's
queue can race that polling boundary. Writer's existing
VRAM handoff still owns its local-model lifecycle. LM Studio remains externally
managed: cancelling an API request does not unload its resident model. Manage
that residency in LM Studio before sampling if its allocation leaves insufficient
memory for H3. Scene analysis never adds H3 evaluations or decode windows.

## Stored contract

`scene_plan` uses `h3_scene_plan_v1` inside the existing project. It contains
analysis/source identity, settings, reference records and shot occurrences.
Each occurrence stores exact source PTS/timebase, rational half-open source
interval, integer half-open project-frame interval, clip mapping, separate scene
and camera-setup IDs, measured event provenance, selected reference IDs, VLM
observations and user corrections. Reanalysis preserves other source scenes and
unselected intervals; a manual split carries a durable interval override marker.
Observations are kept per model, and user-edited descriptions retain editing
provenance. It does not confuse source time with logical
chunk time or native latent time. The runtime manifest explicitly records that
shot timing is not structurally enforced. Apply rejects a newer editor revision;
analysis workers only publish immutable media/analysis candidates, never the
active project or a queued snapshot.

## Deployment and qualification

Use ComfyUI Patcher overlays: Continuum Plus PR #83 on current main, and Prompt
Writer Plus PR #22 on its main when semantic interpretation is desired. The Writer
PR is independent of its State Manager contribution stack (#19–#21); users of
that stack retain their established order and add the scene API layer after it.
Use the matching `h3-timeline-smart-references-v27` desktop/backend/frontend
contract. The Windows validation workflow produces the matching unsigned portable
artifact. This feature does not merge or release either repository.

Automated coverage includes exact synthetic 67/153 cuts and return reuse, VFR and
one-hour PTS origins, resume/cache behavior, flash/motion/fade checks, PQ/HLG
10-bit codec conversion, no-upscale MP/lattice behavior, physical image transport,
identity budgeting, aliases, prompt/hash invariants, user correction persistence,
production HTTP/Chromium editing and packaged Windows execution. These establish
software contracts, not representative-frame quality on real films. Measurement
databases and extracted-reference caches do not yet have automatic disk pruning.
Each independently analysed selection range may create another SQLite cache,
so full-film storage use is not bounded by the reference-image budget alone.
Two-hour 1080p/4K CPU throughput and disk-use measurements remain unqualified.
Outstanding acceptance includes real therapy/source footage, difficult dissolves/occlusions,
matched MPC HDR/Dolby Vision captures, live LM Studio semantics, trained-H3 cut
fidelity, accelerator combinations and end-to-end GPU performance.

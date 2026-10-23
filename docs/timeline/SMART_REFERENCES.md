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

The visual layer uses the native physical-prompt compiler, including Fixed
prompts that otherwise have no timed segments. The compiler routes the augmented
shot schedule to the actual H3 invocation, rather than displaying it only in
the logical preview. When the experimental physical compiler is disabled,
the legacy nominal conditioner also receives the augmented scene text (including
for next-chunk lookahead), while scene-free windows retain their original prompt.
Global text, dialogue, speaker identifiers, authored timing
and imported prompt ownership are preserved. A visual schedule appears once
without splitting or copying authored dialogue into every shot interval. Per-shot user corrections take precedence over
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


### Continuation shot-schedule clocks

Studio's **nominal preview** and the compiler-disabled legacy conditioning path
use per-window timestamps beginning at 0 seconds. Physical Timeline conditioning
instead carries structured job-relative project-frame shot ranges. The physical
compiler renders `Shot plan:` once, against the exact descriptor's starting
frame (including retained context), so a 34-frame context at 24 fps shifts
current-chunk cuts by 1.416667 seconds. This is an advisory visual instruction,
not proof of frame-accurate edits by MiniMax H3. The physical compiler never
parses nominal shot-plan prose and never duplicates authored dialogue or imports
adjacent chunks' shot descriptions. A single shared identity-precedence sentence
applies across the entire shot schedule.


## October 22 screenshot/Conveyor UX contract

Detection retains source frame PTS and produces a **full-resolution JPEG screenshot**
(quality 95, 4:4:4), without H3-specific downsampling. VLM analysis still
receives separate bounded inference derivatives and the preview can be reduced;
neither changes the screenshot asset. Standard JPEG cannot preserve HDR metadata,
so PQ/HLG/Dolby Vision material still needs explicit SDR color conversion. The
original movie remains the color/metadata authority.

**Download source-resolution JPG** saves an individual screenshot. The optional
**Send selected JPGs to Image Conveyor** action materializes at most eight
unique screenshots under
`ComfyUI/input/h3_timeline_screencaps/<reference-id>.jpg`. It fills only
**connected, empty** persistent shelf outputs of one unambiguous Conveyor node
(upstream Media Resolution connections are respected), without replacing user
character identities. If the active shelf belongs to a named character preset,
the action detaches the preset rather than overwriting that preset. The exact
scene-reference ID → physical shelf slot mapping and per-chunk socket masks are
saved in the Timeline project; those images do not go through the separate
internal reference path again. Dynamic/grouped outputs, ambiguous Conveyor
connections, and insufficient vacant connected slots fail visibly.

Without explicit Conveyor installation, the earlier internal scoped-image
conditioning still functions, with the native reference sampler's own
preparation and size policy. That fallback is **not** the user's Media
Resolution path, and the screen explicitly distinguishes them.

The editor displays the effective compiled MiniMax H3 prompt in a visible,
read-only per-chunk textarea. Its logical labels are chunk-local; the physical
compiler transforms exact source-frame shot ranges against actual context
for the native H3 conditioning clock. This read-only derived text must not be
written over an authored prompt or managed State Manager Text Box, which has
its own save/queue lease. Shot descriptions require explicit user review and
the separate prompt-inclusion setting.

Data are **persistent**, not temporary: under
`ComfyUI/input/h3_timeline_studio/scene_analysis/<analysis-token>/`,
`ComfyUI/input/h3_timeline_studio/scene_references/<reference-id>/master.mkv`,
and `ComfyUI/input/h3_timeline_studio/assets/<content-sha256>`.
The FFV1 master and original movie remain available for native-depth audits;
the normal screenshot is high-quality SDR JPEG. No automatic long-term
pruning is yet provided.


### Image Conveyor ancestry and occupied shelf behavior (2026-10-22)

The sampler's `reference_image_1..8` sockets may connect through any number of
**Media Resolution** or other IMAGE-processing nodes to a Conveyor output.
The export action uses the same bounded upstream ancestry readout used by
Studio's reference previews, accepting a unique source for writes but
explicitly refusing to assume a one-to-one route through an ambiguous
multi-IMAGE merge. The Image Conveyor `ref_image_N` output ordinal is
**not** the sampler input's physical `reference_image_N` ordinal. Source
shelf 5 routed to sampler input 2 must be stored as physical input **2**
in `scene_plan.conveyor_slots` and the chunk socket masks. The backend never
guesses a missing connection.

Existing portrait/identity shelf images are not replaced. When connected
shelf outputs are occupied, the export **still publishes** new full-resolution
JPEGs to empty shelf locations on the same Conveyor and reports them as
**staged, not yet connected to H3**. To condition H3 with those staged images,
the corresponding shelf outputs must be connected through the user's Media
Resolution chain into unused physical H3 sampler IMAGE inputs; rerunning the
same export will reuse matching imported file identities and activate any
new uniquely verified routes. When staged only, the older internal scoped
conditioning still exists and can still use the sampler's reference sizing
policy. Export does not silently invent node links, replace active character
references, or claim staged files are already part of the model input.

The exporter atomically registers all analysis-generated asset metadata in
the Timeline project together with its shot plan and any verified per-chunk
socket selections. Otherwise a plan referencing extracted assets that were
not yet applied could fail project validation. A graph-level Conveyor shelf
edit is **not** reversed by Timeline Undo; the UI requires confirmation before
modifying a live shelf or detaching its active named preset.


## Per-shot Picture cues and the connected base prompt (2026-10-22)

Timeline Studio's saved `prompts.script` is **not authoritative** when a
connected State Manager Text Box drives the sampler's `sequence_prompt`.
The live H3 queue uses `with_external_prompt_source`, not the saved Studio
draft. `POST /scenes/compile` now accepts the **request-local**, bounded
`external_prompt` read through the same unique State Manager Text Box input,
runs the same adapter, and produces an effective read-only prompt preview
merging that real base with active shot cues. No widget/preset is overwritten.
For connected relative `[0-7s]`, `[7-14s]` sources, the adapter now anchors
the timeline to the actual nonzero `chunk_origin_frame`, such as scene frame
17400, without changing an explicitly provided prompt origin.

At runtime a minimal screenshot cue is attached to **each detected shot
range**, e.g. `[0-2.791667s] Match <Picture 4> for camera framing and staging.`
`<Picture 4>` is only used if the exact screenshot has an active allocated
native reference image in that chunk. Native physical H3 compiles these
structured frame intervals on the descriptor's **context-prefixed clock**;
the displayed logical-chunk times are not physical offsets. Original dialogue,
acting precedence (including Mary's deliberate deadpan), and the entire
authored timeline remain unchanged and take priority over composition
evidence. The full VLM report can be edited in the review UI but **is never
dumped into H3**. A separate optional explicit `user.prompt_cue` of at most
180 plain-text characters per shot can express a narrowly scoped visual
override. The deprecated `generate_descriptions` setting no longer emits
VLM paragraph dumps as sampling text.

**Occurrences are not distinct photographs.** Export now counts references
from all shot occurrences of the chosen source within the project selection,
including references retained for disabled shots; a disabled shot is never
made active for conditioning by importing its still. A four-reference review
will export four distinct screenshots (provided all are referenced by shots
inside the selected scope), even if only two enabled shots are visible.
The modal's confirmation reports shot occurrences, unique screenshots and
the number referenced by enabled shot cues.

Explicit Conveyor export sets `scene_plan.conveyor_only=true`.
**Staged, unconnected screenshots are never secretly injected into H3 via a
separate sampler-scoped reference resize path.** They can be inspected in
Image Conveyor but must be connected through the user's existing Media
Resolution pipeline to a free H3 sampler socket to earn a `<Picture N>`
alias. Connected identity inputs still retain their slots. This is distinct
from non-Conveyor use, which retains the existing internal scoped-reference
fallback.

The connected State Manager Text Box remains the authored source of truth.
The composed prompt in Studio is a **derived, read-only** view and does not
mutate the Text Box, saved presets, or their queue/save leases. Manually
pasting a derived physical-clock cue into the original 112-second State
Manager script is **not** equivalent: it risks applying a sample-local
context offset as absolute timeline time. Review the compiled per-chunk
result before generation. No miniMax H3 frame-perfect cut claims are made.


### Automatic shot-cue receipt and responsive review modal (2026-10-22)

The cue panel is **not** gated on pressing an out-of-view
`Preview compiled inputs` button. Completing deterministic analysis,
loading saved analysis, finishing optional VLM interpretation, splitting or
reassigning shots, enabling/disabling a shot, editing a short manual H3 cue,
reverting pending changes, and applying a plan all trigger a debounced,
generation-guarded **read-only preview compilation**. Its results contain
one timed receipt row for each analysed shot interval; if a screenshot is
only staged in Conveyor, the corresponding row explicitly says no active
H3 `<Picture N>` alias exists, rather than remaining blank or inventing a
slot. Full model observations still stay out of the generation prompt.
The detailed compiled prompt panel remains collapsed during automatic
refresh and expands on explicit inspection.

If Studio cannot unambiguously read the connected State Manager Text Box,
the preview now labels its base source `UNVERIFIED`; it never misreports
saved Studio draft text as the authoritative H3 input. The queue-time
State Manager save/lease and native conditioning contract are unchanged.

The dialog's controls use a min-width-zero, three-column responsive grid
(two on medium layouts, one on narrow windows). Its full-length source
selector, contextual notes, option toggles and four analysis actions occupy
bounded cells or dedicated rows. The shot review inputs and long VLM
textareas likewise remain within the dialog instead of overlapping
neighbours. No crop/resample or PNG change was introduced.


## Dynamic per-chunk scene-reference streaming (October 23)

**Image Conveyor's eight persistent shelf outputs are static for a single queued
ComfyUI graph.** Changing a shelf entry in JavaScript while a long
`run_sequence()` is underway does not change a previously computed upstream
IMAGE tensor and cannot safely implement per-chunk swapping. A movie with
hundreds of screenshots also cannot be represented by eight shelf positions.

The new explicit **Activate CHUNK-SCOPED H3 references** action selects
`scene_plan.reference_transport = "streamed"`, saves `conveyor_only = true`,
clears obsolete fixed photo-to-Conveyor mappings, and preserves the actually
connected *persistent identity* socket selection for every logical chunk.
Scene JPEGs are retained in Timeline's immutable, persistent content-addressed
assets. `TimelineInputs.materialize()` loads only the screenshots referenced
by shots **first appearing** in that chunk (or already in progress at the
first scene chunk), deduplicates recurring setups, and prepares
the H3 image latents on demand, and merges those scoped references **after**
the three persistent identity images. `<Picture 4>`, `<Picture 5>`,
etc. therefore resolve on the correct chunk and may be reused for *different
screenshots* in later chunks, without altering Maekar/Mary/Clark references.
Examples: 3 identity + 3 scene JPEGs = 6 in chunk 1; 3 identity + 1
scene JPEG = 4 in chunk 2. Across 100 chunks, an arbitrary number of unique
stored JPEG assets can be selected in the same bounded scene-reference slots.

**H3's limit remains eight images per invocation**, not eight unique files
per movie. Selection is clamped to `min(scene_budget, 8 - actual persistent
image count - manual scoped references)` for each window; surplus candidates
generate warnings. Existing `conveyor_reference_slots` masks and exact
physical counts are respected at runtime. Browser estimates for bundled
IMAGE outputs can be wrong; actual sampler slot sizes and logs remain the
authority, and the compiler enforces the eight-image limit.

This is explicitly **NOT** a dynamic output of the static Media Resolution
node. The stored reference JPEG is never downscaled/re-encoded by extraction
and is not modified by streaming. Native H3's `Max Identity` preparation
still applies any required 32-pixel image-latent grid alignment and the
2048-pixel short-edge ceiling. The current graph's Media Resolution settings
are not automatically imported into this stream. That would require a
separate chunk-aware processing interface or node. The UI describes the
difference clearly; it does not claim that the original Media Resolution node
processed streamed tensors.

The optional **Stage JPEGs in Conveyor shelf** command remains available to
inspect files in the visual shelf when slots are free. During streamed mode,
that action does not modify sampler socket masks, scene-to-Picture mappings or
add a second copy of an already streamed reference. No existing character
shelf slot is overwritten. A long sequence never needs to stage all its
screenshots in the eight-slot Conveyor. The original external State Manager
prompt is still read-only, and the per-chunk picture cues are generated from
the actual selected shots on the correct logical/physical clocks.

A shot crossing the 7-second boundary contributes its still once, in the
chunk where it begins; H3's carried video frames supply the continuation in
the following chunk. Thus three shots beginning in chunk 1 and one *new*
shot beginning in chunk 2 use three and one streamed screenshots, even if
the third shot itself continues into chunk 2. If a user deliberately wants
the same screenshot repeated across a boundary rather than relying on
continuation, its occurrence/reference must be authored in that second
chunk explicitly. This avoids consuming a second chunk slot by accident.

A shot already in progress at the beginning of a later logical chunk gets
a **non-spoken carried-context shot cue** instead of retransmitting its JPEG.
For example, `[0-4.958333s] Continue the previous camera shot from carried
video frames` precedes `[4.958333-7s] Match <Picture 4> for camera framing`
when a new camera shot begins. The cue receipt counts carry-over intervals
separately from actually active Picture instructions. Native physical H3
compilation rebases the timestamps to the context-prefixed clip clock.


## October 23 — count actual shelf references, not linked IMAGE wires

**Eight connected LiteGraph IMAGE inputs do not imply eight populated H3 pictures.**
The source classifier reports a counted Picture only when an Image Conveyor
reference shelf is in persistent mode, has a real reference entry and a
verified active output, or when a unique upstream Conveyor provenance or
known direct image-loader provides concrete evidence. Empty shelf outputs
remain **zero** even if connected through Media Resolution to a sampler
socket. Unresolvable/ambiguous processors are reported separately as
**unverified** and excluded from the explicit per-chunk mask, rather than
silently charging the identity budget. The user can inspect those sockets;
the backend still enforces its actual image cardinality when preparing
the tensor bundle. The exact slot selection is transmitted in
`conveyor_reference_slots` for both native sampling and encoder lookahead.

The previous planner used `connected && !unavailable` as the definition
of an active identity, and the preview used the same assumption, so a
graph with all eight IMAGE connections wired but only three populated
Conveyor shelves incorrectly claimed an eight-picture budget. The
`resolvePersistentReferenceOccupancy` classifier now supplies one
consistent, evidence-based slot list and preview sizing to both the
activation planner and effective prompt compiler. Regression tests
exercise all eight wired Media Resolution paths, three populated
Conveyor shelves, removal of a shelf entry without rewiring, and unknown
intermediaries.

### Review then allocate, *then* generate H3 Picture cues

A newly analysed scene plan defaults to `reference_transport="pending"`.
It carries real source-frame shot boundaries and extracted still identities
but **does not encode scene references or emit `<Picture N>` prompts**.
The review modal shows a plain timestamp/evidence list that explicitly
says each Picture alias is unassigned. Only the separate
**Activate CHUNK-SCOPED H3 references** action binds verified persistent
identity inputs and lazily loaded scene JPEGs to *each individual
chunk*. The read-only effective H3 prompt preview is then recompiled
with the actual selected reference map. Applying detection alone does
not silently activate the images or overwrite the connected State Manager
script. Legacy explicitly configured scoped inputs can still be loaded
by the native sampler under their existing separate contract.

### Eight-lane Media Resolution fanout

For an eight-lane Media Resolution processor the graph inspector can see
all eight upstream Conveyor source candidates for **every** connected
downstream output. This does not mean each output carries all eight images.
The stable one-to-one lane mapping (matching output socket 1 to shelf 1,
etc.) is used only for a recognized MediaResolution processor with all
eight shelf ordinals from one Conveyor; the corresponding shelf must also
have a live, enabled image. Arbitrary multi-input IMAGE mergers are not
assumed to be lane-preserving and remain unverified. This avoids both
the original false "8/8" count and the over-conservative "0/8" count
on the eight-lane compositor topology. Such mappings remain a frontend
inference until the sampler sees the real tensors.

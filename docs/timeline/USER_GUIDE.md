# Continuum Timeline Studio

Timeline Studio edits a media-light project and routes its effective prompts, image references, video guidance and audio to Continuum's native sampler. Retained footage and accepted generated takes form an independent output cut.

## Installation and compatibility

Install the Timeline Studio PR through ComfyUI Patcher, restart ComfyUI and reload the frontend. Existing V3.4 workflows keep their public node identifiers and widget ordering. Install the package requirements in ComfyUI's Python environment if its existing PyAV is older than 17. FFmpeg and FFprobe must be on the server's PATH. Media processing runs on the server's CPU; sampling still requires the normal H3 model, CLIP and VAEs.

The implementation has been exercised with ComfyUI 0.41.0 and frontend 1.55.18, including the actual canvas, workflow serializer and queue. Older frontend versions and every Nodes 2.0 renderer combination have not been qualified. Frontends that do not treat a native modal dialog as modal run ComfyUI's graph undo on Ctrl+Z even while the editor is open; that reloads the graph and closes the editor (observed with frontend 1.47.12, not with 1.55.17). No attention-provider or model allowlist is imposed. Keep Sol, Spectrum, Sage, VDN and LoRA patches upstream of the sampler, as in existing Continuum workflows.

## Quick start

1. Load `examples/timeline/Timeline_Studio_Generate.json`. Choose installed model, CLIP and VAE filenames. The example uses only Core nodes and Continuum.
2. Open **Continuum Timeline Studio**. The project stores the 24 fps output canvas, duration, chunk length and selected render interval. The sampler exposes width and height as ordinary graph-connectable controls. Chunk length uses output frames: 168 means seven seconds; the native supported range is 96–360 frames.
3. Write the global prompt or a Fixed, List or Timeline script. Add timed Prompt region tracks for more detailed scheduling. A double-click in a Prompt track creates a region.
4. Connect the typed timeline to **Continuum Timeline Sampler**. Its two latent lists go through Core video/audio VAE Decode, then **H3 Continuum Assemble V3.4**. Set **Timeline Output = Natural retained timeline (Refinement)**. Connect assembled images/audio and the sampler's take output to **Continuum Timeline Save Take**.
5. Choose **Queue selection** to reuse matching results, or **Reroll selection** to request a fresh alternative; each editor reroll asks native Run Storage to allocate a fresh nonce under its run lock, so restoring an old widget value cannot repeat an earlier alternative. Both queue immutable graph/project snapshots in submission order, after any work already queued, splitting selections longer than 16 chunks into ordered jobs. Rejected takes are never reused as a cached result. Editing the project afterwards does not change those queued inputs.
6. Open **Review takes**, preview an alternative, then accept/select it. The original take files remain available. Queue selection after rejecting the latest matching take requests a new native nonce, including when the graph is run directly. Normal queueing restores a selected rerolled take's stored seed schedule; explicit reroll inputs remain reproducible. The accepted take appears on the Generated takes lane. Generated audio automatically fills intervals without authored final audio; the authored soundtrack has precedence where it covers the accepted span.
7. Export **FFV1 / PCM** for a lossless normalized composition, or **H.264 / AAC** for delivery. Exports are written to ComfyUI's output directory. The Export node also supports ordinary ComfyUI queue execution.

## Original sampler controls

The Timeline sampler shares its native control definitions with **H3 Continuum Sampler V3.4**. Its graph inputs retain the model/CLIP/VAEs, sampler and sigma schedule, first/last images, eight persistent reference-image sockets, Video Reference and Driving Audio. Reference sockets expand as they are connected, as on V3.4. The original V3.4 node remains available.

| Control or feature | Timeline location and behavior |
| --- | --- |
| Chunks / chunk seconds | Editable on the sampler in V3.4 order. These project-bound controls update Studio’s render selection and chunk length, including an open editor and its undo history. Chunk count describes the selected logical chunks; selections longer than 16 are submitted as bounded jobs. Chunk seconds is quantized to the 24 fps output frame grid. Studio’s inspector edits the same data. |
| Width / height | Native integer widgets and connectable INT inputs on the sampler. Unlinked edits update Studio’s canvas too. Connected values are resolved during execution and determine the actual latent dimensions. Accepting the take adopts its resolved canvas while the project still has the frozen source canvas; a later explicit canvas edit is preserved. Old API graphs that omit these inputs retain the project’s dimensions. |
| Sequence Prompt / Prompt Format | Optional sampler STRING input and Auto/Fixed/List/Timeline selector. When connected, this source replaces the Studio global/script/prompt-region text on the project clock. When disconnected, Studio owns those prompts. The native malformed-text fallback remains permissive. The managed prompt sidecar retains native format/provenance handling; its geometry verification is bounded to the native 16-chunk horizon. |
| Base seed / control after generate | Standard Core fixed/increment/decrement/randomize widget. Studio advances seed controls once per submitted batch; every job in that frozen batch shares the base seed and adds its chunk offset. |
| Continuity, continuation method, audio continuity | Sampler controls passed to the same native continuation runtime. The existing native mask/AV requirements still apply. |
| Run Storage / Run Name | Save + Auto Resume (Timeline default), or Off. A name overrides the storage namespace; job and causal-parent suffixes keep separate chains separate. Off samples the entire job without reading or writing the raw chunk cache. Immutable take metadata and continuation sessions are still saved for review and chaining. |
| Regenerate From / Variation Nonce | Sampler inputs; 0 means Auto. Explicit boundaries are one-based within the selected job. Studio rerolls request a new native nonce. With storage Off, the entire job is sampled even when a suffix seed boundary is supplied. |
| Reference Size | Match Output or Max Identity, applied to persistent and scoped stills. Persistent pictures are numbered first; scoped `@aliases` follow them. The combined limit remains eight active pictures. |
| Video Reference Size | Efficient, Balanced or Match Output, applied to both persistent video and scoped guidance. Scoped video replaces persistent Video Reference in the windows where it is active; persistent video is used elsewhere. |
| Driving Audio | Connected audio is authoritative over Studio guide lanes. It is sliced on the absolute project clock, guides native sampling and is returned unchanged for the job's final audio through the assembler plan and appended `driving_audio` output. Authored Final/Both soundtrack clips retain their compositor precedence. |
| Qwen Lookahead | Sampler switch using the native bounded CPU prefetch and descriptor checks. Eligible stable-geometry windows can pre-encode the next prompt and image references. Scoped video/voice windows and motion-dependent unknown geometry stay sequential. |
| Report detail, developer diagnostics, sampling preview | Same Continuum application settings as V3.4; the corresponding API inputs reach native sampling. |
| Exact Refine State | Connecting `refine_state` enables capture, as on V3.4. Every chunk is sampled again so states and masks match all latent outputs. Save + Auto Resume allocates a fresh native variation for that execution. |

Saved Timeline workflows migrate positional widget values to named values before applying the V3.4 visual order. Old seven-widget and expanded layouts preserve their saved seeds, regeneration boundaries, nonces and canvas dimensions. Connected socket slot indices are remapped while retaining link identities. Existing Timeline output indices are preserved; `driving_audio` is appended. The supplied Generate/Recreate/Replace workflows include the sampler controls in this order. Sequence Prompt precedes first/last images; image reference sockets remain contiguous as they expand to eight; Video Reference, Driving Audio and Audio VAE follow them. Job index follows the ordinary sampling controls.

## Importing and editing a movie

**Use Continuum Timeline Desktop on Windows, not an ordinary Chrome tab.**
The desktop wrapper hosts your same ComfyUI frontend and uses Electron's
native Explorer file-path API in an isolated preload. A one-time Windows
portable application is required because browsers deliberately hide the
original full path. See [Windows wrapper setup](../../desktop/timeline-native/README.md).
After launching it, **drag the movie from Windows Explorer** into Timeline
Studio or click Import media. No per-video paths, settings, scans, search
indexing, uploads or movie duplicates. The same applies to Relink.

The isolated desktop bridge privately translates the *selected* Windows drive
path to its exact WSL mount location, e.g. `D:\\Videos\\film.mkv` to
`/mnt/d/Videos/film.mkv`. It submits the path to the same-origin
`/h3_continuum/timeline/link` endpoint, without disclosing the absolute path
to page JavaScript. The backend checks that the source is a regular file under
an allowed mounted root, reads it once to establish its SHA-256 identity, probes
the media, and stores only a tiny persistent path pointer. There is **no
recursive directory traversal**, Windows Search, guessing from file name,
browser movie-byte transfer, or full source movie copy. A previously stored
managed copy with an identical hash is retained instead of silently deleted.
The original must remain at its current path and unchanged for subsequent
playback, sampling and export, including after a ComfyUI restart. Invalid or
missing drives fail explicitly; there is no upload fallback.

Ordinary Chrome **cannot** supply a native path and now displays an explicit
zero-copy desktop requirement; it will not attempt a drive scan. The legacy
`/import` HTTP API still exists solely for external clients and is not
called by Studio drag, file-picker or relink.

Full-stream decoded audio caching is off by default. Set
`H3_TIMELINE_PCM_CACHE_GB` to opt into its disk cache; disabling it can slow
repeated late audio seeks. Bounded preview proxies, generated takes and exports
still consume disk, but **import never duplicates the original movie**.
Set the project duration and render out as intended. Two-hour sources are
decoded in selected video/audio windows, not into a full-film IMAGE batch.

Drag an asset card onto an existing track to place another selection. Set the clip's source in and out in absolute source PTS seconds, and timeline in/out in output frames. Track order is bottom-to-top composition precedence: the last active footage track owns overlapping video. Hidden changes display; muted changes output. Locked tracks resist timeline editing operations.

Select clips with click or Shift-click. Drag to move; drag edge handles to trim; Alt-drag slips the source while retaining placement. Split, Duplicate and Delete operate on the selection. Ripple deletion cuts the same time intervals from every unlocked track, including spanning soundtracks, and closes the cut. Source mapping, markers, selection and project duration follow the cut; locked tracks keep their placement. Ripple extension inserts time in other unlocked lanes, splitting spanning clips around an explicit gap. Snapping includes chunk boundaries, markers and clip edges. Zoom and horizontal scroll expose distant parts of a long film. Filmstrip and waveform requests cover visible windows on fixed tiles, so scrolling only fetches new tiles and revisited ones come from the browser cache.

The inspector edits source ranges, speed, gain, fades, aliases and routing. Positive or negative speed is an explicit retime; audio retiming changes pitch. Audio loops and pitch-preserving time stretch are not inferred. Missing source material uses the declared clip policy: Error, Hold for video, or Silence for audio. New audio clips default to Silence because an audio stream commonly ends a few milliseconds before its video; other clips default to Error. Output gaps are black video and silence. Scaling uses aspect-preserving containment with black borders.

Markers can have names and end frames, making named render regions. Project in/out and every clip interval are half-open `[in,out)`.

| Shortcut | Operation |
| --- | --- |
| Left / Right | Step one output frame |
| Space | Play/pause a bounded composed preview with final audio |
| S | Split at the playhead |
| I / O | Set render in / exclusive render out |
| Delete / Backspace | Delete selected clips |
| Ctrl/Cmd+C, V | Copy/paste clips |
| Ctrl/Cmd+Z, Shift+Z | Undo/redo |
| Ctrl/Cmd+S | Save |

Shortcuts are scoped to the editor and suppressed in text inputs. While the editor is open, its key presses do not reach ComfyUI's graph shortcuts. Closing an editor or changing workflows disposes its listeners and media elements. Duplicating or pasting a Studio node, including pasting one clipboard several times, creates an independent project ID and retains immutable media references. Copied generated footage becomes independent retained footage, without borrowing another project's continuation state.

## Generate, recreate and replace

**Generate** uses the prompts and explicitly scheduled model conditions. A retained movie is not automatically a video reference in this mode.

**Recreate** interprets the selected original footage as visual guidance unless an explicit Video guidance track supplies it. A retained clip can disable this automatic video use in its inspector. Audio remains independently routed. This creates a new interpretation, not pixel-accurate reconstruction.

**Replace** generates selected intervals and composites accepted footage only into those intervals. Original footage remains authoritative outside the replacement. When no first frame is connected to the sampler, the first generated frame of a chain is anchored to the retained frame at the job start; a replacement whose render-in lies on a chunk boundary therefore begins from the frame it replaces. Cuts inside a continuous shot are still not guaranteed seamless. Regenerating native continuation changes the causal state; later generated footage can become stale even if its text is unchanged.

For a movie's audio without its video, use an Audio clip with Guide, Final or Both and disable video guidance for that span. Final-only changes do not participate in diffusion identities. Accepted generated audio fills only sample intervals without authored final audio; moving or muting an authored soundtrack updates this fallback automatically. Guide-only audio is model conditioning; it is not automatically copied into the output. Final means preserved/mixed PCM without model guidance. Reference means voice/style conditioning without final-copy intent. Neither disables audio use. No route promises guaranteed lip synchronization or voice identity.

## References and prompt routing

An Image reference set track scopes reusable still images to intervals. Give an asset or clip a semantic alias, such as `Alice`, and write `@Alice`. Each invocation resolves active images into contiguous public `<Picture N>` tags. First/last presentation is then remapped by the existing Qwen encoder; the actual mapping is recorded in the input inspector. The limit is eight active reference images per invocation, rather than eight assets per project. These are appearance references, not guaranteed frame-local keyframes.

Video guidance selects only the source intervals intersecting that logical chunk. A seven-second source interval remains seven seconds even when the native reference tensor is padded to a `17k+5` length. The added reference frames explicitly hold the last selected frame. No neighboring source window is used as padding. When guidance covers only part of a chunk, uncovered frames hold the nearest guide frame and the chunk reports a warning in Inspect inputs and the sampler status.

Fixed/List/Timeline parsing retains the existing permissive fallback. Exact outer chunk headers route only their own bodies. Prompt tracks add timed regions; Override replaces inherited script text inside its own span while retaining the global prompt. Each chunk becomes an outer section on its exact chunk boundary and its timed regions become a strict inner partition, so the native physical compiler places them on each invocation's own clock, including native context, without importing neighboring chunks. An `@alias` without an active image in a chunk is passed through unchanged and reported as a warning. Inspect inputs shows the logical schedule; sampled takes add the actual descriptor, compiled text, Qwen mapping, source selections and reuse flags.

## Native time and continuation

The editor distinguishes source PTS, output frames, logical chunk windows and physical H3 windows. Native sample boundaries need not fall at seven-second edges. The first seven-second sample normally occupies 175 physical frames. Continuation adds protected context and native rounding; that context is never counted as fresh output. Native geometry remains owned by Continuum. First/last-frame inputs anchor the boundaries of the selected generation chain.

The compiler includes the compatible accepted prefix and its bounded causal job suffix when rerendering an internal selected chunk; only the chosen output interval is composited. Run Storage authenticates it against effective media, text, model/CLIP/VAE/sampler identities and the sigma schedule. Changing chunk N’s visual conditions invalidates its causal suffix. Guide-audio edits invalidate every window of a job whose actual encoded guide range contains the edit, until narrower Audio VAE dependencies and phase behavior are qualified. The range follows cumulative native output placement across jobs. Takes using the older margin-based guide identity regenerate once. Edits elsewhere in the project leave that job's takes valid. A different accepted upstream take also changes downstream lineage. Valid preceding output is reusable; stale generated footage is rejected by composition rather than advertised as valid.

Jobs retain natural output while transferring native state. Their recorded physical output ranges determine placement across job boundaries. Only the selected intersection is placed in the cut. Use Natural retained timeline at the assembler: cropping each job independently would discard material needed to preserve the native chain. Timeline Studio disables terminal FL2VA grouping for its scoped jobs and uses separate invocations; legacy V3.4 grouping remains unchanged.

## Persistence, relinking and recovery

Save persists the project with an optimistic revision check. Reload restores its last saved manifest. Workflow JSON stores the same media-light project, never full video or latent tensors. Scroll, zoom, playhead and selection are saved in the node's properties; they are not prompt inputs, so they never invalidate cached execution. Saving concurrently from another editor produces a revision diagnostic instead of silently overwriting it.

The asset library's Relink action imports replacement content and updates clip references. Changed content gets a new content ID, invalidating affected conditioning. Missing or externally altered registered files must be relinked before sampling. Keep the `input/h3_timeline_studio` directory when moving an installation; keep Continuum Run Storage, session files and outputs to retain generation history.

With Run Storage enabled, sampling commits each completed chunk through native Run Storage. A cancelled or failed job keeps its valid completed prefix. Take specifications and their native sessions are immutable; review selection is project state. Exact Refine State forces sampling from the first job chunk so every emitted state matches its latent. It uses the existing `H3_CONTINUUM_REFINE_STATE` contract; optional downstream algorithms remain external.

## Export and performance policies

Composition decodes one output-second block at a time. Lossless export preserves normalized 8-bit SDR RGB pixels and float PCM after declared frame sampling, scaling, channel mapping, gain and mixing. It does not copy the source compressed bitstream. H.264/AAC is re-encoded. Mono audio is duplicated at unchanged amplitude to the stereo mix. The default output rate follows the first audio asset, or 48 kHz without one. Peaks above full scale are clipped after summation; crossfades use overlapping clips and their fades.

Audio extraction decodes a registered source's audio stream once per sample rate, sequentially from its first sample, into `input/h3_timeline_studio/pcm_cache`, and serves every later window as an exact absolute sample-index slice. This avoids packet-PTS seek-rounding shifts and keeps windows, previews and export linear in their duration. The first use of a long source pays one full audio decode. The cache keeps completed files under a 16 GiB budget; streams larger than the budget use sequential prefix decoding that writes and reads only the selected window, so repeated late windows cost additional decode time; `H3_TIMELINE_PCM_CACHE_GB` changes that limit, and the directory can be deleted at any time. Full-job guide audio, with bounded right context for physical overrun, is encoded through the existing H3 Audio VAE path. Uncovered guide-mix samples are explicitly silence and the input manifest records the encode/sample scope; jobs are bounded to 16 chunks. Reference-image encodes use a 128 MiB CPU cache. Video-reference preparation is limited to its selected invocation and the selected reference canvas size.

Composition previews render at most 30 seconds at a time and use the actual final mix. Source-window proxies (up to 4 GiB) and composition previews (up to 2 GiB) are bounded on disk, oldest first. HDR tone mapping, source-stream pass-through, subtitles, OTIO, freeform masks/transitions, dock integration, internal native keyframes and per-span hot LoRA switching are not provided by this version. No inactive controls advertise them.

## Troubleshooting and qualification

- **Save Take asks for a natural timeline:** select Natural retained timeline at the assembler; reconnect the take from the matching sampler execution.
- **Stale generated footage on export:** rerender the dependent suffix, choose a compatible alternative, or remove the stale generated region to reveal retained original footage.
- **Audio guide fails:** provide the H3 Audio VAE and check the clip's source range/padding. Guide-free generated-audio continuation still requires native 39-frame AV context; guide-only continuation can use supported 5/22-frame modes.
- **No source video in a chunk:** inspect Video guidance tracks or Recreate's per-clip disable setting. Audio-only use does not enable video.
- **Slow first waveform, preview or guide on a long source:** this is the one-time audio decode into the PCM cache described above, not GPU model loading.
- **Same project edited elsewhere:** reload the newer server revision or duplicate the Studio node for an independent project.

CPU, media, HTTP, serializer and browser evidence is listed in `VALIDATION.md`. Trained-H3 output quality, audiovisual phase across native and multi-job boundaries, accelerator combinations, hot-run overhead and perceptual retake quality require the GPU acceptance matrix. They must not be inferred from passing unit tests.

## Editor view

![Timeline Studio editing real source media](editor.png)

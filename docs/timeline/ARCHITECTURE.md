# Timeline Studio developer contract

## Ownership and schema

`timeline/model.py` validates `h3_continuum_timeline_project_v1` and compiles logical windows without loading models or decoding media. `Timeline Studio` emits a request-local deep copy as `H3_CONTINUUM_TIMELINE_PROJECT`. `timeline/runtime.py` supplies invocation-local conditions to `v2/sequence.py` after its native geometry is resolved. All latent shapes, masks, context extraction, RoPE, model cloning and CPU chunk retention remain in Continuum.

The JSON root contains `schema`, `project_id`, rational `fps` (currently 24), integer `duration_frames`, `chunk_frames`, output dimensions, operation, assets, tracks, prompts, selection, markers and a legacy editor-state field (`ui`) that compilation ignores; current editors keep view state in node properties. Assets use registered SHA256 IDs. Clips contain `[start,end)` output frames, absolute rational `source_in`, rational speed, padding and independent audio role. Source time is `source_in + (frame-start)*speed/24`. Integer sample boundaries derive from absolute rational project time, never repeated per-chunk rounded durations.

Track roles are retained, generated, video_guide, image_reference, audio and prompt. Track order is semantic for overlapping composition; lock/visibility/name are not sampling inputs. Audio roles are neither, guide, final, both and reference. New schemas require explicit migration; unknown versions do not guess at temporal semantics.

`accepted_windows` records selected take IDs, logical chunk spans and parents. It allows a sub-selection to include an authenticated accepted prefix for reuse. The selected output interval remains unchanged. Immutable take records carry input hashes, native revision/session references, physical output intervals, chunk file identities, compiled model-input manifests and the project snapshot. Renditions are separate immutable registered media. Accepted choices belong to the revisioned project.

## Identities and lifetime

Generation identities include effective compiled text and its timed segments, ordered active images, selected video intervals and guide/reference audio inputs. Final audio has a separate identity. Actual reference hashes and presentation mappings enter the physical descriptor; the legacy encoder cache also includes that presentation for Timeline Studio requests. No UI/layout metadata is salted into sampling hashes.

Native Run Storage provides model, encoder, VAE, sampler, sigma and per-chunk authentication, atomic chunk commits, locks and interruption recovery. Timeline's optional selected revision pins the causal alternative; existing callers retain ordinary longest-valid-prefix selection. The full guide-encode identity remains global within a bounded native job, as in V3.4. Each job hashes the guide clips over its encoded range plus a 64-frame margin for native drift, and records that scope on its takes; staleness is recomputed over the recorded scope, and records without one keep the project-wide rule. A narrower Audio VAE dependency/phase contract is unproven. Final-only audio is excluded. Scheduled visual references remain per-window.

Accepted-prefix comparisons include stored native chunk file identities. A different take may share an exact reusable prefix but have an incompatible suffix. Export checks only the causally relevant portion of a retained generated clip and its ancestors. It does not conflate a valid selectable alternative with validity under the currently accepted continuation lineage.

Reference media is materialized at the actual invocation, and conditioning caches are invocation-local. A bounded CPU image cache avoids re-encoding unchanged still references. Completed chunk latents remain CPU-resident. The timeline UI never loads or patches a GPU model. Exact refinement capture is opt-in and aligned with all sampled chunks.

## Media and server boundary

`timeline/media.py` owns streamed, immutable asset registration, PTS-based frame selection, exact sample-grid PCM windows backed by a bounded whole-stream decode cache, mixing and optimistic atomic project persistence. Probe results are memoized by file identity. Browser-controlled paths are never opened. Playlist demuxers are excluded, and registered IDs are validated before lookup. Asset content is checked before sampling. Large-file upload streams in bounded chunks directly into the asset store, hashed as it arrives. Route-side media work uses request-local cancellation and never reads ComfyUI's execution interrupt flag, which reading would clear.

`timeline/compose.py` performs aspect-preserving nearest-PTS normalization and ordered retained/generated composition in one-second blocks. The lossless route is FFV1 plus float PCM; the delivery route is H.264/AAC. It is not compressed stream copy. Source media stays authoritative outside selected accepted replacements. Bounded composition previews use this same video/audio path.

`timeline/routes.py` registers same-origin project, import, media, thumbnail, waveform, proxy, input compilation, take-selection, composition-preview and cancellable export endpoints. The current server instance owns its export tasks; projects and takes survive restart on disk. `timeline/nodes.py` is a UI facade over those modules and native Continuum.

## Frontend

`web/timeline/core.js` contains pure editing operations and undo history. `editor.js` provides the resizable editor. The ruler, clip filmstrips and waveform requests are limited to visible windows. `timeline_studio.js` registers the compact node and its lifecycle. Shortcuts are editor-scoped, typing is excluded, editor key events stop at the dialog, workflow switching disposes editors, cloning and repeated pastes fork identity, and graph serialization carries the project widget. Editor view state is stored in node properties rather than the project widget, so it is never part of the prompt or its cache signature.

Each queue action has an immutable batch identity. Ordered jobs choose the predecessor from that batch, so interleaved projects or batches do not borrow the latest unrelated take. Native storage namespaces use parent chunk payload identity, allowing an equivalent reused parent to retain downstream caches.

A queue action freezes the complete API graph once and clones that snapshot per bounded job, appending jobs to the queue in order (front-of-queue submission would run later jobs first). It does not repeatedly serialize a changing live graph. Save responses preserve newer local edits rather than restoring an older submitted snapshot.

## Prior-art audit and licensing

The implementation is original MIT code. No code from the following GPL projects was copied: Sonder Editor, comfyui-obvpm-timeline, MiniMaxH3 Director, LTXDirector or VideoHelperSuite. Their timeline lanes, take lineage, preview, trim and audio handling were inspected as interaction/contract precedents. NKD Preview Tools is MIT; Capricorncd Timeline and OpenTimelineIO are Apache-2.0. Their source selection, waveform, rational-time and clip/parent-range conventions were inspected; no implementation was imported.

Primary sources: [Sonder](https://github.com/SonderSaid/ComfyUI-Sonder-Editor), [Capricorncd](https://github.com/capricorncd/ComfyUI-Capricorncd-Timeline), [obvpm](https://github.com/chanon/comfyui-obvpm-timeline), [NKD](https://github.com/Nekodificador/ComfyUI-NKD-Preview-Tools), [H3 Director](https://github.com/seesee75-commits/ComfyUI-MiniMaxH3-Director), [LTXDirector](https://github.com/comfy-nodes/ComfyUI-LTXDirector), [VHS](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite), [OTIO](https://github.com/AcademySoftwareFoundation/OpenTimelineIO).

Flow's `H3_CONTINUUM_REFINE_STATE` and assembly-plan interfaces remain available. Sol, Spectrum, VDN and LoRA loaders continue to own their model/attention implementations. Timeline Studio does not mutate global ComfyUI classes or accelerator layout tensors.

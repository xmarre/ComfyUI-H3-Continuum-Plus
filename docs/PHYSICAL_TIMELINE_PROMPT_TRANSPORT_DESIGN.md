# Physical timeline prompt transport design

Status: implementation specification for an experimental conditioning correction. Production promotion requires the decoded-media gate below. No production fix is included in this design change.

## 1. Verdict and evidence boundary

Continuum has two verified information-transport defects relevant to timed prompts:

1. Timeline parsing selects one maximum-overlap section per nominal chunk and discards the remaining sections and their ranges.
2. Ordinary sampling selects that nominal chunk's text before resolving the physical continuation window. The physical window includes earlier context and native-grid rounding.

Preserve the source timeline, resolve actual geometry first, and compile one chronological, physical-local text description for each H3 invocation. Keep all persistent references and PR #20 presentation rules. This corrects which instructions reach the model; it does not establish that H3 follows textual timestamps accurately.

**There is no structural time-local text/reference router in the audited Core H3 path.** Textual time labels are ordinary Qwen input. The supported candidate is a single Qwen conditioning sequence containing chronological text with local ranges. Do not describe it as enforced per-frame conditioning, and do not invent attention masks, blend independently encoded prompts, or add sampler passes to make that claim true. If bounded testing finds local timing guidance ineffective, this design's renderer is not a proven production solution; retain legacy execution and document that model-level temporal control remains unresolved.

The reported reference oscillation is consistent with the transport defects but not causally proved by source inspection. Persistent-reference competition, imperfect learned temporal adherence, and acceleration-dependent quality remain possible contributors.

### Audited source state

Live GitHub state fetched on 2026-09-13:

| Source | State |
|---|---|
| Continuum-Plus main | `a5b8943844594545301b20d01af5d9e3fa38ae29` |
| PR #20 base | `bf25353d8bec44afea22c89717c4301ce13c2036` |
| PR #20 head / branch | `ccf50cddf83124707b866e4df3956e85d41dda4b` / `fix/first-frame-continuation-presentation` |
| PR #20 status | Open draft, one commit, mergeable; no submitted reviews or inline comments; CodeRabbit review skipped |
| PR #20 checks | Python 3.10, 3.11, 3.12, 3.13 and publisher-toolchain succeeded |
| Effective main + PR20 tree | `236c1d2b8d720a7e407656d1c4af1b134367ca20` |
| Effective-tree checkpoint | `205946859368869344af92c454e4210f783fd04b` |
| Investigation mirror | `mirror/physical-timeline-design-20260913` |
| Findings checkpoint | `09210370e2afe7096fa27b4e62e46485942b8de4` |
| ComfyUI master | `d43a5fa20c8547ff42d13232f589a06536c42b97` |
| Core PR #15375 | Merged as `ff6c8a8af144fc9e9e7bc436b1b202f9316848d8` |
| Flow diagnostic PR #32 | Open; head `1c8689c68ebb0cf2cf87a7adc7e898c058bf9c5c`, base `f6a940fdc2fb6d31249a487aff5e4a1297151e9c` |

Main differs from PR20's base only in README/README_JA repository naming and pyproject repository URL. PR20 changes CHANGELOG, hybrid-conditioning documentation, hybrid tests, and v2/sequence.py; the changes do not overlap. The effective tree was constructed from main's tree plus the exact four PR20 blobs. Neither main nor PR20 was rewritten.

Sources: [Continuum main](https://github.com/xmarre/ComfyUI-H3-Continuum-Plus/tree/a5b8943844594545301b20d01af5d9e3fa38ae29), [PR20](https://github.com/xmarre/ComfyUI-H3-Continuum-Plus/pull/20), [effective snapshot](https://github.com/xmarre/ComfyUI-H3-Continuum-Plus/tree/205946859368869344af92c454e4210f783fd04b), [Core](https://github.com/Comfy-Org/ComfyUI/tree/d43a5fa20c8547ff42d13232f589a06536c42b97), [Core masking PR](https://github.com/Comfy-Org/ComfyUI/pull/15375), [runtime evidence summary](https://github.com/xmarre/MiniMax-H3-Flow-Aligned-Regenerate/pull/32).

The task's 00417 account and Flow PR32 description agree: native target-grid exact continuation, one sampler invocation, zero history boundaries, exact target inputs forwarded, no external mixed-grid calls, positive native VDN local/rectangular Sol calls, cute_sm120, Spectrum active, and continued semantic oscillation. These are reported runtime observations. The original workflow JSON, reference images, metrics and decoded video were not available in this investigation; the exact production parser mode/text was therefore not independently reconstructed. Current public Core is not evidence of the installed Core SHA. Those are explicit validation prerequisites, not reasons to invent their contents.

## 2. Verified execution contracts

### Prompt and facade path

V3's sampler forwards sequence_prompt through the V2 sampler, builds a prompt plan, applies external overrides and calls run_sequence. build_sampler_prompt_plan gives non-None sequence_prompt precedence over a connected Prompt Plan. Connected schema-1 plans are otherwise resized by repeating/truncating their existing logical prompts; changing chunk duration cannot recover discarded sections.

make_prompt_plan emits schema 1: mode, chunk count/duration, prompts, hashes and diagnostics. _parse_timeline_sections initially knows ranges, source order and header kind, but prepends the preamble to every section body. _parse_timeline then chooses a [Chunk N] section if present; otherwise maximum overlap wins, ties select earlier source order. A wholly uncovered chunk repeats the prior selected prompt, or uses the first usable source section for the initial gap. Malformed syntax falls back to Fixed; prompt content must not become an execution gate.

apply_prompt_overrides replaces selected logical texts, changes mode to List and loses Timeline provenance in that view. A new implementation must preserve override provenance separately.

### Physical sample and conditioning

run_sequence currently chooses prompts[sequence_index] and obtains Qwen conditioning before choosing context and extension shape. PR20 scopes First Frame to previous_state is None, Last Frame to final invocation, and caches by (prompt, include_first, include_last). It also omits the original frame-0 keyframe on continuation and applies that rule to terminal physical samples.

Native Masked copies the previous latent tail into a uniquely owned target, sets a binary zero mask on the prefix, preserves source tensors, removes competing prefix keyframes, and preserves minimax_refs. Guide adds marked context references and maps their RoPE positions onto the target prefix via layout_adapter; its target prefix is generated and later trimmed, not exact-protected.

sample_chunk uses the ordinary ComfyUI guider/noise/mask/sampler route. clone_model_for_chunk preserves wrapper integration and adds the existing Continuum actual-prefix hint using fresh model_options/transformer_options dictionaries. Conditioning/refine capture must continue receiving the actual physical conditioning object.

### Core and learned behavior

In comfy/model_base.py, MiniMaxH3.extra_conds projects/refines the Qwen conditioning once per sampling, builds PackedLayout, and passes keyframes, references and modality tags in minimax_payload. In the audited path, minimax_frame_count is Continuum helper metadata; Core geometry comes from latent_shapes and resolved keyframe indices. Merely changing that metadata cannot rebase text.

Core _token_grid_masks pools video masks with amax onto spatial DiT patches, pools audio channels, then rounds strengths upward to 1/256. This current behavior is more general than PR15375's original binary description. _denoise_mask_values/_denoise_mask_conds expose model masks. scale_latent_inpaint injects protected video at the conditioning noise level and handles packed audio scaling. KSamplerX0Inpaint restores caller latent values outside the mask. Exact binary prefix masks remain exact under this pooling.

MiniMaxH3Model._forward assigns per-row timesteps, embeds all target rows, and includes them in the packed transformer. forward scales output velocities by masks. Dense Attention.forward calls optimized_attention with mask=None; denoise masks do not exclude tokens from attention. External attention implementations may select connections, but this design must not change their ownership.

PackedLayout places text, keyframes/references and targets into a joint sequence. Keyframes have structural resolved_frame_index positions. Persistent references occupy reference spans; they have no source-timeline interval router. Text positions are token positions, not parsed seconds. Reference-image presentation and DiT reference blocks are distinct channels.

MiniMaxH3Tokenizer uses Qwen3VL-32B through ComfyUI's CLIP interface; this is not a second ordinary CLIP embedding beside Qwen. It presents minimax_ref_items with independent image/audio/video counters and then tokenizes the prompt with weights disabled. Video timestamps are also presentation text. No [0-5s] parser or mapping from text ranges to target rows exists in these modules. encode_from_tokens_scheduled is not evidence of spatial/temporal video routing; sampling-step schedules and video time are different axes.

reference.py prepends First/Last presentation images before persistent references and shifts public Picture tags by their count exactly once. Reference Image N must keep referring to the same public socket/content even when Qwen's internal number changes. Reference Audio numbering is independent. Driving Audio remains a target audio authority, not a numbered reference.

## 3. Failure model and reproduced geometry

Use frame-edge intervals [start,end), with actual integer frame counts. Let R be retained generated frames before an invocation; C its trimmed context; F its total physical frames; fps=24. Then:

- global physical window: [R-C, R-C+F);
- overlap/context: [R-C,R);
- retained contribution: [R,R+F-C);
- next retained count: R'=R+F-C.

Only Native Masked's overlap is an exact protected target interval. Guide's overlap is guided/regenerated.

For ordinary invocation i (zero-based), preserve existing geometry:
- Initial without State: F=align_frame_count_up(round(chunk_seconds*24)), C=0.
- Continuation: choose C from actual accepted State and existing audio policy.
- requested_new=max(1, round((i+1)*chunk_seconds*24)-R).
- Non-final uses make_extension_shape(C, requested_new/24); final uses make_extension_shape_at_least(C, requested_new).
- Never substitute i*chunk_seconds for R-C.

Two 7-second chunks, Native Masked Auto with generated audio:

| Quantity | Frames | Seconds |
|---|---:|---:|
| Logical boundary | 168 | 7 |
| Initial physical end / R | 175 | 7.2916667 |
| Context C | 39 | 1.625 |
| Second start R-C | 136 | 5.6666667 |
| Requested new frames | 161 | 6.7083333 |
| Second F: align_up(39+161) | 209 | 8.7083333 |
| Second end | 345 | 14.375 |
| Requested final end | 336 | 14 |
| Final overrun | 9 | 0.375 |

39 frames correspond exactly to 65 audio latent steps. 5/22 contexts are valid for native video-only or Driving Audio paths; they are not valid exact generated-AV prefix boundaries under the current contract.

The protected window is [136,175); the suffix begins at 175, not at the logical 168 boundary. A desired transition at global 8 seconds appears at physical-local 7/3 seconds. Lizard's [6,8) interval intersects at local [1/3,7/3), iguana [8,10) at [7/3,13/3), turtle [10,12) at [13/3,19/3), and blue jay [12,14) at [19/3,25/3). The final state extends through 209/24 locally for physical overrun. The preceding [5.6667,6) source interval must also be included; its actual text must come from the original prompt.

A bounded execution of current temporal.py and prompts.py reproduced these frame counts. A deliberately synthetic seven-section, two-seconds-per-section prompt collapsed to only Picture 1 and Picture 5 for two logical 7-second chunks. This proves the parser's information loss; it is not a reconstruction of 00417.

A four-chunk 7-second probe produced initial R=175 and later retained endpoints 328,498,685. Contexts 5/22/39 change physical starts and total sizes, even when retained endpoints coincide. Auto context must be resolved from each actual preceding State; do not assume this equality universally.

Last Frame remains at local F-1. Exact assembly may copy the final physical frame to output frame target_frames-1 while removing pre-tail overrun. Therefore physical model time and final presentation time are not a globally identical mapping when Last Frame preservation is active. Compile against model time; keep assembly behavior unchanged.

## 4. Required invariants

- Preserve PR20 First Frame/Qwen/keyframe/cache behavior, including continuation from an initial State and terminal merged FL2VA.
- Preserve public Reference Image 1..8 identity and order, all persistent DiT reference blocks, Reference Audio/Video, and final Last Frame.
- Preserve native geometry, exact source prefix and caller noise/mask values; no context decode/re-encode; source tensors read-only.
- Preserve sampler, sigmas, seeds/nonces, H3 invocation count, actual-prefix hint, Spectrum history/forecast topology and VDN/Sol/DiffAid/Untwist ownership.
- More text can change packed text length and attention cost. Measure token count, packed spans and runtime; promise no extra H3 evaluations, not identical FLOPs or guaranteed speed.
- Keep accepted latents on CPU, run-scoped caches, public wrapper composition, position_ids identity, atomic safetensors/JSON storage.
- Preserve V1 identifiers, compact V3.4 widget layout and existing serialized inputs. Prompt mistakes remain nonblocking Fixed fallback. No new model allowlists.
- No audio phase, VAE decoder, seam, waveform or assembly corrections in this change.

## 5. Proposed data model

Introduce a pure module v2/physical_prompts.py; keep geometry helpers shared with current temporal.py.

Prompt Plan schema 2 retains schema-1 logical prompts/hashes as a compatibility/reporting view and adds source:
- kind: fixed, list, timeline, or legacy_logical;
- original_text (exact input), requested/resolved parser mode;
- preamble separately from section bodies;
- sections: stable source ordinal, kind (time/chunk), exact decimal start/end as rational strings or chunk index, body;
- overrides: logical index to opaque replacement body, with explicit precedence;
- source_digest: canonical SHA256 of semantic fields, excluding diagnostic line numbers.

Do not attempt to recover original sections from selected prompt strings. Migrate schema 1 to legacy_logical with an explicit diagnostic; never claim recovered timing. Rebuild schema-2 logical views from its source when adapting chunk settings. Input precedence remains unchanged.

PhysicalSampleDescriptor v1:
- physical group identity and covered logical indices;
- fps numerator/denominator; R, C, F and global start/end as frame edges;
- target duration; continuation method; initial-State origin policy;
- exact protected interval or null; guided overlap interval or null; retained suffix;
- include_first/include_last and last_keyframe_index;
- terminal contract/split identity;
- presentation contract identity, including source-video selection if applicable.

Compiler identity: physical_timeline_text_v1. Legacy identity: legacy_nominal_v1. Renderer and interval-resolution policy are part of the compiler version. Any behavior change affecting emitted text increments it.

CompiledPhysicalPrompt v1:
- public-numbered text, text SHA256;
- ordered contributing source intervals and physical-local rational intersections;
- fallback/overrun status;
- descriptor digest and presentation digest;
- physical_conditioning_hash over compiler identity, descriptor semantic fields, emitted text, ordered presentation contract, and terminal identity.

Keep entry.prompt and its existing 64-character prompt_hash as logical compatibility fields. Store physical metadata under entry.plan.physical_prompt; update validators/serializers explicitly. Do not put a prefixed transport hash into the existing 64-character field.

## 6. Compilation algorithm and mode policy

All interval math uses exact rational source times and integer frame edges, not accumulated rounded float seconds. Reproduce existing Python round semantics only when calling existing geometry planning. Half-open intervals share a boundary without double inclusion.

Pseudocode:

    source = normalize_or_migrate(plan)      # no latent/model operations
    state = accepted_previous_state()
    geometry = resolve_existing_geometry(state, retained_frames, logical_index)
    descriptor = describe(geometry, presentation_flags, terminal_contract)
    segments = resolve_source_over_window(source, descriptor)
    compiled = render_once(segments, descriptor, compiler_version)
    conditioning = get_or_encode(compiled, presentation_contract)
    attach_current_keyframes_refs_and_masks(conditioning, geometry)
    sample_once_using_existing_path()
    persist_entry_with_physical_identity()

Resolve geometry/context once per invocation and reuse the descriptor for conditioning, sampling, storage and diagnostics. Do not independently recompute it with different rounding. Reuse validation may compute descriptors from already loaded candidate states without sampling.

### Valid Timeline

1. Convert [Chunk N] bodies to logical intervals [(N-1)d,Nd), with priority over timed sections in that interval. External overrides have highest priority and are opaque replacements for that logical interval.
2. Partition the requested [0,D) source domain at every time/chunk/override boundary. For each atomic interval, choose the active override, then explicit chunk body, then active timed section.
3. For overlapping timed sections choose earliest source ordinal, emit a diagnostic, and preserve all original AST entries. This is a new deterministic conflict policy for the physical compiler; legacy maximum-overlap selection remains only in the legacy view. Equal nominal overlap between disjoint sections no longer discards either.
4. Uncovered intervals hold the last resolved body. A leading gap uses the earliest chronological usable body, ties by source order, with a diagnostic. This deliberately refines whole-chunk fallback into interval fallback; legacy execution retains its old fallback. No valid body or malformed syntax takes the existing Fixed fallback.
5. Clip the resolved schedule to [S,E), S=(R-C)/fps, E=(R-C+F)/fps. Rebase each endpoint by subtracting S. Do not shift the transition to the protected-prefix boundary.
6. For E>D, hold the body active immediately before D. Do not pull in out-of-request future sections to fill physical overrun. Retain their AST for later plan adaptation.
7. An externally supplied initial State with R=0 gives S=-C/fps. This is context before the new sequence origin; its source semantics are unknown. Extend the first resolved body as textual lead-in, report unknown prior semantics, and do not infer timeline origin from State.clip_index. Exact copied context remains authoritative. Explicit Session continuation uses its known retained cursor.
8. Render preamble once, then chronological blocks using [start-ends] and unchanged section bodies. Format local rational seconds deterministically to six decimal places, trimming trailing zeros; preserve exact fractions in metadata and do not emit zero-width blocks. Sub-microsecond intervals coalesce only for rendering, retaining provenance and a warning. Join adjacent equal bodies; retain source dependencies in metadata. Never recursively parse body text or rewrite its embedded prose timestamps.
9. If one identical body covers the whole window, emit preamble plus body without unnecessary range scaffolding. No speculative instruction rewriting or new negative prompts.
10. Shift public Picture tags only in reference.encode_reference_prompt, once, after compilation. Metadata retains public numbering.

This is ordinary textual conditioning. If the model ignores the labels, there is no supported structural equivalent in the audited API that satisfies the current no-attention-redesign/no-extra-evaluation scope.

### Fixed, List and opaque legacy plans

Fixed passes its text through the existing normalization/fallback path, without timestamp interpretation. Ordinary List and migrated legacy_logical retain per-invocation prompt selection, including repeat-last/truncation. They are per-clip instructions, not newly declared global timelines. Do not silently change them to timed scripts. Schema-2 Timeline with overrides keeps its source Timeline semantics instead of becoming an information-poor List.

Explicit [Chunk N] inside valid Timeline is a timeline interval under the new compiler. Users selecting Fixed continue to receive opaque Fixed semantics even if their prose contains brackets.

### Terminal merging

Retain existing merge eligibility, physical sizes, split slices, seed contract and atomic reuse of the pair. The descriptor represents one actual invocation and one physical_conditioning_hash shared by both logical entries. For Timeline, the general compiler replaces _terminal_pair_prompt. For opaque List/legacy inputs preserve its current paired text behavior through a legacy renderer adapter; identical texts remain shared. Do not claim nominal [0-5s]/[5-10s] becomes physically correct merely by calling the old helper from a new module. A future change to opaque-list timing requires its own explicit compatibility/media decision.

### Timeline Video versus Reference Video

Reference Video is a persistent reference asset; leave its sampling and numbering unchanged.

Timeline Video currently slices by sequence_index*chunk_seconds, resamples to an initial native chunk size and gives Qwen local timestamps. It has the same selection mismatch, but remains a reference block, not structurally time-routed target conditioning. Add a descriptor-based source-window adapter for the experimental physical compiler: sample source video at global frame-edge times S+j/fps for j in [0,F), using existing source decoding and resize rules; clamp unavailable leading/trailing source times to source endpoints with diagnostics. Encode one source-video window per invocation, never the protected generated latent. Use actual sampled local times for Qwen frames; preserve <Video 1>. Hash the selection grid, source contract and processed presentation identity. Respect valid H3 reference geometry without stretching a nominal slice across the physical window. Keep legacy extraction available under legacy compiler identity. Terminal merge remains disabled with Timeline Video. This adapter has its own matched gate because it changes reference content/size and may cost more VAE work; it must not be silently enabled by a text-only success.

## 7. Run Storage, cache and compatibility

Current versions: Prompt Plan 1; Session 1; Run Storage 2; sampling contract 5; native mask 1. Eager RunStorageController.prepare compares nominal chunk hashes; _preserved_prefix repeats logical/method checks. Both miss changes to discarded source sections. _load_entry substitutes current logical prompt text. Logical hashes alone cannot authenticate actual past conditioning.

Implement two-phase acceptance:
1. Retain existing static model/reference/sampler/noise/nonce checks to find candidate prefixes.
2. Before accepting each candidate, reconstruct its descriptor from the already accepted prefix and actual prior State; compile current expected text/presentation identity; compare against stored physical metadata.
3. Stop at first mismatch and regenerate the suffix. Never trust stored S/C/F as expected values without recomputing. Never accept a later entry after a rejected predecessor.
4. For terminal pairs validate both halves and one shared physical identity atomically. Last Frame/prompt changes affecting either half invalidate the pair.
5. Apply this common predicate in Run Storage and explicit Session reuse before returning a validated prefix. On resumed captures, rebuild actual compiled conditioning when required by refine consumers.
6. On commit persist physical metadata and compare descriptor to actual sampling plan. The same contract must be used for newly sampled entries, not reconstructed from logical prompt text later.

Revision identity and nonce lineage must include source_digest and compiler policy at the top level; avoid adding them to the global sampling hash, which would invalidate every prefix for any later timeline edit. Keep existing global model/asset checks. Dynamic per-entry physical verification catches relevant source changes even when legacy logical hashes match. Candidate lookup must not bypass it on exact revision-ID hits. Changing the compiler version rejects affected physical entries.

Version boundaries:
- Prompt Plan 2, with explicit schema-1 migration.
- Session 2 and Run Storage 3 for entries using the new physical contract. Old readers must reject these schemas rather than ignore new mandatory reuse semantics.
- New reader supports legacy Session 1 / Storage 2 candidates through a conservative compatibility adapter. Do not rewrite old files in place.
- Keep sampling contract 5 and native mask contract 1: sampler/mask semantics do not change. Compiler identity is separate. If implementation discovers another sampling-semantic change, justify a separate version change.
- State 1, geometric Plan 1 and Assembly Plan 1 remain unless validation reveals their parsers cannot preserve optional transport metadata. Never make transport metadata alter latent geometry or assembly hash interpretation.
- Preserve existing workflow widget ordering/node IDs. Use a defaulted internal keyword/execution scope for legacy versus experimental compiler selection during validation, without adding a compact-UI widget. The implementation must provide an exact reversible activation command or bounded diagnostic entry point.

Legacy reuse is allowed only when equivalence can be established: same emitted bytes, presentation roles/assets, geometry, current existing sampling checks, and adequate provenance. Fixed/List initial samples can often qualify. A Timeline initial sample whose corrected prompt gains missing sections cannot qualify. Legacy continuation without proof of PR20 presentation identity cannot be assumed safe; regenerate it even when text matches. Do not unnecessarily discard an independently compatible initial chunk. Missing compiler metadata is never treated as current-version metadata.

Cache key: (compiler_version, compiled_text, include_first, include_last, presentation_digest). Presentation digest distinguishes Reference/Timeline Video windows and ordered First/Last/reference/audio assets. Cache remains run-scoped. Attach invocation-specific keyframes/frame count/masks to copied metadata after retrieval; do not mutate cached nested lists. No hashes of huge GPU tensors each step; reuse asset fingerprints and existing file hashes. Descriptor hashing is CPU-only. Revision locks, atomic writes, tensor checksums and nonce semantics remain intact.

Reroll/regenerate-from uses the existing requested boundary and effective nonce. Transport changes can force an earlier invalidation; report that exact first rejected physical group. Never force reuse just because the user requested a later reroll boundary. No destructive deletion of stored revisions.

## 8. File-by-file implementation plan

| File / functions | Responsibility |
|---|---|
| version.py | Prompt Plan/Session schema constants and readers' migration support |
| v2/prompts.py | Preserve source AST/preamble; schema validation/migration; rebuild connected-plan views; preserve overrides and fallback provenance |
| v2/physical_prompts.py (new) | Pure descriptor, interval resolution, renderer, hashes, legacy adapters and common equivalence logic |
| v2/sequence.py: run_sequence, _conditioning_cache | Resolve actual geometry before encoding; integrate compiled identity; retain PR20; pass exact conditioning to sampling/capture |
| v2/sequence.py: _preserved_prefix and terminal helpers | Sequential shared reuse predicate; atomic physical pair; replace Timeline-only special composition |
| temporal.py | Reuse current alignment/extension helpers; add only shared descriptor support if needed, no rounding changes |
| v2/session.py, v2/session_io.py | Versioned physical metadata serialization/validation and legacy reads |
| run_storage.py: build_sampling_contract, prepare, _valid_prefix, _load_entry, commit_chunk | Source/compiler revision identity; sequential physical checks; v3 writes/v2 migration; preserve atomicity |
| reference.py, v2/h3_builder.py | Preserve one-time tag shift; expose deterministic presentation identity; attach copied per-invocation metadata |
| timeline_video.py | Experimental actual-window source selection and selection fingerprint; legacy adapter |
| v2/nodes.py, v3/nodes.py, v3/driving_nodes.py | Preserve precedence/overrides and facade signatures; forward source metadata without widget churn |
| v3/plan.py, hardening.py | Verify optional metadata/terminal recombination survives; no geometric plan or output-duration change |
| masked_continuation.py, continuation.py, model_patch.py, v2/sampling.py | Regression targets; no algorithm change expected |
| tests/test_v2_prompts.py, test_v34_hybrid_conditioning.py, test_terminal_merge.py, test_v31_run_storage.py, test_v2_session.py; new physical prompt tests | Contract coverage and migration evidence |

Reference Audio/Driving Audio/Reference Video helpers should need no semantic changes beyond exposing already existing asset identities. Audit graph_contract.py fingerprints so activation/source identity neither disappears nor invalidates unrelated prefixes through an accidental global graph salt. Do not change Patcher-managed production installation in this design task.

## 9. Diagnostics and bounded tests

One compact record per physical invocation: group/logical indices, R/C/F, global/context/suffix frame intervals, contributing source ordinals, rational local ranges, compiler/text/physical hashes, First/Last flags, public-to-Qwen image mapping, text token count, video-window identity and reuse decision. Full prompt dump only under explicit diagnostic capture. Record input/provenance once per run, not every transformer call.

Required unit/integration matrix:
- 7s/39 two-chunk geometry above; initial overrun; corrected intersections around 6/8/10/12/14 seconds.
- More than two chunks, arbitrary supported 4–15s durations, fractional seconds, nearest-grid undershoot and final ceil.
- 5/22/39 video contexts; legal audio combinations; existing invalid exact-AV combinations remain governed by current validation.
- Native exact masks/source immutability; Guide overlap has no exact-protected label.
- First+Last and eight references; initial, middle, final and initial-State cases; identical text must not collapse First/Last cache entries.
- Reference tags shifted once; image/socket order, independent Audio/Video counters; references persist across sections.
- Preamble once, disjoint equal-overlap sections both present, overlaps, gaps, out-of-range sections, malformed/empty Fixed fallback.
- Fixed/List/repeat-last/JSON, [Chunk N], external sparse overrides, connected schema-1/2 plans and duration adaptation.
- Terminal first-pair and later Guide pair; unchanged eligibility/split/seed; shared physical hash; half-pair rejection.
- Storage exact hits, candidate search, missing metadata, compiler change, source-only change hidden by old logical hashes, unrelated later edit preserving initial prefix, corrupted metadata, old-reader rejection.
- Explicit Session parity; reroll boundary/nonce; resumed State context; reference asset changes; First/Last presentation changes.
- Timeline Video selection/timestamps/overrun/identity and Reference Video persistence.
- Same sampler/noise/sigmas/masks/model-options wrappers; one call per existing physical group; unchanged actual-prefix request; conditioning/refine capture matches actual invocation.
- Token-budget diagnostics do not silently truncate semantics or turn malformed prompt content into a custom stop.

This investigation executed a pure-source parser/geometry probe successfully. The local Python environment lacked torch, so existing tensor-based pytest suites and CUDA tests were not run here. PR20's live CI is prior upstream evidence, not a test of the proposed implementation. No production test results are claimed.

### Smallest model-level diagnostic

Before promoting the renderer, use existing conditioning capture to record the actual legacy prompt, compiled prompt, presentation mapping and token count for the same saved inputs. First establish whether 00417 was Timeline or opaque Fixed and whether its inner text already contains local/global timing.

Run a matched legacy/candidate pair with the complete production stack and identical raw inputs. If first-chunk compiled text is identical and provenance allows reuse, reuse that exact prefix to isolate continuation. If first-chunk text changes, run both complete sequences; do not mix incompatible prefixes and call it a matched full-run test.

If order improves but timing interpretation remains ambiguous, one bounded contrast can shift only a transition label within the same physical window while preserving section order/body and all sampling settings. Compare decoded transition timing; absence of a repeatable response leaves time-label adherence inconclusive. Do not infer it from embedding differences alone. No automatic broad sweep or extra H3 calls within any generation.

## 10. Matched CUDA/media promotion gate

Use the exact 00417 prompt, seven references, seed, resolution, chunk plan, sampler/sigmas, VDN/Sol/Spectrum/DiffAid/Untwist and native exact-prefix route. Capture workflow JSON, installed commit/overlay/file hashes, model/LoRA identities and all timing/geometry/ownership metrics. Current raw inputs must be retrieved before this gate can run; this document's synthetic probe is not a substitute.

Accept only when decoded media shows:
- lizard -> iguana -> turtle -> blue jay in the requested order;
- no premature iguana and no lizard reassertion after the iguana transition;
- clean continuation boundary, preserved reference fidelity and no opening First Frame reappearance;
- protected latent prefix equals the accepted source under the existing dtype contract;
- unchanged intended NFE/forecast topology and no added H3 invocations;
- native VDN local/rectangular Sol ownership retained and no external mixed-grid continuation.

Inspect the full sequence, boundary and transition frames; use global frame timestamps to separate genuine early transitions from final assembly trimming. Record actual transition frames and compare against intended timing; model timing tolerance is an observed result, not a new hard guarantee. Check both physical decoded groups and final assembled output. Audio must use the same independently selected pipeline in both runs; no audio repair is part of this comparison.

If the candidate passes only parser/unit/CI checks, it remains experimental. Failure with structurally correct compilation requires a new model-level hypothesis; it does not justify declaring success or silently adding reference gating.

## 11. Rejected alternatives and remaining questions

| Alternative | Status and scope |
|---|---|
| Low-resolution/Mixed-Grid continuation as primary cause | Falsified for the reported current failure: it persisted on native target-grid controls; not a universal judgment of progressive methods |
| Target-query sparsification/lifting as primary cause | Falsified for this failure by full-target/native controls |
| Incorrect user Picture declaration order | Falsified in supplied workflow account; internal presentation mapping remains a test obligation |
| First Frame leakage as sole current cause | Superseded as an explanation after PR20 overlay; keep its fix and verify runtime provenance |
| Sigma remapping for prior opening-frame leak | Falsified only for that earlier identity-sigma control; not a general temporal-quality claim |
| mixed_grid_low_suffix | Unsuccessful: reported framing instability and loss of desired native VDN route |
| Full target-grid staged dense diagnostic | Unsuccessful: did not cure ordering and was too slow |
| Target-Sparse lifter as production fix | Inconclusive/unsupported; experimental evidence does not justify promotion |
| Splicing two selected logical strings | Rejected structurally: discarded source sections cannot be recovered |
| Treating timestamps as enforced attention routing | Falsified by audited Core path; learned interpretation remains inconclusive |
| Hard temporal reference gating / per-frame embedding blend | Unsupported alternative within current contracts; changes model/attention behavior without established benefit |
| Global compiler salt | Superseded by versioned entries and sequential identity checks; needlessly discards compatible prefixes |

Unresolved questions and resolution:
1. Exact 00417 parser mode/body, assets and installed Core/Patcher provenance: retrieve raw workflow/log/files before implementation-time causal claims or CUDA validation.
2. Learned interpretation of local ranges: matched decoded test and bounded transition-label contrast above.
3. Persistent-reference competition after corrected transport: inspect matched output first; do not remove references speculatively.
4. Timeline Video temporal fidelity: separate matched window-adapter test; reference presentation timestamps do not prove target alignment.
5. Text length/performance: log actual token/packed row counts and sampler elapsed time; more text may change accelerated attention behavior despite identical NFE.
6. Long/overlapping authored prose that embeds its own timing: preserve opaque bodies and report ambiguity; do not guess how to rewrite natural language.
7. Initial-State unknown prior semantics: validate lead-in behavior without pretending the State contains its original prompt history.

### Implementation-time rechecks

Re-fetch main, PR20 refs/diff/reviews/checks and companion PR32. Reconstruct current main+overlay anew. Verify current parser/schema/facade/override precedence, terminal eligibility, context/motion selection, frame grid and assembly Last Frame preservation. Audit installed Core mask pooling/audio scaling, extra_conds, tokenizer, PackedLayout and wrapper APIs. Verify current Session/Storage schema and nonce/graph fingerprint behavior. Verify no Patcher overlay modifies these contracts. Check reference numbering with actual presentation assets and real CLIP. Revalidate sampler/forecast ownership against the installed VDN/Sol/Spectrum versions.

Deviations require source or runtime evidence recorded beside the revised design. Preserve PR20 as a distinct fix. Work on a mirror; checkpoint before risky work, long tests and after substantial progress. Production code belongs in a separate implementation change, never as a replacement for PR20.

## 12. Non-git evidence and reproducibility

No irreplaceable generated metrics, media or binaries were created. The pure-source probe and its expected results are reproducible from the pinned sources and the recipe below. Local source downloads are disposable copies of those commits.

The original 00417 workflow, references, metrics and decoded media remain required external validation inputs; exact paths were not supplied. Retrieve them by run identity and record their real paths/content hashes in the implementation evidence record. Do not invent filenames.

Local handoff input: /workspace/scratch/1dac3c689499/upload/Pasted text(20260913-030532).txt. Its material requirements are incorporated here; it is not required once this design is preserved.

Reproduction recipe for the completed pure-source probe (run at repository root):

    import sys, types
    from pathlib import Path
    root = Path.cwd()
    for name, path in [("probe_h3c", root), ("probe_h3c.v2", root / "v2")]:
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        sys.modules[name] = module
    from probe_h3c.temporal import align_frame_count_up, make_extension_shape_at_least
    from probe_h3c.v2.prompts import make_prompt_plan
    from probe_h3c.constants import PROMPT_FORMAT_TIMELINE
    script = "\n\n".join(
        f"[{2*i}-{2*i+2}s]\n<Picture {i+1}> section {i+1}"
        for i in range(7)
    )
    plan = make_prompt_plan(mode=PROMPT_FORMAT_TIMELINE, script=script,
                            chunks=2, chunk_seconds=7)
    assert plan["prompts"] == ["<Picture 1> section 1", "<Picture 5> section 5"]
    retained = align_frame_count_up(168)
    shape = make_extension_shape_at_least(39, 336-retained)
    assert (retained, retained-39, shape.total_frames,
            retained-39+shape.total_frames) == (175, 136, 209, 345)

Package namespaces are isolated only to avoid unrelated UI/torch imports; the actual unmodified parser and geometry modules execute. This is a source-contract probe, not H3 inference.

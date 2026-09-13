# Changelog

## Unreleased

- Scope First Frame Qwen visual presentation to the initial physical sample. Continuation chunks now rely on carried/generated context plus persistent Reference Images, preventing the opening composition from being reintroduced later while preserving initial I2VA/FL2VA semantics.
- Keep continuation conditioning caches distinct from initial-sample conditioning so identical prompt text cannot accidentally reuse a First-Frame-bearing Qwen embedding after the sequence start.

## 3.4.2

- Adapted upstream's V3.4 FL2VA terminal merge as an explicit physical-decode-group contract: 2×5-second FL2VA uses one Core-equivalent initial sample, while 3+ chunk merging is limited to Guide / Motion Context with the validated Balanced 22-frame prefix.
- Preserved the fork's Native Masked 39-video-frame / 65-audio-step generated-AV boundary by retaining per-logical-chunk sampling for long Native Masked sequences.
- Made terminal Run Storage pairs atomic, versioned their fingerprints and plan markers, and required bit-exact latent overlap before reconstructing one physical Core VAE decode unit.
- Added First/Last Frame images to hybrid Qwen presentation while preserving public Reference Image `<Picture 1>` through `<Picture 8>` numbering and keeping Last Frame out of non-final conditioning.
- Added explicit V3.4 assembler timeline output modes: exact requested duration remains the default, while Natural retained timeline (Refinement) exposes the complete physical retained timeline for downstream global tracking/refinement.
- Added `H3 Continuum Finalize Duration V3.4` so downstream refinement can operate on the natural timeline and then reapply Continuum's validated exact target-frame, final-frame, generated/Driving Audio, and final-anchor duration policy once after stitch-back.
- Added strict natural-timeline/finalizer validation so already-compacted input cannot be silently trimmed a second time.
- Corrected Timeline documentation so section headers and prompt text match the parser's line-based syntax.

## 3.4.1

- Completed the post-v3.4.0 V3.4 hotfix set: package registration/runtime-contract synchronization, Core-aligned prompt validation, finalized package metadata, and conditional-widget visibility are all included in this release.
- Fixed hybrid First/Last/Reference conditioning so Reference Images remain additive to the temporal keyframe mode instead of suppressing First Frame or Last Frame semantics. Native Masked and Guide continuation preserve the correct keyframe/reference composition, Run Storage fingerprints keyframes and references independently, and the V3.4 facade supports the full ordered Reference Image 1..8 range.
- Added tested-main GitHub Release automation and Comfy Registry publishing with the expanded Python/runtime validation matrix used by the current V3.4 package.
- Added V3.4 **Native Masked — exact continuation** as the recommended/default exact chunk-continuation mechanism using ComfyUI Core MiniMax H3 per-token denoise masks from PR #15375.
- Preserved **Guide / Motion Context** as a separate softer continuation method and kept V2/V3.3 workflow behavior on the legacy guide/RoPE path.
- Reused accepted generated H3 video/audio latents directly for protected target prefixes without decode/re-encode, with exact 39-frame/65-audio-step generated-AV boundary validation.
- Kept Driving Audio authoritative by protecting video only in that mode, retaining absolute source-audio guide slices, and preserving final source-audio assembly behavior.
- Versioned Native Masked continuation chunk fingerprints/plans so Run Storage never silently reuses Guide chunks as Native Masked while preserving reusable chunk 1 where possible.
- Kept Spectrum Interop API v1 Actual Prefix 2 independent of the old layout rewrite.
- Added exact per-chunk `refine_state` output for learned-latent sampler-2 workflows. Each state carries a fresh clone of the exact Continuum MODEL wrapper plus the exact positive CONDITIONING used for that chunk.
- Restored the exact captured Native Masked video/audio denoise-mask members onto the matching split LATENT outputs when refinement-state capture is requested, keeping chunk N's video, audio, model, conditioning and masks aligned.
- Made refinement capture output-link-driven so ordinary Continuum workflows keep the previous runtime/memory behavior when `refine_state` is unused.
- Kept Run Storage fail-closed for exact refinement when a stored prefix would leave raw runtime MODEL/CONDITIONING state unavailable; use Run Storage Off or regenerate from Chunk 1 for an exact refinement run.
- Added regression coverage and documentation for native masks, temporal mapping, references/keyframes, restartability, assembly, refinement-state capture, output ordering, Run Storage alignment and compatibility requirements.
- Completed coordinated real CUDA validation with the integrated MiniMax H3 latent upscaler/refiner, Spectrum and DiffAid. The exact handoff ran through target-resolution sampler 2 successfully, and the validated three-step refinement used `actual -> forecast -> actual` with user-confirmed impeccable media quality.

## 3.4.0

- Completed the V3.4 runtime path from the public sampler through V3/V2 to `run_sequence()`.
- Added Driving Audio with absolute-time guide slices while preserving the original source for final output.
- Added persistent Video Reference conditioning with Efficient 0.4 MP, Balanced 0.6 MP, and Match Output modes.
- Included Driving Audio and Video Reference identities in Run Storage contracts.
- Changed malformed or empty prompts to warning-and-fallback behavior instead of stopping generation.
- Removed independent compatibility, model, and upstream-node restrictions that were stricter than ComfyUI Core.
- Added V3.4 runtime, driving-audio, and context diagnostics coverage.

## 3.3.0

- Promoted Timeline Video to a stable public node with chunk-local slicing and a 0.4 MP default.
- Added `H3 Continuum Assemble + Seam` with stable Audio Seam Auto and guarded Video Seam Auto defaults.
- Kept `Auto 2` as an experimental exposure-ramp extension without widening the visible option label.
- Added native Reference Audio conditioning and retained up to three ordered Reference Images.
- Preserved stable sampler Node IDs, Run Storage compatibility, Core VAE Decode, and Spectrum Interop API v1 Actual Prefix 2 behavior.
- Added consolidated runtime validation results and documented known Timeline Video and Auto 2 limitations.

## 3.2.4

- Added native MiniMax H3 Reference Audio 1 conditioning with deferred Audio VAE encoding.
- Matched Core's permissive prompt handling by warning instead of stopping on unavailable Picture or Audio tags.
- Matched Core's dynamic Reference behavior by compacting active image inputs into Picture 1 through Picture N.
- Kept Sampling, Continuation, Spectrum Interop, external Core VAE Decode, and assembly semantics unchanged.

## 3.2.3

- Formalized the third ordered Reference Image as a compatibility-preserving extension.
- Kept V3.2.2 zero, one, and two-image Reference Contract JSON unchanged.
- Added Image 3 shape, dtype, exact SHA-256, position, and preprocess metadata only for three-image runs.
- Added Golden Contract and Revision identity regression coverage for V3.2.2 saved runs.
- Kept Sampling, Continuation, Spectrum Interop, Core VAE Decode, and assembly semantics unchanged.

## 3.2.2

- Allowed Ref2VA, FL2VA, and unverified checkpoints with Reference conditioning.
- Kept checkpoint classification as diagnostics without automatic MODEL switching.
- Kept Strict Compatibility for genuinely unsafe or unsupported H3 contracts.

## 3.2.1

- Declared V3.2.1 stable after GitHub Actions, Windows Ref2VA generation, Spectrum Actual Prefix 2, chunk integrity and complete Auto Resume passed.
- Added T2VA, First/Last Frame, FL2VA and persistent two-image Reference conditioning to the production sampler.
- Added crash-safe raw AV Run Storage, automatic resume, deterministic Revisions and partial regeneration.
- Combined ordered upstream graph fingerprints with runtime MODEL/CLIP/VAE weight probes for fail-closed reuse.
- Added per-chunk SHA-256 verification and rejected Regenerate From when Run Storage is disabled.
- Added official MiniMax H3 Sigma Shift graph-contract support.
- Updated CI dependencies, Windows runtime verification and package metadata for V3.2.1.
- Standardized project licensing as MIT.
- Kept sampling, Continuation, Spectrum Interop and external Core VAE decode semantics unchanged.

## 2.1.7

- Added the compact static `H3 Continuum Sampler` facade over the unchanged V2 execution core.
- Added Clip Overrides, Advanced, and Result pack helper nodes.
- Preserved `H3ContinuumSamplerV2` as a Legacy/Core workflow compatibility node.
- Removed the frontend display controller and all dynamic socket, proxy-widget, DOM, resize, and serialization workarounds.
- Kept State, Session, Prompt Plan, Sampling, Native Continuity, and Spectrum Interop contracts unchanged.

## 2.1.6

- Rebuilt the Sampler V2 frontend as one Vue-aware display controller.
- Removed the Individual Clip Overrides accordion; only active Clip Prompt sockets are shown.
- Kept connected out-of-range Clip Prompt sockets visible when chunks is reduced.
- Added one Advanced Settings control; connected advanced sockets stay visible while collapsed.
- Removed fixed node-height handling and all input/output add/remove UI folding.
- Kept backend socket/widget order, types, serialization, State/Session/Prompt Plan schemas, sampling, and Spectrum Interop unchanged.
- Exposed show_preview as a per-node basic control and removed the duplicate global preview setting.

## 2.0.1

- Fixed the bundled V2 example workflow widget serialization for `base_seed` with `control_after_generate`.
- Added an in-memory migration guard for the malformed 2.0.0 example where later widgets were shifted by one slot (`diagnostics` arrived as `0`).
- Preserved valid 2.0.0 workflows and all V1/V2 node identifiers and schemas.
- Added regression coverage for the legacy shifted-widget signature.

## 2.0.0

- Added production **H3 Continuum Sampler V2** for integrated N-chunk generation.
- Kept the V1 continuation algorithm and all V1 node identifiers unchanged.
- Added Fixed, List, and Timeline prompt planning plus a connectable Prompt Plan node.
- Cached each unique Qwen prompt conditioning before H3 sampling.
- Reused one externally accelerator-patched MODEL, sampler, and sigma schedule across all chunks.
- Deferred Video/Audio VAE decoding until every H3 chunk finished.
- Captured full raw AV chunk latents and next-state tails on CPU before decoding.
- Removed the second device-to-host state transfer by deriving next-state tails from committed CPU entries.
- Added conservative latent-motion Auto Context selection with 5/22/39-frame profiles.
- Added deterministic independent 64-bit per-chunk seeds and reroll nonces.
- Added V2 Session resume, accepted-prefix reuse, branch-safe reroll, and atomic safetensors/JSON persistence.
- Added exact cumulative audio sample-boundary assembly to prevent long-chain rounding drift.
- Added exact total-duration correction.
- Added single-allocation decoded IMAGE assembly and preflight RAM safety checks.
- Added Basic and Full diagnostics, including video overlap MAE/PSNR and audio correlation/boundary jump.
- Added optional V1 State entry point and V2 Session-to-V1 State conversion.
- Added V2 public API modules and regression/orchestration tests.
- Updated installer runtime verification for all ten registered nodes.

## 1.0.1

- Fixed a false strict-compatibility failure when Sol-Attn wraps MiniMax H3
  `PackedLayout.__init__` with `*args` / `**kwargs` for Morton/span tracking.
- Strict checking still fails closed when the native H3 constructor genuinely
  removes required positional or keyword capabilities.
- Added regression tests for the native signature, Sol-Attn forwarding wrapper,
  and a genuinely incompatible constructor.

## 1.0.0

- Added **H3 Continuum Join** for initial and continuation clips.
- Added direct native video/audio latent state capture with 5/22/39-frame profiles.
- Represented continuation history as one native H3 `video` / `video_audio` reference block.
- Added Finish, Assemble, atomic State Save/Load, signed audio-grid offsets, in-place MM-RoPE adjustment, strict compatibility tests, and accelerator composition.

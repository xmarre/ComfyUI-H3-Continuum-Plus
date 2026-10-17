# Validation and hardware acceptance

## Executed evidence

The standalone Python suite passes **525 tests**. This includes the existing Continuum regression suite plus Timeline Studio's model, runtime, media and composition tests. The JavaScript editing suite passes **8 tests**. Syntax, compileall and the repository's targeted Ruff checks pass.

The implementation checkpoint's GitHub Actions test matrix passed on Python 3.10, 3.11, 3.12 and 3.13, including publisher-toolchain verification. Final commit checks are authoritative on the implementation PR.

| Layer | Executed check | What it establishes |
| --- | --- | --- |
| Temporal compiler | Exact `[0,7)` / `[7,14)` source windows; absolute 44.1/48 kHz sample boundaries; long prompt lists; exact outer-header isolation; malformed-text fallback | Deterministic logical scheduling and role separation |
| Actual native sequence loop with CPU sampler fixtures | Invocation-local reference changes under both compiler modes; changed chunk 2 preserves chunk 1; equal encoder inputs reuse conditioning while changed references do not | Runtime routing, causal prefix and cache integration; not H3 output quality |
| Real media | FFmpeg/PyAV 25 fps, VFR and nonzero-PTS sources; mono/stereo normalization; exact guide-frame padding and original source selections | Source decoding and normalized time/sample behavior |
| Long source | A real two-hour video, with a selected seven-second window, decodes fewer than 64 source frames including seek context | No complete-film decode on the selected video path; does not quantify every codec's memory use |
| Composition | Lossless normalized RGB and float PCM comparisons; replacement interval with unchanged original frames and soundtrack outside it; cancellation cleanup | Working retained/generated composition and normalized export |
| Persistence | Atomic project saves, revision conflicts, registered-asset verification, missing/stale path rejection and relinking identities | Disk/project identity behavior |
| Real ComfyUI server | Import, save/reopen, compile, thumbnails, waveform, bounded composition preview and export endpoints; four registered nodes; actual queued Studio→Export execution | HTTP/node integration without trained weights |
| Real ComfyUI frontend | Open, split, undo/redo, save/reload, typing isolation, A/B transport, composed AV preview, independent cloning, workflow disposal, graph serialization and delivered sampler-workflow serialization | Browser integration on ComfyUI 0.41.0 / frontend 1.55.17 |

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

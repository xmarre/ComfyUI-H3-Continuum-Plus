# Physical timeline prompt transport validation

This document describes the validation-only execution controls and evidence boundary for the physical timeline prompt transport implementation. The candidate is not a production default: legacy nominal prompt transport remains the default until matched decoded-media validation passes.

## Execution controls

No workflow widget or serialized node input is added. The execution path is selected only through process environment variables before ComfyUI starts.

### Legacy nominal control

Linux / WSL:

```bash
env -u H3_CONTINUUM_PHYSICAL_PROMPTS \
    -u H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO \
    python main.py
```

PowerShell:

```powershell
Remove-Item Env:H3_CONTINUUM_PHYSICAL_PROMPTS -ErrorAction SilentlyContinue
Remove-Item Env:H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO -ErrorAction SilentlyContinue
python main.py
```

Setting `H3_CONTINUUM_PHYSICAL_PROMPTS=0` is equivalent to leaving it unset.

### Candidate physical text transport with legacy Timeline Video

Linux / WSL:

```bash
H3_CONTINUUM_PHYSICAL_PROMPTS=1 \
env -u H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO \
python main.py
```

PowerShell:

```powershell
$env:H3_CONTINUUM_PHYSICAL_PROMPTS = '1'
Remove-Item Env:H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO -ErrorAction SilentlyContinue
python main.py
```

This is the primary text-only validation path. Timeline Video retains its nominal per-chunk adapter.

### Candidate physical text transport plus physical Timeline Video adapter

Timeline Video is a separate behavioral intervention and has a separate gate.

Linux / WSL:

```bash
H3_CONTINUUM_PHYSICAL_PROMPTS=1 \
H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO=1 \
python main.py
```

PowerShell:

```powershell
$env:H3_CONTINUUM_PHYSICAL_PROMPTS = '1'
$env:H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO = '1'
python main.py
```

Do not use the Timeline Video candidate result as evidence for text-only promotion. It requires its own matched decoded-media comparison.

## Runtime evidence required before a matched run

Record the actual installed runtime rather than substituting public repository state:

- ComfyUI checkout path, current commit SHA, branch and dirty/overlay state;
- any Patcher/overlay checkout that changes MiniMax-H3 masking, conditioning or wrapper behavior, including its path, commit SHA and dirty state;
- the Continuum implementation commit used for the run;
- installed VDN, Sol-H3, Spectrum, DiffAid and Untwist versions/commits that participate in the model path;
- model, LoRA and VAE identities needed to reproduce the run.

The public Core source audited by the design is useful source evidence, but it is not proof of the installed runtime used for CUDA validation.

## Required saved-input evidence

The design identifies run `00417` as the matched behavioral reproduction. Its original workflow, prompt source, seven reference images, metrics, logs and decoded media are external validation inputs. They are not stored in this repository and no repository path is asserted for them.

Before CUDA validation, record for every retrieved artifact:

- the real filesystem path;
- SHA-256 of the exact file bytes;
- workflow parser mode and exact source prompt text;
- seed, resolution, chunk plan, sampler and sigmas;
- all reference identities and ordering.

Do not reconstruct a missing artifact from logs, screenshots or remembered values and present it as the original input.

## Structural preflight

For the exact saved input, capture before sampling:

- legacy logical prompt text for each invocation;
- candidate compiled physical prompt text;
- physical descriptor `R/C/F`, global frame interval and protected/guided interval;
- compiler, descriptor, text and physical-conditioning hashes;
- public Reference Image to Qwen Picture mapping;
- First Frame / Last Frame presentation flags;
- Qwen text token count and packed span/layout metadata when exposed by the installed Core;
- Timeline Video adapter/selection identity when applicable.

A Timeline prompt may change physical text while keeping the compatibility `entry.prompt` and 64-character `prompt_hash` unchanged. Those logical fields must not be used as proof that physical conditioning is equivalent.

## Matched CUDA comparison

Use the same prompt, all references, seed, resolution, chunk plan, sampler/sigmas, Native Masked continuation, VDN, Sol-H3, Spectrum, DiffAid and Untwist stack for both paths.

If the first physical invocation has byte-identical compiled text and the same presentation identity, its exact compatible prefix may be reused to isolate continuation behavior. If the first invocation changes, run both complete sequences from the beginning.

For each run record:

- H3 logical / actual / forecast NFE topology;
- number of physical H3 invocations;
- text token count and packed text/target/reference spans when available;
- sampler and end-to-end timing;
- VDN/Sol ownership receipts;
- protected-prefix equality under the existing dtype contract;
- decoded transition frames on the global timeline.

More compiled text may increase attention cost. The invariant is no additional H3 evaluations, not identical FLOPs or runtime.

## Promotion gate

The candidate remains experimental unless decoded media shows all required behavior:

- reference progression occurs in the requested order;
- no premature transition and no regression to an earlier reference after the transition;
- the physical continuation boundary is clean;
- First Frame does not reappear on continuation;
- reference fidelity is preserved;
- the protected native prefix is unchanged;
- intended H3 NFE/forecast topology is unchanged and no H3 invocation is added;
- native VDN/Sol ownership remains intact.

Textual time ranges remain learned Qwen guidance. Passing parser, hash, storage, unit or CI checks does not establish structural per-frame temporal routing. If corrected transport is structurally valid but decoded timing remains ineffective, keep legacy execution as the default and record learned temporal adherence as unresolved rather than adding an unplanned attention-routing mechanism.

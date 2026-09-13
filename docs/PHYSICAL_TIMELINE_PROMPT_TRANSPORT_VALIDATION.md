# Physical Timeline Prompt Transport Validation

This draft PR is intended to be applied through ComfyUI-Patcher. Applying the PR is the activation boundary for the text-transport candidate; no extra workflow node and no shell-level feature enablement are required.

The package entry point defaults `H3_CONTINUUM_PHYSICAL_PROMPTS` to `1` only when the variable is unset. An explicit environment value still wins, so `H3_CONTINUUM_PHYSICAL_PROMPTS=0` remains an emergency disable/bisect hook. Descriptor-aware Timeline Video remains separately gated and is not enabled by this PR default.

The active compiler is `physical_timeline_text_v2`. Validation media must show the compiler activation line and, for the recovered nested 00418-style schedule, `H3C-PT205` before the result can be treated as evidence for or against the candidate.

ComfyUI-Patcher materializes tracked overlays as synthetic merge commits. Therefore the local repository HEAD after Patcher application is not expected to equal the PR head SHA; PR membership must be determined from the materialized tree/overlay state, not by raw local HEAD equality.

Promotion still requires matched decoded media proving correct semantic order, exact protected-prefix behavior, unchanged H3 invocation/NFE topology, and retained VDN/Sol ownership.

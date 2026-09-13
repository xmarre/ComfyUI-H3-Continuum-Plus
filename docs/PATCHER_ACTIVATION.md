# ComfyUI-Patcher activation contract

PR #22 is intended to be applied through ComfyUI-Patcher. Applying the PR is the activation boundary for the physical Timeline text-transport candidate; no additional workflow node and no shell-level feature enablement are required.

The package entry point defaults `H3_CONTINUUM_PHYSICAL_PROMPTS` to `1` only when the variable is unset. An explicit environment value still wins, so `H3_CONTINUUM_PHYSICAL_PROMPTS=0` remains an emergency disable/bisect hook. Descriptor-aware Timeline Video remains separately gated and is not enabled by this default.

ComfyUI-Patcher materializes tracked overlays as synthetic merge commits. Therefore a local repository HEAD produced by Patcher is not expected to equal the pull-request head SHA. Validation must use the materialized overlay state and runtime activation evidence, not raw local SHA equality.

The runtime candidate is `physical_timeline_text_v2`. For the recovered nested 00418-style prompt, valid evidence should include the physical compiler activation line and `H3C-PT205`. Promotion still requires matched decoded media proving semantic order, exact protected-prefix behavior, unchanged H3 invocation/NFE topology, and retained VDN/Sol ownership.

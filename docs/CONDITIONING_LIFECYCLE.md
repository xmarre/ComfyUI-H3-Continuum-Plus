# Bounded conditioning lookahead

Eligible neighboring physical invocations are encoded together before the first
invocation samples. This lets the following invocation reuse Qwen conditioning
after sampling has caused text-encoder weights to be partially offloaded.
The transformer sampler, generated context, masks, seeds and VAE schedule are
unchanged.

Eligibility requires a known context length: an explicit continuity profile, or
Native Masked Auto with generated audio continuity (the fixed 39-frame AV
boundary). The preceding invocation's planned state capacity must accommodate
that context. Motion-dependent Auto, Timeline Video and terminal merged pairs
retain sequential encoding. No generated video/audio is predicted or cached.

The ordinary physical prompt compiler and encoder prepare the future result.
Authored dialogue history, when provided by the dialogue-history overlay, stays
owned by that compiler. At consumption, both the actual descriptor and the
ordinary conditioning key are rechecked. A mismatch uses ordinary encoding.

Lookahead retains one independent CPU conditioning snapshot, capped at 256 MiB.
It is request-local and discarded after the next invocation; it has no persistent
prompt, model, LoRA or GPU cache. This bounds added retained memory independently
of sequence length. An oversized result is not retained. Existing request-local
conditioning reuse remains in place.

The feature is enabled by default. `H3C_CONDITIONING_LOOKAHEAD=0` restores the
sequential schedule for a matched comparison. Compare the same workflow, seed,
prompt, references, model/LoRAs, geometry, NFE and accelerator settings. Check
that eligible two-chunk runs encode both chunks before sampling and perform no
new text-encoder load between chunks. Compare complete prompt wall time, Qwen
and model load times, allocated/reserved/free memory and decoded audio/video.

CPU tests establish conditioning identity and scheduling for one through four
chunks. They do not establish trained-Qwen numerical parity across residency
states, GPU speedup, peak memory or repeated-generation stability. The candidate
requires those GPU checks before release.

The V3 production sampler also offers an optional **Qwen Lookahead** advanced
boolean. Its value is invocation-local and can be toggled between two otherwise
identical hot workflow executions without restarting the ComfyUI process. The
Advanced V3 configuration pack exposes the same setting. Existing workflows
without this input continue using the process-wide `H3C_CONDITIONING_LOOKAHEAD`
default. Nested/concurrent executions do not share a mutable toggle. The
`H3C-PT236` receipt times speculation; `H3C-PT237` reports the actual
conditioning call's unsynchronized **host** wall time and whether its *canonical
encoder key* matched the prefetched snapshot. Neither counter is CUDA event
latency; these measurements should not be added to enclosing prompt walls.

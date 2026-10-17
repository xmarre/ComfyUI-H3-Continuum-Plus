# Experimental causal dialogue history

This experiment addresses a conditioning information loss in Timeline
continuation. The physical compiler suppresses authored scene/dialogue text in
an exact protected prefix to avoid replay. The audio latents survive, but Qwen
never sees that interval's dialogue script or speaker declaration.

Applying the experimental PR overlay enables this transport. No additional
launcher setting is required. `H3C_CAUSAL_DIALOGUE_HISTORY=0` disables it for a
controlled comparison. This is **not a validated speech fix**.
Native Masked and Guide/Motion Context use the same encoder transport. Fixed,
List and legacy opaque prompts retain their existing behavior.

Each physical invocation emits `H3C-PT235 dialogue-history selection`, recording
whether the transport is enabled, whether authored history was selected, its
digest and encoder-cache reuse. `history_available=False` means the invocation
has no selected native dialogue history; an initial chunk normally has none.
When new Qwen encoding actually uses history, `H3C-PT234 dialogue-history encoder
receipt status=applied` records its token count and confirms zero retained
history rows. An unsupported tokenizer/encoder layout emits a specific fallback
warning. Selection alone does not prove the history was encoded successfully.

## Transport and ownership

The compiler resolves authored Timeline intervals intersecting the carried
window from the preceding logical invocation. Outer chunk routing still selects
only the current body for active generation. Native `<d>...</d>` dialogue and its
surrounding speaker/delivery description enter a separate encoder history; the
shared preamble supplies authored speaker definitions. Only authored script is
encoded as history: interval timestamps, JSON wrappers, internal field names
and contextual instructions remain in diagnostics and are never presented as
Qwen language. No transcript is invented
for an external State. An initial chunk or an overlap with no native dialogue
uses ordinary encoding.

Qwen encodes reference presentation, history and current instructions in one
causal forward. Before conditioning reaches H3, the history hidden-state rows
and their modality tags are removed. Reference rows stay in their original order;
current text rows retain their causal access to the history. The DiT receives
no extra historical token rows, no additional audio reference, and no modified
latent, noise, seed, sigma, position ID or denoise mask. Reference picture numbers
and scheduled-conditioning metadata are preserved. Temporary encoded history
states are not cached; compacted conditioning is owned by the invocation cache.

The history participates in the physical conditioning hash, persisted metadata
and encoder-cache identity. Session/reuse validation recompiles the same history;
a history-policy or content change invalidates the affected continuation. The
new metadata field is optional; histories are authenticated with the same
conditioning digest as the active prompt. Unsupported tokenizer/encoder layouts
fall back to ordinary current-prompt encoding. That fallback cannot pass
unfiltered historical embeddings into H3.

## Limits and required validation

An authored interval is not a transcript or word alignment. The compiler cannot
know which words were spoken, or infer an unfinished word from a timestamp.
Ongoing utterances retain their complete authored script in encoder history;
active future text is never deleted or guessed from word position. A single
unsplit dialogue block that crosses the physical join can still occur in active
suffix instructions. This experiment does not establish that H3 will avoid
restarting that utterance or preserve its voice. Excluding direct history rows
also cannot guarantee that the current hidden states will never induce replay.

CPU tests establish causal transport, row exclusion, variable visual expansion,
metadata/hash integrity, no mutation/RNG consumption, current-body isolation and
no-dialogue/first-chunk behavior. They cannot establish speech intelligibility,
trained Qwen/H3 behavior, audiovisual timing quality, GPU throughput or kernel
reproducibility. Hardware acceptance must include a sentence across a join,
completed dialogue, new dialogue, speaker/scene changes, and 3–4 chunk speech,
with completed-phrase replay and voice identity assessed on decoded output.

No merge or release is justified by these CPU checks alone. Existing audio-phase
alignment and exact-prefix restoration remain separately tested contracts.

A separate integration check can exercise Core's actual Qwen causal backbone
with a tiny random CPU model, without downloading checkpoints:

```bash
python tools/verify_causal_dialogue_history.py --comfy-root /path/to/ComfyUI
```

The expected result is equal reference states, context-sensitive current text
states, removed history rows, and bitwise repeatability in this CPU example.
This still does not establish trained-model speech behavior.

# Experimental causal dialogue history

This experiment addresses a conditioning information loss in Timeline
continuation. The physical compiler suppresses authored scene/dialogue text in
an exact protected prefix to avoid replay. The audio latents survive, but Qwen
never sees that interval's dialogue script or speaker declaration.

Set `H3C_CAUSAL_DIALOGUE_HISTORY=1` before starting ComfyUI to enable the
experiment. It is disabled by default and is **not a validated speech fix**.
Native Masked and Guide/Motion Context use the same encoder transport. Fixed,
List and legacy opaque prompts retain their existing behavior.

## Transport and ownership

The compiler resolves authored Timeline intervals intersecting the carried
window from the preceding logical invocation. Outer chunk routing still selects
only the current body for active generation. Native `<d>...</d>` dialogue and its
surrounding speaker/delivery description enter a separate encoder history; the
shared preamble supplies authored speaker definitions. No transcript is invented
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

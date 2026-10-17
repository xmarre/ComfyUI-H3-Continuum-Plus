# Internal Continuum controls and generated speech

MiniMax H3 conditions video and speech on the same Qwen text embeddings. The
physical Timeline compiler must never insert implementation instructions into
that text. In particular, exact protected prefixes, uncovered fresh gaps,
terminal lead-out intervals, and discarded native-grid padding are structural
intervals rather than natural-language descriptions for the model to interpret.

The compiler records these intervals with their original rational timestamps,
role, source provenance and diagnostic receipts. They do not appear as words or
empty synthetic ranges in the encoded prompt. Exact protected-prefix latent
restoration still happens in the sampler. Uncovered fresh gaps receive no
invented prose. User-authored Timeline bodies, dialogue markers, speaker
definitions and intentional dialogue timing remain in the generated prompt.
Fixed/List and opaque legacy prompts are passed through unchanged; user text
is never filtered by keyword.

The optional causal dialogue-history encoder may expose preceding *authored*
dialogue and speaker descriptions to Qwen as causal context, but removes their
hidden-state rows before DiT conditioning. Internal interval fields, JSON
wrappers and explanatory sentences must not enter this Qwen context. Temporal
audit fields remain in compilation diagnostics.

An empty terminal lead-out interval does **not** directly command H3 to stop
speaking, nor does it imply audio muting. Exact-duration output still uses the
existing physical trim and assembly contract. Removing the former natural
language terminal speech-control instruction avoids speech contamination but
may change speech timing near the final cut. No decoded-audio quality guarantee
follows from passing CPU tests.

## Validation

For both Guide/Motion Context and Native Masked, compare matched GPU runs with
the same source prompt, seed, image/audio references, resolution, clip duration,
model and sampler. Include (1) dialogue crossing a chunk boundary, (2) short
dialogue ending before the last-frame trim, (3) a final spoken sentence near the
exact output boundary, and (4) a run with 3+ chunks and authored future
Timeline events. Inspect the exact Qwen input, compiled interval manifest,
speaker intelligibility, inadvertent narration of control instructions,
dialogue repetition and sentence truncation in decoded audio.

The unit suite checks structure, source isolation, prompt identity, and causal
token transport. It cannot establish that the generative model will not
hallucinate unsolicited speech or remain visually and acoustically continuous.

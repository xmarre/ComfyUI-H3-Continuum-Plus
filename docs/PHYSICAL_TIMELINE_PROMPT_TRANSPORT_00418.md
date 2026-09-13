# 00418 physical-timeline evidence

This note records the production evidence that required the physical text compiler to move from `physical_timeline_text_v1` to `physical_timeline_text_v2`.

The 00418 media, full runtime log and metrics were supplied out-of-tree during validation; they are not committed to this repository. The saved workflow embedded in the media reconstructs the exact authored prompt and Continuum controls. The run used two 7-second logical chunks, Strong 39-frame Native Masked continuation, seven ordered Reference Images, generated audio continuity, and no Driving Audio.

## Recovered prompt structure

The authored Timeline has two ordinary bracket sections:

```text
[0-7s]
...
[7-14s]
...
```

Each enclosing body then contains a strict, complete sub-schedule using whole-line bare range headers:

```text
0-2s:
...
2-4s:
...
4-6s:
...
6-7s:
...

7-8s:
...
8-10s:
...
10-12s:
...
12-14s:
...
```

The original design did not have the exact production prompt artifact. It therefore deliberately treated section bodies as opaque prose and prohibited recursive timestamp rewriting. That was the correct conservative rule for unknown prose, but the recovered workflow proves that this production prompt carries a second, explicit timeline grammar inside the enclosing timed sections.

`physical_timeline_text_v1` could only resolve the two enclosing 7-second bodies. In the second physical H3 invocation it would therefore present the entire `[7-14s]` body as one atomic interval even though that body itself says `7-8s`, `8-10s`, `10-12s` and `12-14s`. Rebased outer labels and unre-based inner labels are conflicting temporal instructions.

The decoded 00418 video still showed the corresponding semantic oscillation: lizard, premature iguana, lizard reassertion, then iguana, turtle and blue jay. The user reported that overall image quality was otherwise acceptable.

The old runtime did not print the active physical compiler identity to the console, so the 00418 log alone does **not** prove that the candidate environment gate was enabled for that run. V2 now emits a compact runtime line containing compiler version, physical group/window, interval count and text SHA-256 whenever the physical Timeline candidate is active. Future media evidence can therefore prove the exact text path directly.

## V2 grammar boundary

V2 does **not** generally parse timestamps from prose. It recognizes an inner schedule only when all of the following are true:

1. the enclosing source section is already a valid timed Timeline section;
2. the first nonblank body line is exactly a whole-line `start-end[s]:` header;
3. every inner header has a non-empty body;
4. inner ranges are increasing and non-overlapping;
5. they form an exact contiguous partition of the enclosing timed interval.

If any condition fails, the enclosing body remains opaque exactly as before. Fixed, List, migrated schema-1 plans, explicit Chunk sections, logical prompt/hash compatibility and the default legacy execution path are unchanged.

This is intentionally a compiler-version boundary rather than an unversioned behavior change. Stored physical conditioning identities therefore distinguish V1 and V2.

## Exact 00418 physical window

For the second invocation, the recovered Native Masked geometry is:

```text
retained before R = 175 frames
protected context C = 39 frames
physical total F   = 209 frames
physical start     = 136 / 24 = 5.666666... s
physical end       = 345 / 24 = 14.375 s
protected end      = 175 / 24 = 7.291666... s
```

V2 therefore compiles the physical-local schedule from the strict subranges as:

```text
local [0,       0.333333)  <- global [5.666667, 6)  gharial tail
local [0.333333,1.333333)  <- global [6, 7)         lizard
local [1.333333,2.333333)  <- global [7, 8)         lizard continuation
local [2.333333,4.333333)  <- global [8, 10)        iguana
local [4.333333,6.333333)  <- global [10, 12)       turtle
local [6.333333,8.708333)  <- global [12,14.375)    blue jay / native-grid overrun hold
```

The generated suffix begins at local `1.625 s`, still inside the authored `7-8s` lizard interval. Iguana therefore does not become the active authored state until local `2.333333 s`.

No new sampler invocation, H3 transformer evaluation, temporal mask, structural time router, reference reorder or geometry change is introduced by this compiler change. Timing remains learned Qwen text conditioning.

## Performance evidence from the same run

00418 also establishes a separate performance problem:

- Continuum sampler node: about `560.20 s`;
- total sampler logical calls: `17`;
- actual transformer NFE: `13`;
- Spectrum forecasts: `4`;
- first progressive physical sample: about `250.81 s` sampler wall;
- second exact Native Masked sample: about `252.64 s` sampler wall;
- second sample took the intentional `exact_video_protection` target-grid fallback and used `8L / 6A / 2F`.

The full-target fallback is currently a correctness boundary: silently resizing an exact protected prefix into a private low-grid lifetime would change the attention context even if the returned prefix were restored afterward. The prompt-transport fix does not weaken that safeguard. Any speed promotion for exact continuation needs its own matched decoded-media proof that image quality, protected-prefix semantics and VDN/Sol ownership remain intact.

## Audio scope

The same run had no audible click after the separate global 24-fps / 40-Hz audio phase correction, but the user still judged generated audio quality as low. That issue belongs to the audio-continuation diagnostic work in `MiniMax-H3-Flow-Aligned-Regenerate` PR #32, not to physical prompt transport.

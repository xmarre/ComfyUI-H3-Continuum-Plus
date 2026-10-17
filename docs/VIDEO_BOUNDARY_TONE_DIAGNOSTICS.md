# Decoded video tone measurements

Continuum assembly records `H3C-PT227` at each physical decoded-video join, before applying that join's video patch. The measurement uses existing decoded images and adds no decoder or transformer calls. It does not change images, audio, the assembly plan, or seam decisions.

The receipt contains per-frame RGB mean and population standard deviation, luma mean and population standard deviation, and luma fifth/ninety-fifth percentiles. It examines eight preceding retained frames and up to 24 following retained frames. These statistics expose contrast changes that preserve mean luma and therefore escape a detector based on a brightness pulse. Motion, composition, and lighting also change these statistics; they are not a quality score or an instruction to normalize generated content.

A separate plan-aligned comparison examines at most 22 duplicate overlap frames. It reports sampled RGB difference and per-channel affine gain, with undefined gain reported as `null` for constant reference channels. Native H3's first five frames at a decode origin and last five frames without matching right context can differ. The receipt separates the overlap's context interior and final five-frame tail. A decoder attribution requires independently verified identical carried latents and the native temporal window phase. Guide-mode or independently refined overlap cannot supply that premise.

Photometric measurements operate on an aspect-preserving bilinear sample with a maximum long side of 192 pixels. Equality of these samples is not a byte-exact claim about full-resolution images. Conversion workspace processes four frames at a time; no full decoded chunk is cloned. Unavailable measurements are logged without blocking assembly.

`H3C-PT228` records the actual Video Seam mode and each available guard decision, including luma shift, reversal, global support, correction width, and action. The existing transient/micro-flash and optional exposure-ramp guards remain unchanged. A `clean_boundary` classification belongs to that detector's feature set; it does not certify contrast continuity or perceptual quality.

The Flow Decode Context node separately logs how many exact boundaries received five real future latent tokens and why others were skipped. Combining that activation receipt with the decoded overlap comparison distinguishes a missing decode-context window from an appearance change in the newly generated suffix. It does not by itself distinguish the low/probe, learned transfer, and final refinement stages; that attribution requires a matched decoder intervention or rendered stage witnesses.


## Delayed single-frame luminance pulses

The original Video Seam flash detector compares the first retained continuation
frame with the final preceding frame. A short exposure dip or bright flash on
the next three frames could therefore be missed even when decoded per-frame
statistics showed an impulse. Auto and Auto 3 can diagnose offsets +1,
+2, and +3 against their immediate neighboring frames. Only Auto and Auto 2
apply the new delayed-flash patch automatically: in Auto 3, this patch is
disabled to avoid suppressing the independent sustained-tone estimator. The guard requires a
luminance impulse, prompt recovery, consistent sign across a 4-by-4 tile grid,
and limited spatial-edge disagreement. An impulse that does not meet all these
conditions is left unchanged.

For a qualified delayed pulse, the assembler receives a contiguous patch
through the affected frame, but only that one frame's RGB mean is normalized
toward its immediate neighbors. Earlier frames, later frames, audio, latent
inputs, and camera geometry remain unchanged. PT228 reports the delayed
offset, signed impulse, spatial support, and action. Analyze Only remains
read-only. The existing first-frame and sustained-tone policies are preserved.

This is bounded output-space mitigation. It does not determine whether the
impulse originated in denoising, inter-grid transfer, target refinement, native
VAE decoding, or another component. It does not correct off-axis motion,
and it must not be used to claim that an isolated artistic flash or very fast
shot transition can always be distinguished from an artifact. Verification
with rendered checkpoint media remains necessary before release.


## Sustained-tone correction end-of-support

Auto 3 uses an independently fitted RGB affine transform on static-background
correspondences. Loss of matched background or an unreliable color fit can stop
the correction well before the 96-frame cap, even inside a continuous shot.
Previously, such an early termination left a non-identity transform active on
the last supported frame and then abruptly returned to the original decoded
output. This created an additional artificial exposure edge.

The estimator now records `support_stop_reason`, `release_to_identity`,
`release_frames`, and `first_uncorrected_frame` in PT232. On a non-scene-cut
termination with subsequent decoded frames, the last up-to-16 supported frames
taper the affine gain and bias to the **exact identity**, so the next raw
frame is not preceded by a corrected frame. The existing release at the
96-frame cap remains; a qualified explicit scene cut still terminates without
carrying a transform into another shot. An end-of-chunk without a following
frame requires no artificial release.

This enforces photometric operator continuity at the correction boundary;
it does not guarantee that the underlying generated frames are tonally
continuous, and it can uncover a gradual residual darkening as it releases.
The static-background mask may lose support as the camera moves or objects
enter the scene; no unverified transform is extrapolated across those changes.
An actual checkpoint-media comparison is required to establish perceptual
quality and should use the same pre-assembled decoded input with and without
Auto 3; independently rerunning diffusion confounds the comparison.

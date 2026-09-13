# Physical timeline prompt transport design

Status: investigation checkpoint; specification incomplete, not an implementation authorization or promotion claim.

## Source provenance

Audited main: a5b8943844594545301b20d01af5d9e3fa38ae29. PR #20 head: ccf50cddf83124707b866e4df3956e85d41dda4b; base bf25353d8bec44afea22c89717c4301ce13c2036. Main adds only repository naming changes. Effective overlay tree: 236c1d2b8d720a7e407656d1c4af1b134367ca20; checkpoint commit 205946859368869344af92c454e4210f783fd04b. PR #20 remains unchanged. Core audited at d43a5fa20c8547ff42d13232f589a06536c42b97.

## Established architectural findings

- v2/prompts.py selects the maximum-overlap section per nominal chunk, ties by source order, and discards section timing in the returned schema-1 plan. This is independently lossy before continuation overlap is considered.
- v2/sequence.py conditions each ordinary physical invocation with prompts[sequence_index], before choosing continuation geometry. PR #20 adds correct First/Last cache separation but does not change timeline transport.
- Core H3 masking keeps protected target tokens in attention; it changes row timestep and output velocity and uses sampler inpainting restoration. Protected tokens are context, not attention-excluded rows.
- Core MiniMaxH3Tokenizer presents numbered images/video/audio followed by prompt text. Bracketed second ranges have no runtime text-to-target-row routing. Timed text remains learned guidance. Structural timing adherence cannot be promised.
- Native exact 7-second geometry is initial 175 frames; 39-frame overlap; next physical start 136; final target shape rounds 200 up to 209 frames; global end 345. Requested output is 336 frames.
- New design must preserve a global source representation and compile only after actual geometry is resolved, without changing H3 calls or native geometry. A matched media gate is required before promotion.
- Run Storage eager nominal hashes and explicit Session prefix checks both need physical-conditioning identity. A global salt would invalidate unrelated initial chunks unnecessarily.

## Evidence boundary

The 00417 media findings are supplied in the task and corroborated by Flow PR #32's current description at 1c8689c68ebb0cf2cf87a7adc7e898c058bf9c5c; raw workflow, metrics and decoded media were not supplied to this investigation. No claim of independently decoded verification is made. Current Core source is not proof of the user's installed Core SHA.

# MiniMax H3 Keyless compatibility

Continuum's sampling path is model-opaque with respect to MiniMax H3 main-block attention. It owns physical prompt/conditioning transport, PackedLayout/reference repair, continuation latents and masks, per-chunk MODEL cloning, sampler invocation, and external decode/assembly metadata. It does not project Q/K/V and does not require a main-block `qkv_proj`.

For canonical `h3_keyless_core50_v1`, the 50 main blocks may therefore expose only `qv_proj` plus value-derived routing while the two token-refiner blocks retain native QKV. Continuum's H3 compatibility check is intentionally structural: it requires the H3 layout/latent/rope-facing model API needed by Continuum rather than a physical K projection.

Per-chunk cloning shallow-copies `transformer_options` and adds only the invocation-local `h3_continuum_interop_v1` request. Existing Keyless provider, routing-preprocessor, value/query/routing-position-domain, mask, measure, exact-block, backend-history, and optimized-attention objects remain owned by their producer. Continuum does not reinterpret V as K, create a compatibility K tensor, or replace Keyless attention modules.

Run Storage already observes the qualified diffusion-model type, wrappers, patches, transformer options, model size/dtype, and bounded model-weight probes. The legacy session fingerprint also includes the diffusion-model type and accelerator/wrapper topology. This compatibility work does not weaken those existing reuse boundaries.

These tests establish structural composition only. They do not establish decoded audiovisual parity for a trained Keyless checkpoint, Keyless+Spectrum forecast quality, native fused Sol/VDN execution, or performance. Those remain runtime/media gates.

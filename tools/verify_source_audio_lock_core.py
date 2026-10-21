"""Exercise Core audio locking and attention with an untrained tiny H3 on CPU.

This verifies structural information flow, including the packed audio schedule
scale. It cannot measure trained-model lip synchronization or GPU performance.
Run against the installed ComfyUI source using --comfy-root.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import torch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comfy-root', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.comfy_root.resolve()))
    import comfy.cli_args
    comfy.cli_args.args.cpu = True
    import comfy.ops
    import comfy.utils
    import comfy.latent_formats
    import comfy.nested_tensor
    from comfy.ldm.minimax.model import MiniMaxH3Model, pack_audio
    from comfy.model_base import MiniMaxH3
    from comfy.samplers import KSamplerX0Inpaint
    from comfy.model_sampling import CONST

    root = Path(__file__).resolve().parents[1]
    name = 'ComfyUI_H3_Continuum_Join'
    spec = importlib.util.spec_from_file_location(name, root/'__init__.py', submodule_search_locations=[str(root)])
    sys.modules[name] = importlib.util.module_from_spec(spec)
    from ComfyUI_H3_Continuum_Join.v2 import sequence  # initialize the normal runtime entry
    from ComfyUI_H3_Continuum_Join.source_audio_lock import apply_source_audio_lock

    with torch.random.fork_rng(devices=[]), torch.inference_mode():
        torch.manual_seed(791)
        dit = MiniMaxH3Model(hidden_size=8, num_layers=1, token_refiner_num_layers=0,
            num_attention_heads=1, attention_head_dim=8, ffn_hidden_size=16,
            latents_dim=24, audio_latents_dim=32, text_dim=8,
            timestep_input_dim=8, time_embed_hidden_size=8, time_embed_dim=8,
            rope_inv_freq_len=1, device='cpu', dtype=torch.float32, operations=comfy.ops.manual_cast)
        for key, value in dit.named_parameters():
            value.normal_(0, .15)
            if 'norm.weight' in key:
                value.fill_(1)
        dit.requires_grad_(False)
        dit.rope.inv_freq.fill_(.1)
        video = torch.randn(1,24,2,4,4)
        target = torch.randn(1,32,2,7)
        text = torch.randn(1,3,8)
        shapes = [video.shape, target.shape]
        native = MiniMaxH3.__new__(MiniMaxH3)
        torch.nn.Module.__init__(native)
        native.latent_format = comfy.latent_formats.MiniMaxH3AV()
        native.diffusion_model = dit
        native.latent_shapes = shapes
        projected = []
        times, segments = [], []
        handle = dit.audio_patch_proj.register_forward_pre_hook(
            lambda _module, inputs: projected.append(inputs[0].clone()))
        time_handle = dit.time_embedder.register_forward_pre_hook(
            lambda _module, inputs: times.append(inputs[0].clone()))
        segment_handle = dit.blocks[0].register_forward_pre_hook(
            lambda _module, inputs: segments.append(inputs[2]))
        try:
            for scale in (1., 4.):
                native.model_sampling = SimpleNamespace(audio_scale=scale, shift=12., audio_shift=3.)
                raw = {'samples': comfy.nested_tensor.NestedTensor((video.clone(),torch.zeros_like(target)))}
                locked = apply_source_audio_lock(raw,target)
                # Preserve a video prefix as well to exercise composition.
                vm,am = locked['noise_mask'].unbind()
                vm[:,:,:1] = 0
                masks = [comfy.sampler_helpers.prepare_mask(m,shape,torch.device("cpu")) for m,shape in zip((vm,am),shapes)]
                mask = comfy.utils.pack_latents(masks)[0]
                clean = comfy.utils.pack_latents(locked['samples'].unbind())[0]
                clean = native.process_latent_in(clean)
                values = native._denoise_mask_values(mask,shapes)
                assert values['audio_denoise_mask'].count_nonzero() == 0
                assert values['denoise_mask'].min() == 0 and values['denoise_mask'].max() == 1

                class Bridge:
                    inner_model = native
                    def __call__(self,x,sigma,**kwargs):
                        streams = comfy.utils.unpack_latents(x,shapes)
                        v = dit(streams,sigma*1000,text,transformer_options={},minimax_payload={'audio_scale':scale},**values)
                        return CONST.calculate_denoised(None,sigma,comfy.utils.pack_latents(v)[0],x)
                bridge = Bridge()
                wrapper = KSamplerX0Inpaint(bridge,torch.tensor([1.,.5,0.]))
                wrapper.latent_image = clean
                wrapper.noise = torch.randn_like(clean)
                outputs=[]
                for sigma in (1.,.75,.25,.01):
                    projected.clear(); times.clear(); segments.clear()
                    result = wrapper(torch.randn_like(clean),torch.tensor([sigma]),mask)
                    # Actual H3 audio embed input is the source at every sigma,
                    # after Core's inverse carried-audio schedule conversion.
                    torch.testing.assert_close(projected[-1],pack_audio(target),rtol=2e-6,atol=2e-6)
                    audio_segment = next(s for s in segments[-1] if s[0] == text.shape[1])
                    assert float(times[-1][audio_segment[2]//3]) == 1.0
                    rv,ra = comfy.utils.unpack_latents(result,shapes)
                    torch.testing.assert_close(ra,target*scale,rtol=0,atol=0)
                    outputs.append(rv)
                # Direct paired forward uses actual dense joint attention. A
                # source change must reach generated video even with audio velocity masked.
                def predict(audio):
                    return dit([video,audio],torch.tensor([500.]),text,
                        minimax_payload={'audio_scale':1.},audio_denoise_mask=torch.zeros(1,1,2,7))[0]
                assert not torch.allclose(predict(target),predict(target+1),rtol=1e-6,atol=1e-7)
                assert not torch.equal(outputs[0][:,:,1:],video[:,:,1:])
        finally:
            handle.remove()
            time_handle.remove()
            segment_handle.remove()
    print('PASS: actual Core native masks and KSamplerX0Inpaint; source audio enters joint H3 attention at every tested sigma; full audio preserved; video remains generatable; audio scales 1 and 4. Untrained CPU evidence only.')


if __name__ == '__main__':
    main()

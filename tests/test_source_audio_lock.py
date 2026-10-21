"""Source clocks, complete audio masks, and the actual native sequence boundary."""
from types import SimpleNamespace
from fractions import Fraction
import copy
import sys
from types import ModuleType

import pytest
import torch

# Import the normal runtime entry first (the package has legacy eager V2 imports).
from ComfyUI_H3_Continuum_Join.v2 import sequence
from ComfyUI_H3_Continuum_Join.source_audio_lock import (
    prepare_locked_source, encode_locked_window, apply_source_audio_lock,
)
from ComfyUI_H3_Continuum_Join.timeline.model import blank_project, compile_project, validate
from ComfyUI_H3_Continuum_Join.timeline.runtime import TimelineInputs
from ComfyUI_H3_Continuum_Join.masked_continuation import (
    CONTINUATION_NATIVE_MASKED, continuation_method_scope,
)
from .test_native_masked_sequence import _install_common_runtime, Nested


class AudioVAE:
    audio_sample_rate = 32000

    def __init__(self):
        self.inputs = []

    def encode(self, pcm):
        self.inputs.append(pcm.clone())
        # Preserve physical-window sample phase in a deterministic encode oracle.
        ticks = pcm.shape[1] // 800
        return pcm.movedim(-1, 1).reshape(1, 2, ticks, 800).mean(-1)[:, None].expand(1, 32, 2, ticks).clone()


def stereo(frames, rate=32000):
    n = round(Fraction(frames*rate, 24))
    return {"waveform": torch.linspace(-.9, .9, n).expand(1, 2, n).clone(), "sample_rate": rate}


def install_native(monkeypatch):
    import ComfyUI_H3_Continuum_Join.masked_continuation as native
    monkeypatch.setattr(native, "require_native_mask_support", lambda: None)
    comfy = sys.modules.get("comfy") or ModuleType("comfy")
    nested = ModuleType("comfy.nested_tensor")
    nested.NestedTensor = Nested
    monkeypatch.setitem(sys.modules, "comfy", comfy)
    monkeypatch.setitem(sys.modules, "comfy.nested_tensor", nested)
    monkeypatch.setattr(comfy, "nested_tensor", nested, raising=False)


@pytest.mark.parametrize("start,frames", [(0,175),(153,192),(305,209),(8,175),(17,124)])
def test_exact_pcm_phase_matches_target_at_fractional_audio_tick_boundaries(start, frames):
    vae = AudioVAE()
    audio = stereo(600)
    original = audio['waveform'].clone()
    source = prepare_locked_source(audio, vae, start_frame=0, end_frame=634, required_end_frame=600)
    z = encode_locked_window(source, vae, start_frame=start, total_frames=frames)
    assert z.shape == (1,32,2,round(Fraction(frames*40,24)))
    sample_start = round(Fraction(start*32000,24))
    assert vae.inputs[-1][0,0,0] == original[0,0,sample_start]
    torch.testing.assert_close(audio['waveform'], original, rtol=0, atol=0)
    source.resampled_waveform.fill_(0)
    torch.testing.assert_close(audio['waveform'], original, rtol=0, atol=0)


def test_native_overhang_is_encoded_silence_not_zero_latent_or_time_stretch():
    vae = AudioVAE()
    audio = stereo(168)
    source = prepare_locked_source(audio, vae, start_frame=0, end_frame=202, required_end_frame=168)
    z = encode_locked_window(source, vae, start_frame=0, total_frames=175)
    assert z.shape[-1] == 292
    pcm = vae.inputs[0].movedim(-1,1)
    n = audio['waveform'].shape[-1]
    torch.testing.assert_close(pcm[...,:n], audio['waveform'], rtol=0, atol=0)
    assert torch.count_nonzero(pcm[...,n:]) == 0


@pytest.mark.parametrize("problem", ["missing", "short", "nan", "mono"])
def test_invalid_source_cannot_silently_shift_or_pad_authored_audio(problem):
    audio = stereo(168)
    if problem == 'missing': audio = None
    if problem == 'short': audio['waveform'] = audio['waveform'][...,:-1]
    if problem == 'nan': audio['waveform'][...,0] = float('nan')
    if problem == 'mono': audio['waveform'] = audio['waveform'][:,:1]
    with pytest.raises(ValueError, match='Source Audio Locked'):
        prepare_locked_source(audio, AudioVAE(), start_frame=0, end_frame=202, required_end_frame=168)


def test_full_audio_mask_preserves_video_prefix_and_source_storage(monkeypatch):
    install_native(monkeypatch)
    video = torch.randn(1,24,52,4,4)
    audio = torch.zeros(1,32,2,292)
    vm = torch.ones(1,1,52,4,4); vm[:,:,:7] = 0
    source = torch.randn_like(audio)
    before = source.clone()
    latent = {'samples': Nested([video,audio]), 'noise_mask': Nested([vm,torch.ones(1,1,2,292)])}
    result = apply_source_audio_lock(latent, source)
    assert result['noise_mask'].unbind()[0] is vm
    assert torch.count_nonzero(result['noise_mask'].unbind()[1]) == 0
    assert audio.data_ptr() != source.data_ptr()
    torch.testing.assert_close(audio, before, rtol=0, atol=0)
    audio.add_(3)
    torch.testing.assert_close(source, before, rtol=0, atol=0)
    with pytest.raises(ValueError, match='exactly match'):
        apply_source_audio_lock(latent, source[...,:-1])


def test_lock_reuses_existing_core_capability_failure(monkeypatch):
    import ComfyUI_H3_Continuum_Join.masked_continuation as native
    monkeypatch.setattr(native, 'native_mask_support_issues', lambda: ['missing native audio mask'])
    with pytest.raises(RuntimeError, match='PR #15375'):
        apply_source_audio_lock({}, torch.empty(0))


def test_modes_change_sampling_identity_but_final_audio_is_independent():
    p = blank_project()
    legacy = copy.deepcopy(p); legacy.pop('audio_generation_mode')
    base = compile_project(p)['jobs'][0]['windows'][0]['generation_hash']
    assert compile_project(legacy)['jobs'][0]['windows'][0]['generation_hash'] == base
    hashes = {base}
    for mode in ('source_locked','generated'):
        p['audio_generation_mode'] = mode
        value = compile_project(p)['jobs'][0]['windows'][0]['generation_hash']
        hashes.add(value)
        p['final_audio_source'] = 'generated'
        assert compile_project(p)['jobs'][0]['windows'][0]['generation_hash'] == value
        p['final_audio_source'] = 'original'
    assert len(hashes) == 3
    p['audio_generation_mode'] = 'invalid'
    with pytest.raises(ValueError, match='audio generation mode'): validate(p)


def test_resumed_job_reads_source_prefix_before_job_origin():
    p = blank_project(); p['audio_generation_mode'] = 'source_locked'
    job = compile_project(p)['jobs'][0]
    provider = TimelineInputs(p,job,None)
    provider.driving_audio = stereo(900)
    provider.set_offset(175)
    vae = AudioVAE(); source = provider.audio_source(vae)
    provider.locked_audio_window(source, vae, retained_frames=0, trim_frames=22, total_frames=192)
    first_sample = round(Fraction((175-22)*32000,24))
    assert vae.inputs[-1][0,0,0] == provider.driving_audio['waveform'][0,0,first_sample]
    assert provider.lock_source_origin == 136


@pytest.mark.parametrize('method', ['native', 'guide'])
def test_three_chunk_sequence_presents_locked_target_before_every_sampler(monkeypatch, method):
    kwargs = _install_common_runtime(monkeypatch, [])
    install_native(monkeypatch)
    p = blank_project(); p.update(width=96,height=64,audio_generation_mode='source_locked')
    job = compile_project(p)['jobs'][0]
    provider = TimelineInputs(p,job,None)
    provider.driving_audio = stereo(504)
    vae = AudioVAE(); source = provider.audio_source(vae)
    provider.materialize = lambda *a,**k: (None,None,None,None,None)
    provider.checkpoint = lambda *a: None
    kwargs.update(timeline_inputs=provider, driving_audio_source=source, driving_audio_vae=vae)
    kwargs['prompt_plan']['chunks'] = 3
    from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan
    kwargs['prompt_plan'] = make_prompt_plan(mode='Fixed',script='dialogue',chunks=3,chunk_seconds=7.0)
    seen=[]
    def sample(**kw):
        video,audio = kw['latent']['samples'].unbind()
        vm,am = kw['latent']['noise_mask'].unbind()
        assert am.count_nonzero() == 0
        assert vm.max() == 1
        if seen and method == 'native': assert vm.min() == 0
        assert not any(kf.get('audio_latent') is not None for _,md in kw['conditioning'] for kf in md.get('minimax_keyframes',[]))
        # Compare the complete target at sampler entry, not merely output masks.
        expected = vae.encode(vae.inputs[-1])
        torch.testing.assert_close(audio, expected)
        seen.append(audio.clone()); video.fill_(len(seen))
        return kw['latent']
    monkeypatch.setattr(sequence, 'sample_chunk', sample)
    if method == 'native':
        with continuation_method_scope(CONTINUATION_NATIVE_MASKED): sequence.run_sequence(**kwargs)
    else: sequence.run_sequence(**kwargs)
    assert len(seen) == 3
    assert not torch.equal(seen[0], seen[1][...,:seen[0].shape[-1]])


def test_generated_mode_suppresses_driving_without_changing_source():
    p=blank_project(); p['audio_generation_mode']='generated'
    provider=TimelineInputs(p,compile_project(p)['jobs'][0],None)
    provider.driving_audio=stereo(504)
    assert provider.audio_source(AudioVAE()) is None
    assert not provider.guide_active(0)


def test_fractional_source_origin_uses_absolute_sample_boundaries():
    vae=AudioVAE(); audio=stereo(600)
    source=prepare_locked_source(audio,vae,start_frame=1,end_frame=634,required_end_frame=600)
    encode_locked_window(source,vae,start_frame=1,total_frames=175,origin_frame=1)
    expected=round(Fraction(2*32000,24))
    assert vae.inputs[-1][0,0,0] == audio['waveform'][0,0,expected]


def test_refine_transport_retains_full_audio_mask(monkeypatch):
    install_native(monkeypatch)
    from ComfyUI_H3_Continuum_Join.v3.driving_nodes import _attach_refine_masks
    mask=Nested([torch.ones(1,1,52,4,4),torch.zeros(1,1,2,292)])
    videos=[{'samples':torch.zeros(1,24,52,4,4)}]
    audios=[{'samples':torch.randn(1,32,2,292)}]
    _,out=_attach_refine_masks(enabled=True,captured=[{'noise_mask':mask}],video_latents=videos,audio_latents=audios)
    assert out[0]['noise_mask'].shape==(1,1,2,292)
    assert out[0]['noise_mask'].count_nonzero()==0
    assert out[0]['noise_mask'].data_ptr()!=mask.unbind()[1].data_ptr()


def test_source_outside_authored_scene_cannot_leak_into_native_overhang():
    vae=AudioVAE();p=blank_project()
    p.update(audio_generation_mode='source_locked',duration_frames=168,selection=[0,168])
    provider=TimelineInputs(p,compile_project(p)['jobs'][0],None)
    provider.driving_audio=stereo(504)
    source=provider.audio_source(vae)
    provider.locked_audio_window(source,vae,retained_frames=0,trim_frames=0,total_frames=175)
    assert vae.inputs[-1][:,224000:].count_nonzero()==0


def test_output_masks_are_independent_and_preserve_latent_values_and_metadata():
    from ComfyUI_H3_Continuum_Join.source_audio_lock import attach_locked_audio_output_masks
    samples=torch.randn(1,32,2,17,dtype=torch.float64)
    source={'samples':samples,'noise_mask':torch.ones(1,1,2,17),'marker':'kept'}
    before=samples.clone()
    out=attach_locked_audio_output_masks([source,{'samples':samples[...,:5]}])
    assert out[0] is not source and out[0]['marker']=='kept'
    assert out[0]['samples'] is samples
    assert source['noise_mask'].min()==1
    assert out[0]['noise_mask'].shape==(1,1,2,17)
    assert out[1]['noise_mask'].shape==(1,1,2,5)
    out[0]['noise_mask'].fill_(1)
    assert out[1]['noise_mask'].count_nonzero()==0
    torch.testing.assert_close(samples,before,rtol=0,atol=0)

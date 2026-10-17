"""Exercise the actual native sequence loop with deterministic CPU sample fixtures."""
from types import SimpleNamespace
import torch
from ComfyUI_H3_Continuum_Join.constants import DIAGNOSTICS_BASIC,V2_CONTINUITY_OPTIONS
from ComfyUI_H3_Continuum_Join.v2 import sequence
from ComfyUI_H3_Continuum_Join.v2.h3_builder import IdentityAssets
from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan
from ComfyUI_H3_Continuum_Join.temporal import video_latent_t,audio_latent_t
from ComfyUI_H3_Continuum_Join.timeline.model import blank_project,compile_project,validate,digest
from ComfyUI_H3_Continuum_Join.timeline.runtime import TimelineInputs
from ComfyUI_H3_Continuum_Join.reference import ReferenceAssets
from ComfyUI_H3_Continuum_Join.masked_continuation import CONTINUATION_GUIDE,continuation_method_scope

class Nested:
    def __init__(self,parts):self.parts=parts
    def unbind(self):return self.parts

class Clip:
    def __init__(self):self.inputs=[]
    def tokenize(self,text,**kwargs):self.inputs.append((text,kwargs));return text
    def encode_from_tokens_scheduled(self,tokens):return [[torch.zeros(1,2,3),{}]]


def test_references_materialize_at_actual_invocation_and_invalidate_suffix(monkeypatch):
    p=blank_project();p.update(width=64,height=64)
    windows=compile_project(p)['jobs'][0]
    provider=TimelineInputs(p,windows,None)
    calls=[]
    def materialize(index,**kwargs):
        calls.append(index)
        value=torch.full((1,64,64,3),float(index))
        refs=ReferenceAssets((value,),(torch.zeros(1,24,2,4,4),),(digest(index),),digest(index),'Match Output')
        return refs,None,None,None,None
    provider.materialize=materialize;provider.checkpoint=lambda *a:None
    assets=IdentityAssets(None,None,None,None,'none')
    monkeypatch.setattr(sequence,'prepare_identity_assets',lambda *a,**k:assets)
    monkeypatch.setattr(sequence,'encode_identity_latents',lambda *a,**k:assets)
    monkeypatch.setattr(sequence,'check_comfy_h3_runtime',lambda:[])
    monkeypatch.setattr(sequence,'accelerator_summary',lambda _: 'CPU test')
    monkeypatch.setattr(sequence,'clone_model_for_chunk',lambda model,**k:model)
    def empty(w,h,n):return {'samples':Nested([torch.zeros(1,24,video_latent_t(n),h//16,w//16),torch.zeros(1,32,2,audio_latent_t(n))])}
    monkeypatch.setattr(sequence,'empty_h3_latent',empty)
    monkeypatch.setattr(sequence,'sample_chunk',lambda **kwargs:kwargs['latent'])
    model=SimpleNamespace(model=SimpleNamespace(diffusion_model=SimpleNamespace()),model_options={},wrappers={},model_dtype=lambda:torch.float32,model_size=lambda:0)
    clip=Clip();plan=make_prompt_plan(mode='Fixed',script='same text',chunks=3,chunk_seconds=7)
    args=dict(model=model,clip=clip,video_vae=None,audio_vae=None,sampler=None,sigmas=torch.tensor([1.,0.]),first_frame=None,last_frame=None,prompt_plan=plan,width=64,height=64,continuity=V2_CONTINUITY_OPTIONS[1],base_seed=1,audio_continuity=True,exact_total_duration=False,diagnostics_mode=DIAGNOSTICS_BASIC,reroll_from_chunk=0,reroll_nonce=0,strict_compatibility=False,debug=False,latent_only=True,timeline_inputs=provider)
    with continuation_method_scope(CONTINUATION_GUIDE):
        entries,_,session,_=sequence.run_sequence(**args)
    assert calls==[0,1,2]
    assert len(clip.inputs)==3
    assert [float(options['minimax_ref_items'][0]['data'][0,0,0,0]) for _,options in clip.inputs]==[0,1,2]
    assert len(provider.manifests)==3
    assert provider.manifests[0]['descriptor']['total_frames']==175
    assert provider.manifests[1]['window']['range']==[168,336]
    calls.clear();provider.manifests=[]
    provider.windows[1]['generation_hash']=digest('changed reference')
    with continuation_method_scope(CONTINUATION_GUIDE):
        entries,_,_,_=sequence.run_sequence(**args,session=session)
    assert calls==[1,2]
    assert entries[0]['reused'] is True


def test_final_audio_only_changes_do_not_change_sampling_hash():
    from .test_timeline_model import project
    from ComfyUI_H3_Continuum_Join.timeline.model import compile_window
    p=project();c=p['tracks'][0]['clips'][0];c['audio_role']='final'
    a=compile_window(p,0)
    c['gain']='0.2';c['fade_out']='1'
    b=compile_window(p,0)
    assert a['generation_hash']==b['generation_hash']
    assert a['final_hash']!=b['final_hash']

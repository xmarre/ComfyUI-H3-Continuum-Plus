"""Compact project node, native sampler facade, take commit and export nodes."""
from __future__ import annotations

import copy
import time
import json
from pathlib import Path
import tempfile
import uuid

import numpy as np
import torch

from .media import ProjectStore, check_cancel, run_process
from .model import blank_project, compile_project, digest
from .runtime import TimelineInputs

CATEGORY = "MiniMax H3/Continuum/Timeline Studio"


def project_store():
    import folder_paths
    return ProjectStore(Path(folder_paths.get_input_directory()) / "h3_timeline_studio")


def takes(store, project_id):
    directory = store.project_dir(project_id)
    return sorted([json.loads(p.read_text()) for p in directory.glob("take-*.json")],key=lambda r:r.get('created_ns',0))


def take_path(store, project_id, take_id):
    if len(take_id) != 32 or any(c not in '0123456789abcdef' for c in take_id):
        raise ValueError("Invalid take identifier")
    return store.project_dir(project_id) / f"take-{take_id}.json"


def stale_take(p, record, records):
    from .model import compile_window
    for index, old in zip(record["indices"], record["input_hashes"]):
        if compile_window(p,index)["generation_hash"] != old:
            return True
    parent = record.get("parent_take")
    if parent:
        by_id = {r["id"]:r for r in records}
        if parent not in by_id or stale_take(p,by_id[parent],[r for r in records if r["id"]!=record["id"]]):
            return True
        # A newly selected alternative changes the causal parent identity.
        chosen = p.get("accepted",{}).get(str(record["job_index"]-1))
        if chosen and chosen != parent:
            return True
    return False


class H3ContinuumTimelineStudio:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"project_json": ("STRING", {"default": json.dumps(blank_project()), "multiline": True})}}
    RETURN_TYPES = ("H3_CONTINUUM_TIMELINE_PROJECT", "STRING")
    RETURN_NAMES = ("timeline", "input_report")
    FUNCTION = "compile"
    CATEGORY = CATEGORY
    DESCRIPTION = "Open Timeline Studio to edit retained footage, per-window prompts, references and audio. The project owns duration, dimensions and chunk length."

    def compile(self, project_json):
        plan = compile_project(json.loads(project_json))
        return plan, json.dumps({k:v for k,v in plan.items() if k!='project'},indent=2)


class H3ContinuumTimelineSampler:
    @classmethod
    def INPUT_TYPES(cls):
        from ..v3.driving_nodes import V34_CONTINUITY_OPTIONS, V34_CONTINUITY_STRONG
        from ..masked_continuation import CONTINUATION_METHODS, CONTINUATION_NATIVE_MASKED
        return {"required": {"timeline": ("H3_CONTINUUM_TIMELINE_PROJECT",), "model": ("MODEL",), "clip": ("CLIP",),
            "video_vae": ("VAE",), "audio_vae": ("VAE",), "sampler": ("SAMPLER",), "sigmas": ("SIGMAS",),
            "job_index": ("INT", {"default":0,"min":0}), "base_seed": ("INT", {"default":0,"min":0,"max":0xffffffffffffffff}),
            "continuity": (V34_CONTINUITY_OPTIONS, {"default":V34_CONTINUITY_STRONG}),
            "continuation_method": (CONTINUATION_METHODS,{"default":CONTINUATION_NATIVE_MASKED}),
            "regenerate_from": ("INT",{"default":0,"min":0,"max":16}),
            "reroll_nonce": ("INT",{"default":0,"min":0}), "emit_refine_state": ("BOOLEAN",{"default":False})},
            "optional":{"first_frame":("IMAGE",),"last_frame":("IMAGE",)},
            "hidden":{"prompt":"PROMPT","unique_id":"UNIQUE_ID"}}
    RETURN_TYPES = ("LATENT","LATENT","H3_CONTINUUM_ASSEMBLY_PLAN","H3_CONTINUUM_TIMELINE_TAKE","STRING","H3_CONTINUUM_REFINE_STATE")
    RETURN_NAMES = ("video_latents","audio_latents","assembly_plan","take","status","refine_state")
    OUTPUT_IS_LIST = (True,True,False,False,False,True)
    FUNCTION = "sample"
    CATEGORY = CATEGORY

    def sample(self,timeline,model,clip,video_vae,audio_vae,sampler,sigmas,job_index,base_seed,continuity,continuation_method,regenerate_from,reroll_nonce,emit_refine_state,first_frame=None,last_frame=None,prompt=None,unique_id=None):
        from ..v2.sequence import run_sequence
        from ..v2.prompts import make_prompt_plan
        from ..v2.session import model_fingerprint, entry_to_state
        from ..v2.session_io import load_session, save_session
        from ..v2.sampling import capture_chunk_refine_state
        from ..masked_continuation import continuation_method_scope, validate_native_masked_request
        from ..v3.driving_nodes import _normalize_v34_continuity, _attach_refine_masks, _refine_state_output
        from ..v3.plan import prepare_physical_decode_entries
        from ..constants import DIAGNOSTICS_FULL
        p = copy.deepcopy(timeline["project"])
        job = copy.deepcopy(timeline["jobs"][int(job_index)])
        store = project_store()
        for asset_id in {i['asset_id'] for w in job['windows'] for key in ('images','video','guide','voice') for i in w[key]}:
            store.assets.verify(asset_id)
        provider = TimelineInputs(p,job,store)
        guide = None
        continuity = _normalize_v34_continuity(continuity)
        validate_native_masked_request(method=continuation_method,continuity=continuity,audio_continuity=True,
                                      driving_audio_active=all(provider.guide_active(i) for i in range(len(job['windows']))),chunks=len(job['windows']))
        plan = make_prompt_plan(mode="List",script=json.dumps([w['prompt'] for w in job['windows']]),
                                chunks=len(job['windows']),chunk_seconds=p['chunk_frames']/24)
        # The existing Run Storage contract fingerprints model patches/weights,
        # CLIP, VAE, sampler and sigma bytes. Use it to qualify selected sessions.
        from ..run_storage import build_sampling_contract
        from ..driving_audio import _tensor_sha256
        contract_plan = copy.deepcopy(plan)
        contract_plan['timeline_studio']={'contract':1,'audio_guide':any(provider.guide_active(i) for i in range(len(job['windows'])))}
        contract, resume_safe, reasons = build_sampling_contract(model=model,model_fingerprint_value=model_fingerprint(model),
            clip=clip,video_vae=video_vae,sampler=sampler,sigmas=sigmas,prompt_plan=contract_plan,
            width=p['width'],height=p['height'],chunk_seconds=p['chunk_frames']/24,continuity=continuity,
            audio_continuity=True,base_seed=int(base_seed),reroll_from_chunk=0,reroll_nonce=0,
            first_frame_hash=_tensor_sha256(first_frame) if first_frame is not None else 'none',
            last_frame_hash=_tensor_sha256(last_frame) if last_frame is not None else 'none',strict_compatibility=False,
            driving_audio_vae=audio_vae)
        settings_hash=digest([contract['global'],continuation_method,job['start_chunk']])
        all_takes=takes(store,p['project_id'])
        candidates=[r for r in all_takes if r['job_index']==job_index and r['settings_hash']==settings_hash]
        chosen=p.get('accepted',{}).get(str(job_index))
        previous=next((r for r in candidates if r['id']==chosen),candidates[-1] if candidates else None)
        session=None;initial=None;parent=None
        # Native Run Storage authenticates the selected revision's prefix.
        if job['requires_previous_job']:
            parent_id=p.get('accepted',{}).get(str(job_index-1))
            parents=[r for r in all_takes if r['job_index']==job_index-1 and r.get('status')=='sampled' and not stale_take(p,r,all_takes)]
            parent=next((r for r in parents if r['id']==parent_id),parents[-1] if parents else None)
            if parent is None:
                raise ValueError("This job needs the preceding job's valid continuation state; render jobs in order")
            if previous and previous.get('parent_take')!=parent['id']:
                session=None
            if session is None:
                initial=entry_to_state(load_session(prefix=parent['session_prefix'],slot=1)['chunks'][-1])
        provider.offset = parent.get('output_range',parent['range'])[1] if parent else job['range'][0]
        guide = provider.audio_source(audio_vae)
        take_id=uuid.uuid4().hex
        prefix='timeline_'+p['project_id']+'_'+take_id
        record={'id':take_id,'project_id':p['project_id'],'job_index':int(job_index),'range':job['range'],
                'indices':[w['index'] for w in job['windows']], 'input_hashes':provider.hashes,
                'settings_hash':settings_hash,'parent_take':parent['id'] if parent else None,
                'output_start':provider.offset,'created_ns':time.time_ns(),'session_prefix':prefix,'snapshot_hash':timeline['snapshot_hash'],'status':'sampled',
                'operation':p['operation'],'selection':p['selection'],'project_snapshot':p}
        from ..run_storage import run_storage_scope
        storage_name = 'timeline_'+p['project_id']+'_'+str(job['start_chunk'])+'_'+(parent['id'] if parent else 'origin')
        from contextlib import nullcontext
        capture = capture_chunk_refine_state() if emit_refine_state else nullcontext([])
        with run_storage_scope(storage_name,prompt=prompt,unique_id=unique_id) as storage:
            if previous and previous.get('storage_name') == storage_name:
                storage.preferred_revision = previous.get('revision_id')
            def checkpoint(entries,identity_hash,first_frame_hash,last_frame_hash):
                partial_record={**record,'status':'partial','storage_name':storage_name,'revision_id':storage.revision_id}
                store.atomic_json(take_path(store,p['project_id'],take_id),partial_record)
            provider.checkpoint=checkpoint
            with continuation_method_scope(continuation_method),capture as captured:
                entries,last_state,new_session,report=run_sequence(model=model,clip=clip,video_vae=video_vae,audio_vae=None,
                    sampler=sampler,sigmas=sigmas,first_frame=first_frame,last_frame=last_frame,prompt_plan=plan,
                    width=p['width'],height=p['height'],continuity=continuity,base_seed=int(base_seed)+job['start_chunk'],
                    audio_continuity=True,exact_total_duration=False,diagnostics_mode=DIAGNOSTICS_FULL,
                    reroll_from_chunk=1 if emit_refine_state else int(regenerate_from),reroll_nonce=int(reroll_nonce),strict_compatibility=False,debug=False,
                    latent_only=True,session=None,initial_state=initial,driving_audio_source=guide,driving_audio_vae=audio_vae,timeline_inputs=provider)
            storage.finalize(session=new_session,report=report)
            record.update(storage_name=storage_name,revision_id=storage.revision_id)
        save_session(new_session,prefix=prefix,slot=1)
        record['model_inputs']=(previous.get('model_inputs',[])[:sum(bool(e.get('reused')) for e in entries)] if previous else [])+provider.manifests
        record['output_range']=[provider.offset,provider.offset+sum(int(e['plan']['net_frames']) for e in entries)]
        record['reused_chunks']=sum(bool(e.get('reused')) for e in entries)
        store.atomic_json(take_path(store,p['project_id'],take_id),record)
        decode,assembly=prepare_physical_decode_entries(entries,chunk_seconds=p['chunk_frames']/24,preserve_final_frame=last_frame is not None,terminal_merged=False)
        videos=[{'samples':e['video']} for e in decode];audios=[{'samples':e['audio']} for e in decode]
        if emit_refine_state:
            videos,audios=_attach_refine_masks(enabled=True,captured=captured,video_latents=videos,audio_latents=audios)
        refine=_refine_state_output(enabled=emit_refine_state,captured=captured,video_latents=videos,debug=False)
        if reasons:report+='\nSession auto reuse disabled: '+ '; '.join(reasons)
        return videos,audios,assembly,record,report,refine


class H3ContinuumTimelineTake:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required':{'images':('IMAGE',),'audio':('AUDIO',),'take':('H3_CONTINUUM_TIMELINE_TAKE',)}}
    RETURN_TYPES=('H3_CONTINUUM_TIMELINE_TAKE','STRING')
    RETURN_NAMES=('take','status')
    FUNCTION='commit'
    CATEGORY=CATEGORY
    OUTPUT_NODE=True

    def commit(self,images,audio,take):
        from .compose import write_video_frames
        store=project_store();record=copy.deepcopy(take)
        # Exactly the requested job duration is placed into the output cut.
        n=record['output_range'][1]-record['output_range'][0]
        if images.shape[0]<n:raise ValueError('Save Take requires the natural retained timeline from the assembler; select Natural retained timeline before saving')
        with tempfile.TemporaryDirectory(prefix='h3-take-') as directory:
            d=Path(directory);video=d/'video.mkv';sound=d/'audio.f32';output=d/'take.mkv'
            write_video_frames((images[:n].detach().cpu().clamp(0,1).numpy()*255).round().astype(np.uint8),video)
            rate=int(audio['sample_rate']);pcm=audio['waveform'][0].detach().cpu().numpy()
            count=round(n*rate/24)
            if pcm.shape[-1]<count:pcm=np.pad(pcm,((0,0),(0,count-pcm.shape[-1])))
            pcm[:,:count].T.astype('<f4').tofile(sound)
            run_process(['ffmpeg','-v','error','-nostdin','-i',str(video),'-f','f32le','-ar',str(rate),'-ac',str(pcm.shape[0]),'-i',str(sound),'-c:v','copy','-c:a','pcm_f32le','-t',str(n/24),str(output)])
            with output.open('rb') as stream:asset=store.assets.register(stream,'Take '+record['id'][:8]+'.mkv')
        rendition={'take_id':record['id'],'asset':asset,'range':record['output_range']}
        path=store.project_dir(record['project_id'])/('render-'+record['id']+'.json')
        if path.exists() and json.loads(path.read_text())!=rendition:
            raise ValueError('This take already has a different rendered output; create a new take')
        store.atomic_json(path,rendition)
        return {'ui':{'text':['Take '+record['id'][:8]+' ready for review in Timeline Studio']},'result':({**record,'rendition':rendition},'Take saved')}


class H3ContinuumTimelineExport:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required':{'timeline':('H3_CONTINUUM_TIMELINE_PROJECT',),'format':(['lossless','h264'],)}}
    RETURN_TYPES=('STRING',)
    RETURN_NAMES=('output_file',)
    FUNCTION='export'
    CATEGORY=CATEGORY
    OUTPUT_NODE=True

    def export(self,timeline,format):
        from .compose import export_project
        import folder_paths
        p=copy.deepcopy(timeline['project'])
        path=Path(folder_paths.get_output_directory())/('Timeline_'+p['project_id']+'_'+uuid.uuid4().hex[:8]+('.mkv' if format=='lossless' else '.mp4'))
        return (str(export_project(p,project_store(),path,codec=format)),)


NODE_CLASS_MAPPINGS={c.__name__:c for c in (H3ContinuumTimelineStudio,H3ContinuumTimelineSampler,H3ContinuumTimelineTake,H3ContinuumTimelineExport)}
NODE_DISPLAY_NAME_MAPPINGS={c.__name__:name for c,name in ((H3ContinuumTimelineStudio,'Continuum Timeline Studio'),(H3ContinuumTimelineSampler,'Continuum Timeline Sampler'),(H3ContinuumTimelineTake,'Continuum Timeline Save Take'),(H3ContinuumTimelineExport,'Continuum Timeline Export'))}

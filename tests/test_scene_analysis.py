"""Real codec/PTS tests and compiler invariants; no GPU fidelity claim."""
from fractions import Fraction
import copy
import time
import av
import numpy as np
import pytest
from ComfyUI_H3_Continuum_Join.timeline import scene_detection as detection
from ComfyUI_H3_Continuum_Join.timeline.scene_analysis import SceneAnalysis
from ComfyUI_H3_Continuum_Join.timeline.scene_color import limited_dimensions, color_metadata, color_filter, extract_reference
from ComfyUI_H3_Continuum_Join.timeline.scene_plan import settings, edit_plan
from ComfyUI_H3_Continuum_Join.timeline.scene_semantics import validate_observation, FIELDS
from ComfyUI_H3_Continuum_Join.timeline.media import ProjectStore
from ComfyUI_H3_Continuum_Join.timeline.model import blank_project, compile_window, validate


def picture(style=0):
    y,x=np.mgrid[:96,:160]
    return np.stack([(x*3+y)%256,(y*4)%256,((x//12)%2*160+30)+np.zeros_like(y)],2).astype('uint8') if not style else np.stack([(y*2)%256,((x//18+y//13)%2)*180+20,(x+y*3)%256],2).astype('uint8')


def movie(path, images, offset=0, vfr=False):
    with av.open(str(path),'w') as c:
        s=c.add_stream('ffv1',rate=24);s.width,s.height=160,96;s.pix_fmt='bgr0';s.time_base=Fraction(1,24000)
        pts=offset*24000
        for image in images:
            f=av.VideoFrame.from_ndarray(image,format='rgb24');f.pts,f.time_base=pts,Fraction(1,24000)
            pts+=1000 if not vfr or pts%2000 else 1500
            for packet in s.encode(f):c.mux(packet)
        for packet in s.encode():c.mux(packet)
    return path


@pytest.fixture
def clip(tmp_path):
    return movie(tmp_path/'source.mkv',[picture()]*67+[picture(1)]*86+[picture()]*15)


def registered(tmp_path,clip):
    store=ProjectStore(tmp_path/'store')
    with clip.open('rb') as source:asset=store.assets.register(source,cancel=lambda:None)
    p=blank_project();p.update(duration_frames=168,selection=[0,168],width=160,height=96)
    p['assets']=[asset];p['tracks']=[{'id':'source','role':'retained','clips':[{'id':'movie','asset_id':asset['id'],'start':0,'end':168,'source_in':asset['video'].get('start_time','0'),'speed':'1','audio_role':'neither'}]}]
    return store,validate(p),asset


def measure(tmp_path,path):
    db=detection.open_measurements(tmp_path/'frames.sqlite')
    with av.open(str(path)) as c:start=Fraction(c.streams.video[0].start_time or 0)*c.streams.video[0].time_base
    summary=detection.decode_measurements(path,db,start,start+20,cancel=lambda:None,progress=lambda **_:None)
    return db,summary


def test_therapy_geometry_and_return_setup(tmp_path,clip):
    db,summary=measure(tmp_path,clip)
    events=detection.detect(db,settings(),cancel=lambda:None)
    assert [(e['type'],e['frame']) for e in events]==[('hard_cut',67),('hard_cut',153)]
    assert summary['frames']==168
    a=detection.select_candidates(db,0,67,2)[0];c=detection.select_candidates(db,153,168,2)[0]
    assert detection.setup_match(a['image'],c['image']) and 2<=a['n']<65
    db.close()


def test_flash_and_motion_false_cuts(tmp_path):
    a=picture();images=[np.roll(a,n//2,axis=1) for n in range(80)];images[30]=np.full_like(a,255)
    db,_=measure(tmp_path,movie(tmp_path/'motion.mkv',images))
    assert not [e for e in detection.detect(db,settings({'within_shot':False}),cancel=lambda:None) if e['type']=='hard_cut']
    db.close()


def test_fade_is_a_gradual_event(tmp_path):
    a=picture();images=[a]*24+[np.rint(a.astype(float)*v).astype('uint8') for v in np.linspace(1,0,24)]+[np.zeros_like(a)]*24
    db,_=measure(tmp_path,movie(tmp_path/'fade.mkv',images))
    events=detection.detect(db,settings(),cancel=lambda:None)
    assert any(e['type']=='fade' and 'transition' in e for e in events)
    assert not any(e['type']=='hard_cut' for e in events)
    db.close()


def test_vfr_offset_resume(tmp_path):
    path=movie(tmp_path/'vfr.mkv',[picture()]*90,offset=3600,vfr=True)
    db=detection.open_measurements(tmp_path/'resume.sqlite');count=0
    def cancel():
        nonlocal count
        count+=1
        if count==30:raise InterruptedError('cancel')
    with pytest.raises(InterruptedError):detection.decode_measurements(path,db,3600,3610,cancel=cancel,progress=lambda **_:None)
    assert db.execute('SELECT COUNT(*) FROM frames').fetchone()[0]>0
    result=detection.decode_measurements(path,db,3600,3610,cancel=lambda:None,progress=lambda **_:None)
    rows=list(detection.frames(db));assert len(rows)==result['frames']==90
    assert len({r['time'] for r in rows})==90 and min(r['time'] for r in rows)>=3600
    assert len({rows[i+1]['time']-r['time'] for i,r in enumerate(rows[:-1])})>1
    db.close()


def test_mp_no_upscale():
    assert limited_dimensions(1000,650,.7)==(1000,650)
    w,h=limited_dimensions(2000,1000,.7);assert w*h<=700000 and abs(w/h-2)<.004
    for bad in [float('nan'),float('inf'),0,True]:
        with pytest.raises(ValueError):limited_dimensions(1000,500,bad)


def test_color_and_dolby_fallback():
    for trc in ['bt709','smpte2084','arib-std-b67']:
        meta=color_metadata({'color_transfer':trc,'color_primaries':'bt2020','color_space':'bt2020nc','color_range':'tv','pix_fmt':'yuv420p10le'})
        f,_=color_filter(meta,settings())
        if trc=='bt709':
            assert 't=linear' not in f and 't=bt709' in f and 'tonemap=' not in f
        else:
            assert 't=linear' in f and 'npl=125' in f and 'iec61966-2-1' in f
            assert 'tonemap=' in f
    dv={'side_data_type':'DOVI configuration record','dv_profile':5,'dv_bl_signal_compatibility_id':0}
    meta=color_metadata({'side_data_list':[dv]})
    with pytest.raises(ValueError,match='profile 5'):color_filter(meta,settings())
    assert 'apply_dolbyvision=1' in color_filter(meta,settings({'tone_mapping':'libplacebo'}))[0]
    dv['dv_profile']=8;dv['dv_bl_signal_compatibility_id']=1
    assert any('base layer' in w for w in color_filter(meta,settings())[1])


def test_exact_native_extraction_and_cache(tmp_path,clip):
    store,p,asset=registered(tmp_path,clip);db,_=measure(tmp_path,clip);frame=next(detection.frames(db,70,71));db.close()
    cfg=settings({'h3_mp':.02,'vlm_mp':.01})
    ref=extract_reference(store,asset,frame,cfg,cancel=lambda:None);assets={a['representation']:a for a in ref['assets']}
    assert (assets['h3']['width'],assets['h3']['height'])==(160,96)
    assert assets['vlm']['width']*assets['vlm']['height']<=10000 and assets['sdr_master']['bit_depth']==16
    with av.open(str(store.root/'scene_references'/ref['id']/'master.mkv')) as c:raw=next(c.decode(video=0)).to_ndarray(format='rgb24')
    assert np.array_equal(raw,picture(1))
    cached=extract_reference(store,asset,frame,cfg,cancel=lambda:None);assert cached['cache_hit'] and cached['id']==ref['id']


def plan_project():
    p=blank_project();p.update(duration_frames=336,selection=[0,336],width=160,height=96)
    aid='a'*64;p['assets']=[{'id':aid,'kind':'video'},{'id':'b'*64,'kind':'image'},{'id':'c'*64,'kind':'image'}];p['tracks']=[]
    p['scene_plan']={'schema':'h3_scene_plan_v1','settings':settings(),'references':{'r1':{'asset_id':'b'*64,'source_asset_id':aid},'r2':{'asset_id':'c'*64,'source_asset_id':aid}},'shots':[
        {'id':'s1','range':[0,67],'source_asset_id':aid,'source_start':'0','source_end':'67/24','reference_ids':['r1'],'user':{}},
        {'id':'s2','range':[67,153],'source_asset_id':aid,'source_start':'67/24','source_end':'51/8','reference_ids':['r2'],'user':{}},
        {'id':'s3','range':[153,168],'source_asset_id':aid,'source_start':'51/8','source_end':'7','reference_ids':['r1'],'user':{}}]}
    return validate(p)


def test_alias_budget_identity_and_no_chunk_leak():
    p=plan_project();p['prompts']['global']='Maekar identity: <Picture 1>. <d>[English] We both do.</d>'
    w=compile_window(p,0,picture_offset=1)
    assert len(w['images'])==2 and set(w['aliases'].values())=={'<Picture 2>','<Picture 3>'}
    assert '<d>[English] We both do.</d>' in w['prompt']
    assert w['segments'] is None
    assert w['prompt'].count('<d>[English] We both do.</d>') == 1
    assert w['prompt'].count('Shot plan:') == 1
    assert compile_window(p,1,picture_offset=1)['images']==[]
    full=compile_window(p,0,picture_offset=8)
    assert full['images']==[] and 'Match <Picture' not in full['prompt'] and any('omitted' in s for s in full['warnings'])


def test_effective_hash_only():
    p=plan_project();before=compile_window(p,0,picture_offset=1)['generation_hash']
    p['scene_plan']['shots'][0].update(confidence=.1,setup_id='UI grouping')
    assert compile_window(p,0,picture_offset=1)['generation_hash']==before
    p['scene_plan']['shots'][0]['user']['description']='Verbose VLM review notes, not a prompt.'
    assert compile_window(p,0,picture_offset=1)['generation_hash']==before
    p['scene_plan']['shots'][0]['user']['prompt_cue']='Keep a medium two-shot.'
    assert compile_window(p,0,picture_offset=1)['generation_hash']!=before


def test_user_precedence_dialogue_verbatim():
    p=plan_project();p['scene_plan']['settings']['generate_descriptions']=True
    p['scene_plan']['shots'][0].update(vlm={'description':'invented wide shot'},user={
        'description':'Much longer VLM notes should not enter the prompt.',
        'prompt_cue':'Mary remains neutral.'})
    p['prompts']['regions']=[{'id':'dialogue','start':0,'end':168,'text':'S2 <d>[English] We both do.</d>\n(pause)'}]
    w=compile_window(p,0,picture_offset=1)
    assert 'invented' not in w['prompt'] and 'Mary remains neutral.' in w['prompt']
    assert 'Much longer VLM notes' not in w['prompt']
    assert w['segments'] is None
    assert w['prompt'].count('S2 <d>[English] We both do.</d>\n(pause)') == 1
    assert 'Visual shot request' not in w['prompt']


def test_vlm_schema():
    value={'version':1,'evidence_ids':['id'],'observations':dict.fromkeys(FIELDS),'subjects':[],'confidence':.7}
    assert validate_observation(value,['id'])==value
    for mutate in [lambda x:x.update(evidence_ids=['wrong']),lambda x:x['observations'].update(acting='<d>hello</d>'),lambda x:x['observations'].update(camera_movement='pan'),lambda x:x.update(confidence=float('nan'))]:
        bad=copy.deepcopy(value);mutate(bad)
        with pytest.raises(ValueError):validate_observation(bad,['id'])


def test_job_cache_mapping_user_corrections(tmp_path,clip):
    store,p,asset=registered(tmp_path,clip);service=SceneAnalysis(store);job=service.start(p,asset['id'],config={'max_per_shot':1})
    until=time.monotonic()+40
    while time.monotonic()<until:
        state=service.status(job['id'])
        if state['stage'] in {'complete','failed'}:break
        time.sleep(.05)
    assert state['stage']=='complete',state
    result=service.result(job['id']);assert [s['source_frame_range'] for s in result['shots']]==[[0,67],[67,153],[153,168]]
    assert result['shots'][0]['reference_ids']==result['shots'][2]['reference_ids']
    candidate=service.preview(p,job['id']);assert [s['range'] for s in candidate['scene_plan']['shots']]==[[0,67],[67,153],[153,168]]
    candidate['scene_plan']['shots'][0]['user']['description']='Authored correction'
    assert service.preview(candidate,job['id'])['scene_plan']['shots'][0]['user']['description']=='Authored correction'
    assert 'scene_plan' not in p and service.start(p,asset['id'],config={'max_per_shot':1})['cache_hit']
    first=candidate['scene_plan']['shots'][0];split=edit_plan(candidate,'split',first['id'],frame=24)
    assert len(split['scene_plan']['shots'])==4 and split['scene_plan']['shots'][0]['source_end']=='1'
    merged=edit_plan(split,'merge',split['scene_plan']['shots'][0]['id'],other_id=split['scene_plan']['shots'][1]['id'])
    assert merged['scene_plan']['shots'][0]['range']==[0,67]


def test_prepared_images_reach_native_reference_assets(tmp_path,clip):
    import torch
    from ComfyUI_H3_Continuum_Join.timeline.runtime import TimelineInputs, merge_references
    from ComfyUI_H3_Continuum_Join.timeline.model import compile_project
    from ComfyUI_H3_Continuum_Join.reference import ReferenceAssets
    store,p,asset=registered(tmp_path,clip)
    # Register real still pixels, then exercise the same runtime materializer
    # that actual and lookahead invocations call; no browser image path is used.
    import io
    from PIL import Image
    output=io.BytesIO();Image.fromarray(picture()).save(output,format='PNG');output.seek(0)
    still=store.assets.register(output,cancel=lambda:None);still['kind']='image';p['assets'].append(still)
    fixture=plan_project()['scene_plan'];fixture['shots']=fixture['shots'][:1]
    fixture['shots'][0].update(source_asset_id=asset['id'],reference_ids=['r1'])
    fixture['references']={'r1':{'asset_id':still['id'],'source_asset_id':asset['id']}}
    p['scene_plan']=fixture;p=validate(p)
    provider=TimelineInputs(p,compile_project(p)['jobs'][0],store)
    provider.reference_slot_sizes=(1,0,0,0,0,0,0,0);provider.set_offset(0)
    class VAE:
        calls=0
        def encode(self,image):
            self.calls+=1;return torch.zeros(1,24,1,6,10)
    vae=VAE();scoped,*_=provider.materialize(0,video_vae=vae,audio_vae=None,width=160,height=96)
    assert scoped.count==1 and scoped.images[0].shape==(1,96,160,3)
    identity=ReferenceAssets((torch.zeros(1,96,160,3),),(None,),('identity',),'identity','Match Output')
    physical=merge_references(provider.select_persistent_references(identity,0),scoped)
    assert physical.count==2 and provider.windows[0]['aliases']['scene-r1']=='<Picture 2>'
    provider.materialize(0,video_vae=vae,audio_vae=None,width=160,height=96)
    assert vae.calls==1


def test_full_source_measurements_reused_for_a_selected_range(tmp_path,clip):
    from ComfyUI_H3_Continuum_Join.timeline.scene_detection import reuse_measurements
    db,_=measure(tmp_path,clip);db.close()
    part=detection.open_measurements(tmp_path/'selected.sqlite')
    assert reuse_measurements(tmp_path/'frames.sqlite',part,Fraction(2),Fraction(4),cancel=lambda:None)
    summary=detection.decode_measurements('must-not-be-opened',part,2,4,cancel=lambda:None,progress=lambda **_:None)
    rows=list(detection.frames(part));assert len(rows)==summary['frames']
    assert all(Fraction(2)<=r['time']<4 for r in rows)
    part.close()


def test_split_correction_survives_detector_reanalysis():
    from ComfyUI_H3_Continuum_Join.timeline.scene_plan import mapped_plan
    p=plan_project();aid='a'*64
    p['tracks']=[{'id':'track','role':'retained','clips':[{'id':'movie','asset_id':aid,'start':0,'end':336,'source_in':'0','speed':'1','audio_role':'neither'}]}]
    for shot in p['scene_plan']['shots']:
        shot['mapping']={'clip_id':'movie','source_in':'0','speed':'1','clip_start':0}
    raw={'id':'analysis','source_asset_id':aid,'settings':settings(),'references':p['scene_plan']['references'],
         'shots':copy.deepcopy(p['scene_plan']['shots']),'scope':'selection','project_range':[0,336]}
    corrected=edit_plan(validate(p),'split','s1',frame=24)
    # Manual interval correction must not require an added description.
    result=mapped_plan(corrected,raw)
    assert any(s['range']==[0,24] and s['user'].get('interval_overridden') for s in result['shots'])
    assert not any(s['range'][0]<24 and 0<s['range'][1] and s['range']!=[0,24] for s in result['shots'])


@pytest.mark.parametrize('transfer',['smpte2084','arib-std-b67'])
def test_hdr_ten_bit_codec_conversion(tmp_path,transfer):
    import subprocess
    from PIL import Image
    path=tmp_path/'hdr.mkv'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=s=160x96:r=24:d=0.125','-vf',f'zscale=pin=bt709:tin=bt709:min=bt709:rin=limited:p=bt2020:t={transfer}:m=bt2020nc:npl=125,format=yuv420p10le',
                    '-c:v','ffv1','-threads','1','-color_primaries','bt2020','-color_trc',transfer,'-colorspace','bt2020nc','-color_range','tv',str(path)],check=True)
    store,p,asset=registered(tmp_path,path)
    with av.open(str(path)) as c:f=next(c.decode(video=0));frame={'pts':f.pts,'time_base':str(f.time_base)}
    ref=extract_reference(store,asset,frame,settings(),cancel=lambda:None)
    assert ref['color']['transfer']==transfer and ref['color']['pixel_format']=='yuv420p10le'
    with av.open(str(store.root/'scene_references'/ref['id']/'master.mkv')) as c:
        assert next(c.decode(video=0)).format.name=='yuv420p10le'
    h3=next(a for a in ref['assets'] if a['representation']=='h3')
    with Image.open(store.assets.path(h3['id'])) as im:
        arr=np.asarray(im)
    assert arr.std()>10 and np.mean(arr>=254)<.6


def test_model_preparation_never_rounds_reference_up():
    from ComfyUI_H3_Continuum_Join.timeline.scene_color import native_reference_dimensions
    assert native_reference_dimensions(1000,650,1344,768,'Match Output')==(992,640)
    w,h=native_reference_dimensions(2000,1000,768,512,'Match Output')
    assert w*h<=768*512 and w<=2000 and h<=1000 and w%32==h%32==0


@pytest.mark.parametrize('full_range',[False,True])
def test_sdr_range_black_and_white_are_not_crushed(tmp_path,full_range):
    from PIL import Image
    path=tmp_path/'range.mkv'
    values=np.full((3,96,160),128,dtype=np.uint8)
    values[0,:,:80]=0 if full_range else 16
    values[0,:,80:]=255 if full_range else 235
    with av.open(str(path),'w') as c:
        s=c.add_stream('ffv1',rate=24);s.width,s.height=160,96;s.pix_fmt='yuv444p'
        s.codec_context.color_range=2 if full_range else 1
        s.codec_context.colorspace=s.codec_context.color_primaries=s.codec_context.color_trc=1
        f=av.VideoFrame.from_ndarray(values,format='yuv444p');f.pts=0;f.time_base=Fraction(1,24)
        for packet in s.encode(f):c.mux(packet)
        for packet in s.encode():c.mux(packet)
    store,p,asset=registered(tmp_path,path)
    with av.open(str(path)) as c:f=next(c.decode(video=0));frame={'pts':f.pts,'time_base':str(f.time_base)}
    ref=extract_reference(store,asset,frame,settings(),cancel=lambda:None)
    assert ref['color']['range']==('pc' if full_range else 'tv')
    image=next(a for a in ref['assets'] if a['representation']=='h3')
    with Image.open(store.assets.path(image['id'])) as im:rgb=np.asarray(im)
    assert rgb[:,:70].max()<=2 and rgb[:,90:].min()>=253


def test_selected_range_uses_nearest_start_pts_with_earlier_ties(tmp_path,clip):
    from ComfyUI_H3_Continuum_Join.timeline.scene_detection import reuse_measurements
    full,_=measure(tmp_path,clip);rows=list(detection.frames(full));full.close()
    start=rows[0]['time']+Fraction(1,1_000_000)
    selected=detection.open_measurements(tmp_path/'direct.sqlite')
    detection.decode_measurements(clip,selected,start,Fraction(1),cancel=lambda:None,progress=lambda **_:None)
    actual=[r['time'] for r in detection.frames(selected)]
    assert actual[0]==rows[0]['time']
    reused=detection.open_measurements(tmp_path/'reused.sqlite')
    assert reuse_measurements(tmp_path/'frames.sqlite',reused,start,Fraction(1),cancel=lambda:None)
    assert [r['time'] for r in detection.frames(reused)]==actual
    selected.close();reused.close()


def test_manual_edit_keeps_measured_events_separate():
    p=plan_project();aid='a'*64
    p['tracks']=[{'id':'track','role':'retained','clips':[{'id':'movie','asset_id':aid,'start':0,'end':336,'source_in':'0','speed':'1','audio_role':'neither'}]}]
    for shot in p['scene_plan']['shots']:
        shot['mapping']={'clip_id':'movie','source_in':'0','speed':'1','clip_start':0}
    event={'type':'hard_cut','pts':67,'time_base':'1/24','frame':67}
    p['scene_plan']['shots'][1].update(event=event,events=[event])
    split=edit_plan(validate(p),'split','s2',frame=100)
    left,right=[s for s in split['scene_plan']['shots'] if s['range'][0] in {67,100}]
    assert left['event']==event and left['events']==[event]
    assert right['event'] is None and right['events']==[]
    merged=edit_plan(split,'merge',left['id'],other_id=right['id'])
    assert next(s for s in merged['scene_plan']['shots'] if s['range']==[67,153])['events']==[event]


def test_shot_schedule_does_not_split_timed_dialogue():
    p=plan_project()
    p['prompts']['regions']=[
        {'id':'line-1','start':0,'end':100,'text':'<d>[English] First line.</d>'},
        {'id':'line-2','start':100,'end':168,'text':'<d>[English] Second line.</d>'}]
    w=compile_window(p,0,picture_offset=1)
    assert [segment['range'] for segment in w['segments']]==[[0,100],[100,168]]
    assert w['prompt'].count('<d>[English] First line.</d>')==1
    assert w['prompt'].count('<d>[English] Second line.</d>')==1
    assert sum('Shot plan:' in segment['text'] for segment in w['segments'])==1


def test_reanalysis_preserves_other_source_and_unselected_shots():
    from ComfyUI_H3_Continuum_Join.timeline.scene_plan import mapped_plan
    p=plan_project()
    other='d'*64
    p['assets'].append({'id':other,'kind':'video'})
    foreign={'id':'other-shot','range':[170,210],'source_asset_id':other,
             'source_start':'0','source_end':'5/3','reference_ids':[],
             'user':{'description':'Keep scene B.'}}
    p['scene_plan']['shots'].append(foreign)
    p['scene_plan']['shots'][0]['user']['description']='Keep earlier source interval.'
    a='a'*64
    p['tracks']=[{'id':'track','role':'retained','clips':[{'id':'movie','asset_id':a,
        'start':0,'end':336,'source_in':'0','speed':'1','audio_role':'neither'}]}]
    raw={'id':'analysis-selection','source_asset_id':a,'settings':settings(),
         'references':p['scene_plan']['references'],
         'shots':[copy.deepcopy(p['scene_plan']['shots'][1])],
         'scope':'selection','project_range':[67,153]}
    result=mapped_plan(p,raw)
    assert next(s for s in result['shots'] if s['id']=='other-shot')==foreign
    assert next(s for s in result['shots'] if s['id']=='s1')['user']['description']=='Keep earlier source interval.'
    assert any(s['range']==[67,153] and s['source_asset_id']==a for s in result['shots'])


def test_rec709_midtone_codes_survive_reference_extraction(tmp_path):
    from PIL import Image
    path=tmp_path/'rec709_ramp.mkv'
    src=np.full((3,96,160),128,dtype=np.uint8)
    source_codes=[16,64,128,192,235]
    for index,value in enumerate(source_codes):
        src[0,:,index*32:(index+1)*32]=value
    with av.open(str(path),'w') as container:
        stream=container.add_stream('ffv1',rate=24)
        stream.width,stream.height=160,96
        stream.pix_fmt='yuv444p'
        stream.codec_context.color_range=1
        stream.codec_context.colorspace=stream.codec_context.color_primaries=stream.codec_context.color_trc=1
        frame=av.VideoFrame.from_ndarray(src,format='yuv444p')
        frame.pts=0
        frame.time_base=Fraction(1,24)
        for packet in stream.encode(frame):container.mux(packet)
        for packet in stream.encode():container.mux(packet)
    store,_,asset=registered(tmp_path,path)
    with av.open(str(path)) as container:
        frame=next(container.decode(video=0))
        point={'pts':frame.pts,'time_base':str(frame.time_base)}
    extracted=extract_reference(store,asset,point,settings(),cancel=lambda:None)
    image=next(a for a in extracted['assets'] if a['representation']=='h3')
    with Image.open(store.assets.path(image['id'])) as img:
        pixels=np.asarray(img)
    for index,value in enumerate(source_codes):
        expected=round((value-16)*255/219)
        observed=float(np.median(pixels[:,index*32+8:index*32+24,:]))
        assert abs(observed-expected)<=4,(value,observed,expected)


def test_stale_source_mapping_surfaces_warning_without_shot_conditioning():
    p=plan_project()
    p['scene_plan']['shots'][0]['mapping']={
        'clip_id':'removed-source','source_in':'0','speed':'1','clip_start':0}
    compiled=compile_window(p,0,picture_offset=1)
    assert any('no longer valid' in warning for warning in compiled['warnings'])
    from ComfyUI_H3_Continuum_Join.timeline.scene_plan import allocate
    _,shots,_=allocate(p,0,168,1,[])
    assert 's1' not in {shot['id'] for shot in shots}


def test_fixed_scene_shot_schedule_reaches_native_invocation_without_dialogue_duplication():
    """Compile success is insufficient unless the native conditioning plan sees it."""
    from ComfyUI_H3_Continuum_Join.timeline.runtime import TimelineInputs
    from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan

    p=plan_project()
    authored='S2 <d>[English] We both do.</d>'
    p['prompts']['global']=authored
    native=make_prompt_plan(mode='Fixed',script=authored,chunks=2,chunk_seconds=7)
    windows=[compile_window(p,i,picture_offset=1) for i in range(2)]
    assert windows[0]['scene_schedule'] is True
    assert windows[0]['timed_prompt'] is False
    assert windows[1]['scene_schedule'] is False

    provider=TimelineInputs(p,{'windows':windows,'range':[0,336]},None)
    physical=provider.invocation_plan(native,0)
    assert physical is not native
    assert physical['source']['kind']=='timeline'
    assert physical['prompts'][0].count(authored)==1
    assert physical['prompts'][0].count('Shot plan:')==0
    assert len(physical['source']['scene_shots'])==3
    assert provider.invocation_text(authored,0).count(authored)==1
    assert provider.invocation_text(authored,0).count('Shot plan:')==1
    assert provider.invocation_text(authored,1)==authored
    assert provider.invocation_plan(native,1) is native

    # Without Smart References, native Fixed handling is unchanged.
    bare=copy.deepcopy(p)
    bare['scene_plan']['shots']=[]
    bare_windows=[compile_window(bare,i,picture_offset=1) for i in range(2)]
    normal=TimelineInputs(bare,{'windows':bare_windows,'range':[0,336]},None)
    assert normal.invocation_plan(native,0) is native
    assert normal.invocation_text(authored,0)==authored



def test_scene_schedule_continuation_context_clock_and_legacy_nominal():
    """34 carried frames shift cuts inside the physical H3 clip, not legacy text."""
    from ComfyUI_H3_Continuum_Join.timeline.runtime import TimelineInputs
    from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan
    from ComfyUI_H3_Continuum_Join.v2.physical_prompts import (
        make_physical_sample_descriptor,
    )
    from ComfyUI_H3_Continuum_Join.v2.physical_runtime import compile_invocation_prompt

    p = plan_project()
    # Move the real 0/67/153/168 frame shot pattern to chunk 2.
    for shot in p['scene_plan']['shots']:
        shot['range'] = [value + 168 for value in shot['range']]
    authored = "S2 <d>[English] We both do.</d>"
    p['prompts']['global'] = authored
    windows = [compile_window(p, i, picture_offset=1) for i in range(2)]
    assert not windows[0]['scene_schedule']
    assert windows[1]['scene_schedule']
    assert windows[1]['prompt'].count('[2.791667-6.375s]') == 1
    assert windows[1]['prompt'].count(authored) == 1

    provider = TimelineInputs(p, {'windows': windows, 'range': [0, 336]}, None)
    native = make_prompt_plan(mode='Fixed', script=authored, chunks=2, chunk_seconds=7)
    physical = provider.invocation_plan(native, 1)
    assert physical['source']['kind'] == 'timeline'
    assert 'Shot plan:' not in physical['prompts'][1]
    assert physical['prompts'][1].count(authored) == 1

    descriptor = make_physical_sample_descriptor(
        group_id='scene-chunk-two', logical_indices=(1,),
        retained_before=168, context_frames=34, total_frames=202,
        target_duration_frames=336, continuation_method='guide',
        initial_state_origin='previous_chunk', include_first=False,
        include_last=False, presentation_contract={},
    )
    # Use the actual runtime logical scope; bare compile_physical_prompt
    # would correctly include earlier authored Timeline sections in context.
    compiled = compile_invocation_prompt(
        physical, descriptor, legacy_text=provider.invocation_text(authored, 1),
        candidate=True,
    )
    emitted = compiled.text
    assert any(d.get('code') == 'H3C-PT240' for d in compiled.diagnostics)
    assert emitted.count('Shot plan:') == 1
    assert '[1.416667-4.208333s]' in emitted  # 34/24 -> (34+67)/24
    assert '[4.208333-7.791667s]' in emitted  # (34+67)/24 -> (34+153)/24
    assert '[7.791667-8.416667s]' in emitted
    assert '[0-2.791667s]' not in emitted
    assert emitted.count(authored) == 1
    assert emitted.count('Dedicated character identity references') == 1

    # Default environment gate OFF: legacy nominal text remains chunk-local.
    nominal = compile_invocation_prompt(
        physical, descriptor, legacy_text=provider.invocation_text(authored, 1),
        candidate=False,
    ).text
    assert '[0-2.791667s]' in nominal
    assert '[1.416667-4.208333s]' not in nominal
    assert nominal.count(authored) == 1

    # The compiler is also used by the next-chunk conditioning lookahead.
    assert compile_invocation_prompt(
        provider.invocation_plan(native, 1), descriptor,
        legacy_text=provider.invocation_text(authored, 1), candidate=True,
    ).text == emitted


def test_h3_reference_jpeg_keeps_native_source_raster(tmp_path, clip):
    from PIL import Image
    store, p, asset = registered(tmp_path, clip)
    db, _ = measure(tmp_path, clip)
    frame = next(detection.frames(db, 70, 71))
    db.close()
    # Former .01 MP ceiling would shrink the 160x96 reference.
    ref = extract_reference(store, asset, frame, settings({'h3_mp': .01}), cancel=lambda: None)
    h3 = next(a for a in ref['assets'] if a['representation'] == 'h3')
    assert h3['name'] == 'h3.jpg' and h3['encoding'] == 'JPEG'
    assert h3['source_raster_unscaled'] is True
    with Image.open(store.assets.path(h3['id'])) as img:
        assert img.format == 'JPEG'
        assert img.size == (160, 96)
    assert (h3['width'], h3['height']) == (160, 96)


def test_scene_conveyor_slot_pictures_without_double_injection():
    p = plan_project()
    p['scene_plan']['conveyor_slots'] = {'r1': 4, 'r2': 6}
    p['conveyor_reference_slots'] = {'0': [1, 4, 6], '1': [1]}
    window = compile_window(
        p, 0, picture_offset=3, reference_slot_sizes=(1, 0, 0, 1, 0, 1, 0, 0)
    )
    assert window['images'] == []  # All screenshots come through connected Conveyor.
    assert window['prompt'].count('<Picture 2>') > 0
    assert window['prompt'].count('<Picture 3>') > 0
    assert window['scene_schedule'] is True
    assert 'Shot plan:' in window['prompt']
    # Duplicate slot or unknown reference identities never enter a project.
    from ComfyUI_H3_Continuum_Join.timeline.scene_plan import validate_plan
    bad = copy.deepcopy(p)
    bad['scene_plan']['conveyor_slots'] = {'r1': 4, 'r2': 4}
    with pytest.raises(ValueError):
        validate_plan(bad['scene_plan'], {a['id'] for a in bad['assets']}, bad['duration_frames'])


def test_compact_shot_cues_keep_connected_base_and_exact_picture_numbers():
    from ComfyUI_H3_Continuum_Join.timeline.model import with_external_prompt_source
    p=plan_project()
    p['prompt_source']={'mode':'connected'}
    original=(
        "Realistic live-action psychological drama. <Picture 1> defines Maekar. "
        "<Picture 2> defines Mary. <Picture 3> defines the original couch. "
        "Follow <Video 1> for camera coverage and original dialogue timing.\n\n"
        "Critical acting direction: Mary stays deliberately deadpan; "
        "Maekar is emotionally expressive. Preserve silences.\n\n"
        "[0-7s]\nMaekar challenges Mary.\n<d>[English] You want kids, right?</d>\n"
        "Mary answers neutrally.\n<d>[English] We both do.</d>\n\n"
        "[7-14s]\nMaekar continues.\n"
        "<d>[English] Unless being a professional student is considered a job these days.</d>"
    )
    connected=with_external_prompt_source(p,{'mode':'Auto','script':original,'global':''})
    p['scene_plan']['conveyor_slots']={'r1':4,'r2':5}
    connected['scene_plan']=p['scene_plan']
    connected['conveyor_reference_slots']={'0':[1,2,3,4,5],'1':[1,2,3]}
    # Persistent sockets 1/2/3 are identity/setting. Each active scene
    # reference is a single distinct socket; no internal duplicate image.
    w=compile_window(connected,0,picture_offset=5,
                     reference_slot_sizes=(1,1,1,1,1,0,0,0))
    assert w['images']==[]
    assert w['prompt'].count('<d>[English] You want kids, right?</d>')==1
    assert w['prompt'].count('Critical acting direction')==1
    assert '[0-2.791667s] Match <Picture 4> for camera framing and staging.' in w['prompt']
    assert '[2.791667-6.375s] Match <Picture 5> for camera framing and staging.' in w['prompt']
    assert 'Shot plan:' in w['prompt']
    # Connected original lines MUST still be authoritative rather than an
    # obsolete internal Studio draft. Timed second-chunk dialogue is separate.
    w2=compile_window(connected,1,picture_offset=3,
                      reference_slot_sizes=(1,1,1,0,0,0,0,0))
    assert 'Unless being a professional student' in w2['prompt']
    assert 'You want kids' not in w2['prompt']
    assert '<Picture 5>' not in w2['prompt']
    assert w2['scene_schedule'] is False


def test_long_vlm_notes_dont_pollute_physical_shot_schedule():
    p=plan_project()
    long_description=('Environment: therapy room couch. Lighting: soft. '*35)
    p['scene_plan']['settings']['generate_descriptions']=True
    p['scene_plan']['shots'][0]['vlm']={'description':long_description}
    p['scene_plan']['shots'][0]['user']={'description':long_description}
    w=compile_window(p,0,picture_offset=3)
    assert 'Environment:' not in w['prompt']
    assert len(w['scene_shots'])==3
    assert w['prompt'].count('Match <Picture')==3
    p['scene_plan']['shots'][0]['user']['prompt_cue']='Keep the camera steady.'
    w=compile_window(p,0,picture_offset=3)
    assert w['prompt'].count('Keep the camera steady.')==1
    p['scene_plan']['shots'][0]['user']['prompt_cue']='<Picture 7>'
    from ComfyUI_H3_Continuum_Join.timeline.scene_plan import validate_plan
    with pytest.raises(ValueError,match='plain-text'):
        validate_plan(p['scene_plan'],{x['id'] for x in p['assets']},p['duration_frames'])


def test_connected_state_manager_timeline_anchors_to_nonzero_scene_origin():
    from ComfyUI_H3_Continuum_Join.timeline.model import with_external_prompt_source
    p=plan_project()
    p['duration_frames']=1008
    p['chunk_origin_frame']=672
    p['selection']=[672,1008]
    p['scene_plan']['shots'][0]['range']=[672,739]
    p['scene_plan']['shots'][1]['range']=[739,825]
    p['scene_plan']['shots'][2]['range']=[825,840]
    p['prompt_source']={'mode':'connected'}
    base=("Identity Maekar <Picture 1>; Mary <Picture 2>; set <Picture 3>.\n"
          "Mary remains professionally deadpan.\n"
          "[0-7s]\n<d>[English] You want kids, right?</d>\n"
          "[7-14s]\n<d>[English] We both do.</d>")
    external=with_external_prompt_source(p,{'mode':'Auto','script':base,'global':''})
    assert external['prompts']['origin_frame']==672
    first=compile_window(external,0,picture_offset=3)
    second=compile_window(external,1,picture_offset=3)
    assert 'You want kids' in first['prompt'] and 'We both do' not in first['prompt']
    assert 'We both do' in second['prompt'] and 'You want kids' not in second['prompt']
    assert 'Mary remains professionally deadpan' in first['prompt']
    assert 'Mary remains professionally deadpan' in second['prompt']
    assert first['scene_shots'][0]['range']==[672,739]
    absolute=with_external_prompt_source(p,{'mode':'Auto','script':
          "[28-35s]\nTime is relative to source movie segment.",'global':''})
    assert 'origin_frame' not in absolute['prompts']


def test_explicit_conveyor_only_refuses_hidden_sampler_scaled_fallback():
    p=plan_project()
    p['scene_plan']['conveyor_only']=True
    plain=compile_window(p,0,picture_offset=3,
                         reference_slot_sizes=(1,1,1,0,0,0,0,0))
    assert plain['images']==[]
    assert plain['scene_schedule'] is False
    assert 'Match <Picture' not in plain['prompt']
    p['scene_plan']['conveyor_slots']={'r1':4}
    p['conveyor_reference_slots']={'0':[1,2,3,4]}
    routed=compile_window(p,0,picture_offset=4,
                          reference_slot_sizes=(1,1,1,1,0,0,0,0))
    assert routed['images']==[]
    assert routed['scene_schedule'] is True
    assert routed['prompt'].count('Match <Picture 4>')==2
    # r2 remained staged, so no fake Picture 5 instruction appears.
    assert '<Picture 5>' not in routed['prompt']
    assert any('omitted' in warning.lower() for warning in routed['warnings'])


def test_dynamic_scene_refs_are_per_chunk_and_keep_three_identity_pictures():
    """Two therapy chunks use 3+3 and 3+1 Pictures, not accumulated library IDs."""
    p = plan_project()
    p['duration_frames'] = 336
    p['selection'] = [0, 336]
    p['scene_plan']['reference_transport'] = 'streamed'
    p['scene_plan']['conveyor_only'] = True
    p['scene_plan']['conveyor_slots'] = {}
    p['scene_plan']['settings']['scene_budget'] = 5
    p['assets'] += [{'id':'d'*64,'kind':'image'},{'id':'e'*64,'kind':'image'}]
    p['scene_plan']['references']['r3'] = {'asset_id':'d'*64,'source_asset_id':'a'*64}
    p['scene_plan']['references']['r4'] = {'asset_id':'e'*64,'source_asset_id':'a'*64}
    existing = copy.deepcopy(p['scene_plan']['shots'])
    existing[0]['reference_ids'] = ['r1']
    existing[1]['reference_ids'] = ['r2']
    existing[2]['reference_ids'] = ['r3']
    # Shot 3 persists across the chunk boundary; it must not consume a new
    # reference position in chunk 2. Only new shot 4 adds one screenshot.
    existing[2]['range'] = [153,267]
    next_shot = copy.deepcopy(existing[0])
    next_shot['id'] = 'dynamic-second-chunk'
    next_shot['range'] = [267, 336]
    next_shot['reference_ids'] = ['r4']
    p['scene_plan']['shots'] = existing + [next_shot]
    p['conveyor_reference_slots'] = {'0':[1,2,3], '1':[1,2,3]}
    sizes = (1,1,1,0,0,0,0,0)
    first = compile_window(p, 0, picture_offset=3, reference_slot_sizes=sizes)
    second = compile_window(p, 1, picture_offset=3, reference_slot_sizes=sizes)
    assert len(first['images']) == 3
    assert len(second['images']) == 1
    assert len(first['images']) + 3 == 6
    assert len(second['images']) + 3 == 4
    assert '<Picture 4>' in first['prompt'] and '<Picture 6>' in first['prompt']
    assert '<Picture 7>' not in first['prompt']
    assert '<Picture 4>' in second['prompt'] and '<Picture 5>' not in second['prompt']
    assert len(second['scene_shots']) == 2
    assert second['scene_shots'][0]['range'] == [168,267]
    assert 'carried video frames' in second['scene_shots'][0]['text']
    assert '<Picture' not in second['scene_shots'][0]['text']
    assert second['scene_shots'][1]['range'] == [267,336]
    assert '<Picture 4>' in second['scene_shots'][1]['text']
    assert second['images'][0]['asset_id'] == p['scene_plan']['references']['r4']['asset_id']
    assert 'Match <Picture' in second['prompt']
    assert all(item['scene_native'] for item in first['images'])
    # Neither fixed Conveyor shelf map nor its static eight-slot capacity
    # participates in streamed shot selection.
    assert validate(p)['scene_plan']['reference_transport'] == 'streamed'


def test_streamed_scene_shot_cap_is_per_chunk_not_global_asset_count():
    p = plan_project()
    p['duration_frames'] = 168 * 20
    p['selection'] = [0, 168 * 20]
    p['scene_plan']['reference_transport'] = 'streamed'
    p['scene_plan']['conveyor_only'] = True
    p['scene_plan']['conveyor_slots'] = {}
    p['scene_plan']['settings']['scene_budget'] = 5
    p['conveyor_reference_slots'] = {str(i):[1,2,3] for i in range(20)}
    seed_shot = copy.deepcopy(p['scene_plan']['shots'][0])
    shot_records = []
    for i in range(20):
        key = f'bank-reference-{i}'
        asset_id = f'{i+10:064x}'
        p['assets'].append({'id':asset_id,'kind':'image'})
        p['scene_plan']['references'][key] = {'asset_id':asset_id,'source_asset_id':'a'*64}
        one = copy.deepcopy(seed_shot)
        one.update(id=f'bank-shot-{i}',range=[168*i,168*(i+1)],reference_ids=[key])
        shot_records.append(one)
    p['scene_plan']['shots'] = shot_records
    assert len(p['scene_plan']['references']) > 8
    for i in range(20):
        w = compile_window(p,i,picture_offset=3,reference_slot_sizes=(1,1,1,0,0,0,0,0))
        assert len(w['images']) == 1
        assert '<Picture 4>' in w['prompt']
        assert '<Picture 5>' not in w['prompt']


def test_streamed_scene_budget_clips_excess_without_overwriting_identity_refs():
    p=plan_project()
    p['scene_plan']['reference_transport'] = 'streamed'
    p['scene_plan']['conveyor_only'] = True
    p['scene_plan']['settings']['scene_budget'] = 5
    p['scene_plan']['conveyor_slots'] = {}
    # 7 identity images can only leave one scene reference despite scene_budget 5.
    w=compile_window(p,0,picture_offset=7,reference_slot_sizes=(1,1,1,1,1,1,1,0))
    assert len(w['images']) == 1
    assert len(w['images'])+7 == 8
    assert any('omitted' in msg for msg in w['warnings'])
    from ComfyUI_H3_Continuum_Join.timeline.runtime import TimelineInputs
    provider=TimelineInputs(p,{'windows':[w],'range':[0,168]},None)
    assert provider.reference_size == 'Max Identity'
    assert provider.storage_contract['scene_reference_transport'] == 'streamed'

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
    assert full['images']==[] and 'Scene composition evidence:' not in full['prompt'] and any('omitted' in s for s in full['warnings'])


def test_effective_hash_only():
    p=plan_project();before=compile_window(p,0,picture_offset=1)['generation_hash']
    p['scene_plan']['shots'][0].update(confidence=.1,setup_id='UI grouping')
    assert compile_window(p,0,picture_offset=1)['generation_hash']==before
    p['scene_plan']['shots'][0]['user']['description']='Static medium shot.'
    assert compile_window(p,0,picture_offset=1)['generation_hash']!=before


def test_user_precedence_dialogue_verbatim():
    p=plan_project();p['scene_plan']['settings']['generate_descriptions']=True
    p['scene_plan']['shots'][0].update(vlm={'description':'invented wide shot'},user={'description':'Mary remains neutral.'})
    p['prompts']['regions']=[{'id':'dialogue','start':0,'end':168,'text':'S2 <d>[English] We both do.</d>\n(pause)'}]
    w=compile_window(p,0,picture_offset=1)
    assert 'invented' not in w['prompt'] and 'Mary remains neutral.' in w['prompt']
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
    assert physical['prompts'][0].count('Shot plan:')==1
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

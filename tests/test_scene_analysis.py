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
        f,_=color_filter(meta,settings());assert 't=linear' in f and 'npl=125' in f and 'iec61966-2-1' in f
        assert ('tonemap=' in f)==(trc!='bt709')
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
    assert [s['range'] for s in w['segments']]==[[0,67],[67,153],[153,168]]
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
    assert all('S2 <d>[English] We both do.</d>\n(pause)' in s['text'] for s in w['segments'])


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

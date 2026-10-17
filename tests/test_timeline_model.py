import copy
import io
from fractions import Fraction
import pytest
from ComfyUI_H3_Continuum_Join.timeline.model import blank_project, compile_project, compile_window, sample_boundary, validate

A, B = 'a'*64, 'b'*64

def project():
    p=blank_project()
    p['assets']=[{'id':A,'alias':'Alice'},{'id':B,'alias':'Bob'}]
    p['tracks']=[{'id':'v','role':'video_guide','clips':[{'id':'v1','asset_id':A,'start':0,'end':336,'source_in':'0','speed':'1','padding':'hold'}]},
                 {'id':'refs','role':'image_reference','clips':[{'id':'a','asset_id':A,'start':0,'end':336},{'id':'b','asset_id':B,'start':336,'end':504}]}]
    p['prompts']['global']='@Alice sees @Bob'
    return validate(p)

def test_exact_selections_and_aliases():
    p=project()
    a,b,c=[compile_window(p,i) for i in range(3)]
    assert a['video'][0]['source']==['0','7']
    assert b['video'][0]['source']==['7','14']
    assert c['video']==[]
    assert a['aliases']=={'Alice':'<Picture 1>'}
    assert c['aliases']=={'Bob':'<Picture 1>'}

def test_causal_metadata_and_audio_identity():
    p=project()
    old=[compile_window(p,i)['generation_hash'] for i in range(3)]
    p['name']='Changed title';p['ui']={'zoom':3};p['tracks'][0]['hidden']=True
    assert old==[compile_window(p,i)['generation_hash'] for i in range(3)]
    p['tracks'][1]['clips'][1]['asset_id']=A
    assert old[:2]==[compile_window(p,i)['generation_hash'] for i in range(2)]
    assert old[2]!=compile_window(p,2)['generation_hash']

def test_sample_boundaries_no_drift():
    for rate in (44100,48000):
        assert sum(sample_boundary(i+1,rate)-sample_boundary(i,rate) for i in range(1001))==sample_boundary(1001,rate)

def test_long_project_short_selection():
    p=blank_project();p['duration_frames']=2*3600*24;p['selection']=[3600*24,3600*24+168]
    plan=compile_project(p)
    assert len(plan['jobs'])==1
    assert len(plan['jobs'][0]['windows'])==2
    p['selection']=[0,168*33]
    assert [len(j['windows']) for j in compile_project(p)['jobs']]==[16,16,1]

def test_invalid_contracts():
    p=project();p['tracks'][0]['clips'][0]['start']=-1
    with pytest.raises(ValueError):validate(p)
    p=project();p['project_id']='../escape'
    with pytest.raises(ValueError):validate(p)

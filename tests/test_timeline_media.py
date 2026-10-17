from fractions import Fraction
import io
from pathlib import Path
import subprocess
import numpy as np
import pytest
from ComfyUI_H3_Continuum_Join.timeline.media import AssetStore, ProjectStore, probe, video_frames, audio_window, mix_audio
from ComfyUI_H3_Continuum_Join.timeline.model import blank_project,validate
from ComfyUI_H3_Continuum_Join.timeline.compose import export_project

@pytest.fixture
def movie(tmp_path):
    file=tmp_path/'source.mkv'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=s=64x64:r=25:d=14','-f','lavfi','-i','sine=frequency=440:sample_rate=44100:duration=14','-c:v','ffv1','-c:a','pcm_s16le','-threads','1',str(file)],check=True)
    return file

def test_pts_window_and_native_audio(movie):
    first=video_frames(movie,[Fraction(i,24) for i in range(168)],64,64)
    second=video_frames(movie,[Fraction(i+168,24) for i in range(168)],64,64)
    assert first.shape==second.shape==(168,64,64,3)
    assert not np.array_equal(first[-1],second[0])
    assert np.array_equal(second[0],video_frames(movie,['7'],64,64)[0])
    for rate in (44100,48000):
        a=audio_window(movie,'0','7',rate);b=audio_window(movie,'7','14',rate)
        whole=audio_window(movie,'0','14',rate)
        assert a.shape==b.shape==(2,7*rate)
        np.testing.assert_allclose(np.concatenate([a,b],axis=1),whole,atol=1e-4)

def test_store_roundtrip_and_stale(movie,tmp_path):
    store=ProjectStore(tmp_path/'store')
    with movie.open('rb') as source:asset=store.assets.register(source,'source.mkv')
    assert store.assets.verify(asset['id']).is_file()
    p=blank_project();p['assets']=[asset]
    saved=store.save(p,0);assert store.read(p['project_id'])==saved
    with pytest.raises(ValueError,match='edited elsewhere'):store.save(p,0)
    with pytest.raises(ValueError):store.assets.path('../source')
    store.assets.path(asset['id']).write_bytes(b'stale')
    with pytest.raises(ValueError,match='changed'):store.assets.verify(asset['id'])

def test_audio_only_and_composition(movie,tmp_path):
    store=ProjectStore(tmp_path/'store')
    with movie.open('rb') as source:asset=store.assets.register(source,'source.mkv')
    p=blank_project();p.update(width=64,height=64,duration_frames=336,selection=[0,336],assets=[asset])
    c={'id':'a','asset_id':asset['id'],'start':0,'end':336,'source_in':'0','speed':'1','padding':'error','audio_role':'both'}
    p['tracks']=[{'id':'source','role':'retained','clips':[{**c,'audio_role':'neither'}]},{'id':'a','role':'audio','clips':[c]}]
    p=validate(p)
    mixed=mix_audio(p,store.assets,168,336,44100,'guide')
    np.testing.assert_allclose(mixed,audio_window(movie,'7','14',44100),atol=1e-6)
    p['duration_frames']=48;p['selection']=[0,48]
    for t in p['tracks']:t['clips'][0]['end']=48
    output=export_project(p,store,tmp_path/'out.mkv')
    info=probe(output)
    assert Fraction(info['duration'])==2
    original=video_frames(movie,[Fraction(i,24) for i in range(48)],64,64)
    exported=video_frames(output,[Fraction(i,24) for i in range(48)],64,64)
    np.testing.assert_array_equal(original,exported)

def test_cancel_leaves_no_partial_export(movie,tmp_path):
    store=ProjectStore(tmp_path/'store');p=blank_project();p.update(width=64,height=64,duration_frames=24,selection=[0,24])
    def cancel():raise ValueError('cancelled')
    with pytest.raises(ValueError,match='cancelled'):export_project(p,store,tmp_path/'output.mkv',cancel=cancel)
    assert not (tmp_path/'output.mkv').exists()
    assert not list(tmp_path.glob('output-*.mkv'))

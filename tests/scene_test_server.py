"""Production Timeline HTTP routes for browser/packaged-desktop acceptance.

Only the absent ComfyUI host/graph are stand-ins. Scene jobs, codec I/O,
project validation, compilation, assets and served frontend are real modules.
No sampler or trained-model fidelity is asserted by this server.
"""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
from fractions import Fraction

import av
import numpy as np
from aiohttp import web

ROOT=Path(__file__).resolve().parents[1]
PACKAGE="ComfyUI_H3_Continuum_Join"
spec=importlib.util.spec_from_file_location(PACKAGE, ROOT/'__init__.py',submodule_search_locations=[str(ROOT)])
module=importlib.util.module_from_spec(spec);module.__path__=[str(ROOT)];sys.modules[PACKAGE]=module
import torch  # noqa: F401 - production compiler imports the native runtime modules

from ComfyUI_H3_Continuum_Join.timeline.media import ProjectStore
from ComfyUI_H3_Continuum_Join.timeline.model import blank_project,validate
from ComfyUI_H3_Continuum_Join.timeline.protocol import native_runtime_manifest

TEMP=tempfile.TemporaryDirectory(prefix='h3-scene-http-')
store=ProjectStore(Path(TEMP.name)/'store')
source=Path(TEMP.name)/'source.mkv'
y,x=np.mgrid[:96,:160]
a=np.stack([(x*3+y)%256,(y*4)%256,((x//12)%2*160+30)+np.zeros_like(y)],2).astype('uint8')
b=np.stack([(y*2)%256,((x//18+y//13)%2)*180+20,(x+y*3)%256],2).astype('uint8')
with av.open(str(source),'w') as c:
    s=c.add_stream('ffv1',rate=24);s.width,s.height=160,96;s.pix_fmt='bgr0'
    for i in range(168):
        frame=av.VideoFrame.from_ndarray(a if i<67 or i>=153 else b,format='rgb24')
        frame.pts=i;frame.time_base=Fraction(1,24)
        for packet in s.encode(frame):c.mux(packet)
    for packet in s.encode():c.mux(packet)
with source.open('rb') as file:asset=store.assets.register(file,source.name,cancel=lambda:None)
p=blank_project();p.update(duration_frames=168,selection=[0,168],width=160,height=96,assets=[asset],
    video_guidance_enabled=False,audio_generation_mode='source_locked',final_audio_source='original',
    tracks=[{'id':'original','role':'retained','clips':[{'id':'movie','asset_id':asset['id'],'start':0,'end':168,'source_in':'0','speed':'1','audio_role':'neither'}]}],
    prompts={'global':'Preserve replacement identity from dedicated references.','mode':'Fixed','script':'S2 <d>[English] We both do.</d>'})
p=validate(p)
routes=web.RouteTableDef()
instance=types.SimpleNamespace(routes=routes)
sys.modules['server']=types.SimpleNamespace(PromptServer=types.SimpleNamespace(instance=instance))
sys.modules['nodes']=types.SimpleNamespace(EXTENSION_WEB_DIRS={'Continuum':str(ROOT/'web')})
from ComfyUI_H3_Continuum_Join.timeline import routes as production
production.project_store=lambda:store
production.register_routes()

@routes.get('/fixture')
async def fixture(request):
    return web.json_response({'project':p,'runtime':native_runtime_manifest({'Continuum':ROOT/'web'},ROOT/'web')})

@routes.get('/system_stats')
async def stats(request):return web.json_response({'system':{'os':sys.platform},'devices':[]})

@routes.get('/extensions/Continuum/{path:.*}')
async def frontend(request):
    path=(ROOT/'web'/request.match_info['path']).resolve()
    if not path.is_relative_to((ROOT/'web').resolve()) or not path.is_file():raise web.HTTPNotFound()
    return web.FileResponse(path)

@routes.get('/scene-smoke.js')
async def smoke_script(request):return web.FileResponse(ROOT/'tests/scene_smoke.mjs')

@routes.get('/')
async def page(request):
    return web.Response(content_type='text/html',text='''<!doctype html><link rel="stylesheet" href="/extensions/Continuum/timeline/studio.css">
    <div hidden>H3_SMOKE_READY<input id="h3-smoke-file" type="file"></div>
    <script>window.h3SceneSmoke=new Promise((resolve,reject)=>{window.sceneDone=resolve;window.sceneFailed=reject;});</script>
    <script type="module">
    import {TimelineEditor} from '/extensions/Continuum/timeline/editor.js';
    import {runSceneSmoke} from '/scene-smoke.js';
    try {
      const {project}=await (await fetch('/fixture')).json();
      const node={id:1,properties:{},setDirtyCanvas(){}};
      const app={graph:{_nodes:[node]},queuePrompt:async()=>{window.queueCount++;}};
      const api={apiURL:p=>p,fetchApi:(p,o)=>fetch(p,o)};
      window.queueCount=0;
      window.editor=new TimelineEditor({node,widget:{value:JSON.stringify(project)},api,app});
      window.h3RunSceneSmoke=()=>runSceneSmoke(window.editor);
      window.sceneReady=true;
    }catch(error){window.sceneFailed(error);throw error;}
    </script>''')

app=web.Application(client_max_size=20*1024*1024);app.add_routes(routes)

async def main():
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    print(json.dumps({'url':f'http://127.0.0.1:{port}'}),flush=True)
    await __import__('asyncio').Event().wait()

if __name__=='__main__':
    try:__import__('asyncio').run(main())
    finally:TEMP.cleanup()

"""Same-origin Timeline Studio API. No caller-controlled filesystem paths."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import uuid

from aiohttp import web

from .media import audio_window, run_process, video_frames
from .model import compile_project, validate
from .nodes import project_store, stale_take, take_path, takes

_PREFIX='/h3_continuum/timeline'
_tasks={}


def register_routes():
    try:
        from server import PromptServer
    except ModuleNotFoundError as exc:
        if exc.name == 'server':
            return  # Standalone compiler/media tooling has no ComfyUI server.
        raise
    routes=PromptServer.instance.routes

    def endpoint(method,path):
        def decorate(fn):
            async def safe(request):
                try:return await fn(request)
                except (ValueError,KeyError,IndexError,FileNotFoundError) as exc:
                    return web.json_response({'error':str(exc)},status=400)
            getattr(routes,method)(_PREFIX+path)(safe)
            return fn
        return decorate

    @endpoint('post','/compile')
    async def compile_route(request):
        data=await request.json()
        return web.json_response(await asyncio.to_thread(compile_project,data))

    @endpoint('post','/project')
    async def save(request):
        data=await request.json()
        store=project_store()
        saved=await asyncio.to_thread(store.save,data['project'],data['expected_revision'])
        return web.json_response(saved)

    @endpoint('get','/project/{project_id}')
    async def load(request):
        return web.json_response(await asyncio.to_thread(project_store().read,request.match_info['project_id']))

    @endpoint('post','/import')
    async def ingest(request):
        # Browser File/Blob streaming and aiohttp chunks avoid whole-file RAM.
        store=project_store()
        with tempfile.TemporaryFile() as file:
            async for chunk in request.content.iter_chunked(1024*1024):
                file.write(chunk)
            file.seek(0)
            asset=await asyncio.to_thread(store.assets.register,file,request.query.get('name','Media'))
        if asset.get('video') and (asset['video'].get('nb_frames')=='1' or asset['name'].lower().endswith(('.png','.jpg','.jpeg','.webp','.bmp'))):
            asset['kind']='image'
        return web.json_response(asset)

    @endpoint('get','/media/{asset_id}')
    async def media(request):
        file=project_store().assets.path(request.match_info['asset_id'])
        response=web.FileResponse(file)
        response.content_type=request.query.get('type','video/mp4') if request.query.get('type') in ('video/mp4','video/webm','audio/wav','image/png','image/jpeg') else 'application/octet-stream'
        return response

    @endpoint('get','/proxy/{asset_id}')
    async def proxy(request):
        store=project_store();asset_id=request.match_info['asset_id'];file=store.assets.path(asset_id)
        root=store.root/'proxies';root.mkdir(exist_ok=True)
        # A bounded *window* proxy. Never transcode an entire two-hour import.
        start=max(0,float(request.query.get('start','0')));duration=min(30,max(.1,float(request.query.get('duration','10'))))
        key=f'{asset_id}-{start:.3f}-{duration:.3f}'
        output=root/(key+'.mp4')
        if not output.exists():
            temporary=root/(key+'-'+uuid.uuid4().hex+'.mp4')
            try:
                await asyncio.to_thread(run_process,['ffmpeg','-v','error','-nostdin','-threads','1','-ss',str(start),'-protocol_whitelist','file,pipe','-i',str(file),'-t',str(duration),'-vf','scale=640:-2','-c:v','libx264','-preset','ultrafast','-crf','24','-c:a','aac','-movflags','+faststart',str(temporary)])
                temporary.replace(output)
            finally:temporary.unlink(missing_ok=True)
        return web.FileResponse(output,headers={'Content-Type':'video/mp4'})

    @endpoint('get','/thumbnail/{asset_id}')
    async def thumbnail(request):
        from PIL import Image
        import io
        path=project_store().assets.path(request.match_info['asset_id'])
        stamp=request.query.get('pts','0')
        pixels=await asyncio.to_thread(video_frames,path,[stamp],240,136,padding='hold')
        data=io.BytesIO();Image.fromarray(pixels[0]).save(data,format='JPEG')
        return web.Response(body=data.getvalue(),content_type='image/jpeg')

    @endpoint('get','/waveform/{asset_id}')
    async def waveform(request):
        import numpy as np
        path=project_store().assets.path(request.match_info['asset_id'])
        start=max(0,float(request.query.get('start','0')));end=min(start+60,float(request.query.get('end',str(start+30))))
        pcm=await asyncio.to_thread(audio_window,path,str(start),str(end),1000)
        peaks=np.max(np.abs(pcm),axis=0)
        return web.json_response({'start':start,'end':end,'peaks':[float(np.max(x)) if x.size else 0 for x in np.array_split(peaks,512)]})

    @endpoint('get','/takes/{project_id}')
    async def list_takes(request):
        store=project_store();pid=request.match_info['project_id'];records=takes(store,pid)
        try:p=store.read(pid)
        except FileNotFoundError:p=None
        result=[]
        for r in records:
            render=store.project_dir(pid)/('render-'+r['id']+'.json')
            result.append({**{k:v for k,v in r.items() if k!='project_snapshot'},
                'stale':stale_take(p,r,records) if p else None,
                'rendition':json.loads(render.read_text()) if render.exists() else None})
        return web.json_response(result)

    @endpoint('post','/take/select')
    async def select_take(request):
        data=await request.json();store=project_store();p=store.read(data['project_id'])
        record=json.loads(take_path(store,p['project_id'],data['take_id']).read_text())
        all_records=takes(store,p['project_id'])
        if data['action']=='reject':
            p.setdefault('rejected',[]).append(record['id'])
        else:
            if stale_take(p,record,all_records):raise ValueError('Take inputs or causal parent changed; render a new take')
            render=json.loads((store.project_dir(p['project_id'])/('render-'+record['id']+'.json')).read_text())
            asset=render['asset']
            if asset['id'] not in {a['id'] for a in p['assets']}:p['assets'].append(asset)
            selected=p.setdefault('accepted',{})
            # Selecting a different upstream take invalidates selected suffixes.
            old=selected.get(str(record['job_index']))
            invalidated=[]
            if old!=record['id']:
                for k in list(selected):
                    if int(k)>record['job_index']:invalidated.append(selected.pop(k))
            selected[str(record['job_index'])]=record['id']
            lane=next((t for t in p['tracks'] if t['id']=='accepted-takes'),None)
            if lane is None:
                lane={'id':'accepted-takes','role':'generated','name':'Accepted takes','clips':[]};p['tracks'].append(lane)
            lane['clips']=[c for c in lane['clips'] if c.get('take_id') not in invalidated]
            a,b=record.get('output_range',record['range']);sa,sb=record['selection']
            a,b=max(a,sa),min(b,sb)
            # Remove overlapping old generated clips, retaining their unedited sides.
            remaining=[]
            for c in lane['clips']:
                if c['end']<=a or c['start']>=b:remaining.append(c);continue
                if c['start']<a:remaining.append({**c,'id':uuid.uuid4().hex,'end':a})
                if c['end']>b:
                    from .model import source_time
                    remaining.append({**c,'id':uuid.uuid4().hex,'start':b,'source_in':str(source_time(c,b))})
            remaining.append({'id':record['id'],'take_id':record['id'],'asset_id':asset['id'],'start':a,'end':b,
                'source_in':str((a-record.get('output_range',record['range'])[0])/24),'speed':'1','padding':'error',
                'audio_role':'neither' if any(w['final'] for w in compile_project(record['project_snapshot'])['jobs'][record['job_index']]['windows']) else 'final'})
            lane['clips']=remaining
        return web.json_response(store.save(p,data['expected_revision']))

    @endpoint('post','/export')
    async def export(request):
        from .compose import export_project
        import folder_paths
        data=await request.json();p=validate(data['project']);codec=data.get('codec','lossless')
        if codec not in ('lossless','h264'):raise ValueError('Unknown export codec')
        active=sum(t['status']=='running' for t in _tasks.values())
        if active>=2:raise ValueError('Two exports are already running')
        job=uuid.uuid4().hex;event=threading.Event()
        output=Path(folder_paths.get_output_directory())/('Timeline_'+job+('.mkv' if codec=='lossless' else '.mp4'))
        item={'status':'running','cancel':event,'filename':output.name}
        _tasks[job]=item
        def cancel():
            if event.is_set():raise ValueError('Export cancelled')
        async def work():
            try:
                await asyncio.to_thread(export_project,p,project_store(),output,codec=codec,cancel=cancel)
                item['status']='complete'
            except Exception as exc:item.update(status='failed',error=str(exc))
        item['task']=asyncio.create_task(work())
        return web.json_response({'id':job,'status':'running'})

    @endpoint('get','/export/{job_id}')
    async def status(request):
        job=request.match_info['job_id'];item=_tasks[job]
        return web.json_response({k:v for k,v in item.items() if k not in ('cancel','task')})

    @endpoint('post','/export/{job_id}/cancel')
    async def cancel_export(request):
        _tasks[request.match_info['job_id']]['cancel'].set()
        return web.json_response({'cancelled':True})

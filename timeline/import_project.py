"""Preview/apply operations on copied projects; imports never edit live state."""
from __future__ import annotations

import copy
from fractions import Fraction
import uuid

from .model import blank_project, rational, validate


def replace_regions(existing, incoming):
    """Overwrite covered spans only, retaining every uncovered prompt region."""
    result=copy.deepcopy(existing)
    for new in incoming:
        retained=[]
        for old in result:
            if old['end']<=new['start'] or old['start']>=new['end']:
                retained.append(old)
                continue
            if old['start']<new['start']:
                retained.append({**old,'id':uuid.uuid4().hex,'end':new['start']})
            if old['end']>new['end']:
                retained.append({**old,'id':uuid.uuid4().hex,'start':new['end']})
        result=retained+[copy.deepcopy(new)]
    return sorted(result,key=lambda x:(x['start'],x['end']))


def prompt_mapping(document,project):
    origin=project.get('chunk_origin_frame',0)
    size=project['chunk_frames']
    stop=project['selection'][1]
    incoming=[]
    surplus=[]
    notes=[]
    for i,entry in enumerate(document.get('entries',[])):
        if 'start_seconds' in entry:
            raw_a=origin+rational(entry['start_seconds'])*24
            raw_b=origin+rational(entry['end_seconds'])*24
            a,b=round(raw_a),round(raw_b)
            if raw_a!=a or raw_b!=b:
                notes.append(f'Entry {i+1}: source seconds [{entry["start_seconds"]}, {entry["end_seconds"]}) map to 24-fps frames [{a}, {b}); rounding is at most half a frame.')
            if (a-origin)%size or (b-origin)%size:
                notes.append(f'Entry {i+1} does not match the current {size}-frame chunk grid; it becomes timed inner sections.')
        else:
            a=origin+(entry['chunk']-1)*size
            b=a+size
        left,right=max(origin,a),min(stop,b)
        if a<origin or b>stop:
            surplus.append({'entry':i+1,'range':[a,b],'text':entry['text'],'applied_range':[left,right] if left<right else None})
        if left<right:
            incoming.append({'id':uuid.uuid4().hex,'start':left,'end':right,'text':entry['text'],'entry':i+1})
    incoming.sort(key=lambda e:(e['start'],e['end']))
    if any(a['end']>b['start'] for a,b in zip(incoming,incoming[1:])):
        raise ValueError('Imported entries overlap on the current scene grid; fix the source timing before applying')
    rows=[]
    gaps=[]
    uncovered=[]
    for index,start in enumerate(range(origin,stop,size)):
        end=min(stop,start+size)
        parts=[{'range':[max(start,e['start']),min(end,e['end'])],'text':e['text'],'entry':e['entry']} for e in incoming if e['start']<end and start<e['end']]
        cursor=start
        for part in parts:
            if part['range'][0]>cursor:
                gaps.append([cursor,part['range'][0]])
            cursor=part['range'][1]
        if cursor<end:
            gaps.append([cursor,end])
        if not parts:
            uncovered.append(index+1)
        rows.append({'chunk':index+1,'range':[start,end],'seconds':[str(Fraction(start,24)),str(Fraction(end,24))],'parts':parts,'uncovered':not parts})
    if len(document.get('entries',[]))!=len(rows):
        notes.append(f'{len(document.get("entries",[]))} imported entries map onto {len(rows)} current scene chunks; uncovered chunks retain their existing prompts.')
    return {'regions':incoming,'rows':rows,'surplus':surplus,'uncovered':uncovered,'gaps':gaps,'notes':notes}


def apply_components(current, document, media, selected, target='merge', provenance=None, soundtrack_mode='replace'):
    """Return candidate plus mapping. Preview owns confirmation, caller owns Undo."""
    validate(current)
    if target not in {'merge','replace'}:
        raise ValueError('Choose merge or replace scene')
    if target=='replace' and not (selected.get('video') or selected.get('soundtrack')):
        raise ValueError('Replace scene is available only when media is selected')
    if soundtrack_mode not in {'replace','add'}:
        raise ValueError('Choose replace or add soundtrack')
    p=copy.deepcopy(current)
    videos=[m for m in media if m['component']=='video'] if selected.get('video') else []
    sounds=[m for m in media if m['component']=='soundtrack'] if selected.get('soundtrack') else []
    if target=='replace':
        p=blank_project()
        for key in ('project_id','revision','width','height'):
            if key in current:
                p[key]=copy.deepcopy(current[key])
        p['prompts']=copy.deepcopy(current.get('prompts',{}))
        p['prompt_source']=copy.deepcopy(current.get('prompt_source',{}))
        # Media-only replacement preserves prompt tracks and their authored span.
        if not selected.get('chunks'):
            p['tracks']=[copy.deepcopy(t) for t in current['tracks'] if t['role']=='prompt']
        spans=[m['end'] for m in (videos or sounds)]
        spans += [c['end'] for t in p['tracks'] for c in t['clips']]
        p['duration_frames']=max(spans or [current['duration_frames']])
        p['chunk_frames']=current['chunk_frames']
        durations={m['end']-m['start'] for m in videos[:-1] or videos}
        if len(durations)==1 and 96<=next(iter(durations))<=360:
            p['chunk_frames']=next(iter(durations))
        p['selection']=[0,p['duration_frames']]
        p['name']='Imported scene'
    origin=p.get('chunk_origin_frame',0) if target=='merge' else 0
    if videos:
        lane={'id':uuid.uuid4().hex,'role':'retained','name':'Imported source pictures','clips':[]}
        for m in videos:
            a,b=m['start']+origin,m['end']+origin
            if target=='merge' and b>p['duration_frames']:
                # Media is retained in full; extending the movie does not move its grid.
                p['duration_frames']=b
            clip={'id':uuid.uuid4().hex,'asset_id':m['asset']['id'],'start':a,'end':b,
                  'source_in':m['source_in'],'speed':'1','padding':'hold',
                  'audio_padding':'silence','audio_role':'neither'}
            if selected.get('soundtrack') and not sounds and m['asset'].get('audio'):
                clip['audio_role']='both'
            lane['clips'].append(clip)
        p['tracks'].append(lane)
        p['operation']='recreate'
    if sounds:
        if soundtrack_mode=='replace':
            for t in p['tracks']:
                for c in t['clips']:
                    if c.get('audio_role') in {'guide','final','both'}:
                        c['audio_role']='neither'
        lane={'id':uuid.uuid4().hex,'role':'audio','name':'Imported original soundtrack','clips':[]}
        for m in sounds:
            a=origin+m['start']
            b=origin+m['end']
            # A selected soundtrack covers the active scene, with explicit
            # silence after a short source. No generated-audio fallback.
            b=p['selection'][1] if videos else max(b,p['selection'][1])
            p['duration_frames']=max(p['duration_frames'],b)
            lane['clips'].append({'id':uuid.uuid4().hex,'asset_id':m['asset']['id'],
                'start':a,'end':b,'source_in':m['source_in'],'speed':'1',
                'padding':'silence','audio_role':'both'})
            p['audio_sample_rate']=int(m['asset']['audio']['sample_rate'])
        p['tracks'].append(lane)
    for m in videos+sounds:
        if not any(a['id']==m['asset']['id'] for a in p['assets']):
            p['assets'].append(copy.deepcopy(m['asset']))
    mapping=prompt_mapping(document,p)
    if selected.get('global'):
        p.setdefault('prompts',{})['global']=document.get('global','')
    if selected.get('chunks'):
        p.setdefault('prompts',{})['regions']=replace_regions(p.get('prompts',{}).get('regions',[]),mapping['regions'])
    if selected.get('global') or selected.get('chunks'):
        p['prompt_source']={'mode':'studio','imported_from':provenance or {'kind':'file'}}
    p['import_notes']=mapping['notes']
    validate(p)
    return p,mapping

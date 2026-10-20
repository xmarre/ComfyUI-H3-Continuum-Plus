import { uid } from "./core.js";

export function importedRegions(project,start,end) {
  return (project.prompts?.regions||[]).filter(r=>r.start<end&&start<r.end)
    .map(r=>({...r,start:Math.max(start,r.start),end:Math.min(end,r.end)}));
}
export function setImportedRegion(project,start,end,text,metadata={}) {
  const next=[];
  for(const region of project.prompts?.regions||[]) {
    if(region.end<=start||region.start>=end){next.push(region);continue;}
    if(region.start<start){const part={...region,id:uid(),end:start};delete part.chunk_index;next.push(part);}
    if(region.end>end){const part={...region,id:uid(),start:end};delete part.chunk_index;next.push(part);}
  }
  const added=text===null?null:{id:uid(),start,end,text:String(text),override:true,...metadata};
  if(added)next.push(added);
  project.prompts ||= {global:"",mode:"Fixed",script:""};
  project.prompts.regions=next.sort((a,b)=>a.start-b.start||a.end-b.end);
  return added;
}

export function migrateChunkPrompts(project) {
  const old=project.prompts?.regions||[];
  let migrated=false;
  for(const track of project.tracks||[]) {
    if(track.role!=="prompt")continue;
    track.clips=track.clips.filter(clip=>{
      if(!clip.chunk_prompt)return true;
      if(!migrated){project.prompts ||= {};project.prompts.regions=[];migrated=true;}
      setImportedRegion(project,clip.start,clip.end,clip.text||"",{...clip,
        muted:!!(track.muted||clip.muted),locked:!!(track.locked||clip.locked)});
      return false;
    });
  }
  if(migrated) {
    for(const r of old)setImportedRegion(project,r.start,r.end,r.text,r);
    project.tracks=project.tracks.filter(t=>t.role!=="prompt"||t.clips.length||!t.chunk_workspace);
  }
  return project;
}

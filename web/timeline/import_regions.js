import { uid } from "./core.js";

export function importedRegions(project,start,end) {
  return (project.prompts?.regions||[]).filter(r=>r.start<end&&start<r.end)
    .map(r=>({...r,start:Math.max(start,r.start),end:Math.min(end,r.end)}));
}
export function setImportedRegion(project,start,end,text) {
  const next=[];
  for(const region of project.prompts?.regions||[]) {
    if(region.end<=start||region.start>=end){next.push(region);continue;}
    if(region.start<start)next.push({...region,id:uid(),end:start});
    if(region.end>end)next.push({...region,id:uid(),start:end});
  }
  next.push({id:uid(),start,end,text:String(text)});
  project.prompts ||= {global:"",mode:"Fixed",script:""};
  project.prompts.regions=next.sort((a,b)=>a.start-b.start||a.end-b.end);
}

// Smart References is a detached analysis workspace. Apply uses the editor's
// existing History transaction and rejects results after concurrent edits.
import { clone } from "./core.js";
import { referenceSocketSources, connectedPromptForPreview } from "./chunk_ui.js?v=h3-timeline-smart-references-v34";
const BASE="/h3_continuum/timeline";
const el=(tag,attrs={},...children)=>{
  const node=document.createElement(tag);
  for(const [key,value] of Object.entries(attrs)) {
    if(key==="text")node.textContent=value;
    else if(key==="class")node.className=value;
    else node.setAttribute(key,String(value));
  }
  node.append(...children.filter(x=>x!=null));return node;
};
const button=(text,action)=>{const n=el("button",{type:"button",text});n.onclick=action;return n;};
async function json(api,path,body,method=body===undefined?"GET":"POST") {
  const r=await api.fetchApi(path,{method,...(body===undefined?{}:{body:JSON.stringify(body)})});
  let data;try{data=await r.json();}catch{data={};}
  if(!r.ok)throw Error(typeof data.error==="string"?data.error:data.error?.message||"Scene analysis request failed ("+r.status+")");
  return data;
}

/**
 * Build a non-destructive publication plan. Source shelf indices and physical
 * H3 reference_image_N sockets are DISTINCT namespaces. Never use a Conveyor
 * shelf ordinal as an H3 socket index when Media Resolution reroutes them.
 *
 * Full shelves can still receive JPEGs in *unconnected* spare shelf slots;
 * those images are staged, NOT claimed as live H3 conditioning. The user may
 * wire their shelf outputs later. Native scoped fallback stays unchanged until
 * there is a verified physical route.
 */
export function screenshotIdsForSelection(plan, selection, sourceAssetId=null) {
  const shots=(plan?.shots||[]).filter(shot=>(
    shot.range[0]<selection[1] && selection[0]<shot.range[1] &&
    (!sourceAssetId || shot.source_asset_id===sourceAssetId)
  ));
  return {
    shots,
    ids:[...new Set(shots.flatMap(shot=>shot.reference_ids))],
    enabledIds:[...new Set(shots.filter(s=>s.user?.enabled!==false)
      .flatMap(s=>s.reference_ids))],
  };
}

/**
 * Per-chunk native H3 transport: persistent identity sockets remain fixed,
 * only overlapping screenshot assets are materialized for the current chunk.
 * The compositor's Image Conveyor shelf is an optional UI library; static
 * graph IMAGE outputs cannot change midway through a queued H3 sequence.
 */
export function planChunkReferenceStream(project, sources) {
  const start=project.chunk_origin_frame||0;
  const end=project.duration_frames;
  const size=project.chunk_frames;
  if(!Number.isInteger(size)||size<=0||!Number.isInteger(end)||end<=start)
    throw Error("Invalid Timeline chunk grid for streamed scene references.");
  const shots=project.scene_plan?.shots||[];
  const bank=[...new Set(shots.filter(s=>s.user?.enabled!==false)
    .flatMap(s=>s.reference_ids||[]))];
  const identitySources=sources.filter(s=>
    s.connected && !s.unavailable && Number.isInteger(s.slot) &&
    s.slot>=1 && s.slot<=8 && !s.dynamic &&
    !String(s.label||"").includes("h3_timeline_screencaps/") &&
    !String(s.thumbnail||"").includes("h3_timeline_screencaps"));
  const sockets=[...new Set(identitySources.map(s=>s.slot))].sort((a,b)=>a-b);
  // Bundled IMAGE outputs can carry more than one H3 reference; the native
  // sampler owns the actual dynamic count. Browser count is only an estimate.
  const estimates=identitySources.map(s=>Math.max(1,Number(s.estimatedImageCount)||1));
  const identityEstimate=estimates.reduce((a,b)=>a+b,0);
  const sceneBudget=Math.min(5,Math.max(0,
    Number(project.scene_plan?.settings?.scene_budget??3)));
  if(identityEstimate>=8)
    throw Error("All eight H3 Picture positions are already occupied by persistent references.");
  const perChunk={};
  const stats=[];
  for(let index=0,a=start;a<end;index++,a+=size){
    const b=Math.min(a+size,end);
    const relevant=shots.filter(s=>s.user?.enabled!==false&&s.range[0]<b&&a<s.range[1]&&
      (a<=s.range[0] || (index===0&&s.range[0]<a)));
    const ids=[...new Set(relevant.flatMap(s=>s.reference_ids||[]))];
    perChunk[String(index)]=sockets.slice();
    stats.push({index,from:a,to:b,sourceImageCount:ids.length,
      budget:Math.min(sceneBudget,8-identityEstimate),identityEstimate,
      accepted:Math.min(ids.length,Math.min(sceneBudget,8-identityEstimate)),
      omitted:Math.max(0,ids.length-Math.min(sceneBudget,8-identityEstimate)),ids});
  }
  return {perChunk,stats,bank,sockets,identityEstimate};
}

export function planConveyorPublication(ids, state, sources, nodeId, toggles=null) {
  if(state?.output_mode!=="persistent_refs" ||
     !Array.isArray(state.reference_slots) || state.reference_slots.length!==8)
    throw Error("Image Conveyor #"+nodeId+" must use Persistent references mode.");
  if(!Array.isArray(ids) || ids.length===0 || new Set(ids).size!==ids.length || ids.length>8)
    throw Error("Select between 1 and 8 distinct screenshots per Conveyor shelf.");
  const byShelf=new Map();
  for(const route of sources) {
    if(route.nodeId!==nodeId || !route.connected || !route.conveyor ||
       !Number.isInteger(route.shelfNumber) || route.shelfNumber<1 || route.shelfNumber>8 ||
       !Number.isInteger(route.slot) || route.slot<1 || route.slot>8 ||
       route.dynamic || route.ambiguousPath) continue;
    const list=byShelf.get(route.shelfNumber)||[];
    list.push(route);
    byShelf.set(route.shelfNumber,list);
  }
  const preferredRoute=shelf=>{
    const options=byShelf.get(shelf)||[];
    // One shelf may feed multiple physical ports; use a physical socket only
    // if the source path and output toggle are unambiguous.
    if(options.length!==1 || (Array.isArray(toggles) && toggles[shelf-1]===false))
      return null;
    return options[0].slot;
  };
  const used=new Set();
  const assignments=[];
  const remaining=Array.from({length:8},(_,i)=>i+1);
  for(const rid of ids) {
    if(!/^[a-f0-9]{64}$/.test(rid))throw Error("Invalid screenshot content identity.");
    const annotated="h3_timeline_screencaps/"+rid+".jpg [input]";
    const existing=remaining.filter(n=>!used.has(n) &&
      state.reference_slots[n-1]?.annotated===annotated);
    // Prefer already-imported screenshots with a confirmed physical route.
    const chosen=existing.sort((a,b)=>Number(!!preferredRoute(b))-Number(!!preferredRoute(a)))[0] ??
      remaining.filter(n=>!used.has(n)&&state.reference_slots[n-1]==null)
        .sort((a,b)=>Number(!!preferredRoute(b))-Number(!!preferredRoute(a)))[0];
    if(!chosen)throw Error("Only "+(8-used.size)+" Conveyor shelf positions remain; "+
      "all other positions hold existing references. No character images were overwritten.");
    used.add(chosen);
    assignments.push({
      id:rid, shelfNumber:chosen, physicalSlot:preferredRoute(chosen),
      annotated,
    });
  }
  const routed=assignments.filter(x=>x.physicalSlot!==null);
  const staged=assignments.filter(x=>x.physicalSlot===null);
  return {assignments,routed,staged,knownRoutes:[...byShelf.values()].flat().length};
}

export function applySceneCandidate(editor,candidate,version) {
  if(editor.closed||editor.editVersion!==version)
    throw Error("The timeline changed during analysis. Preview again before applying the shot plan.");
  editor.edit(p=>{
    const ids=new Set(p.assets.map(a=>a.id));
    for(const asset of candidate.assets)if(!ids.has(asset.id)){p.assets.push(clone(asset));ids.add(asset.id);}
    p.scene_plan=clone(candidate.scene_plan);
  });
}
/**
 * Concise, compiler-grounded receipt. Shows every shot occurrence, even when
 * no screenshot has reached an actual native H3 image slot. Never invent a
 * <Picture N> for staged Conveyor files or assume shelf index == socket index.
 */
export function renderShotCueReceipt(result, project) {
  const windows=Array.isArray(result?.windows)?result.windows:[];
  const shots=Array.isArray(project?.scene_plan?.shots)?project.scene_plan.shots:[];
  const asSeconds=frames=>String(Number((frames/24).toFixed(6)));
  const lines=[];
  let occurrences=0, active=0, disabled=0, continued=0;
  for(const w of windows){
    const present=(w.scene_shots||[]).map(row=>({...row,used:false}));
    const current=shots.filter(shot=>shot.range[0]<w.range[1]&&w.range[0]<shot.range[1])
      .sort((a,b)=>a.range[0]-b.range[0]||a.range[1]-b.range[1]||a.id.localeCompare(b.id));
    for(const shot of current){
      occurrences++;
      const a=Math.max(shot.range[0],w.range[0]), b=Math.min(shot.range[1],w.range[1]);
      const range="Chunk "+(w.index+1)+" ["+asSeconds(a-w.range[0])+"-"+asSeconds(b-w.range[0])+"s] ";
      if(shot.user?.enabled===false){
        disabled++;lines.push(range+"Disabled shot; no H3 cue.");continue;
      }
      const match=present.find(row=>!row.used&&row.range?.[0]===a&&row.range?.[1]===b);
      if(match){
        match.used=true;
        if(/<Picture\s+\d+>/.test(match.text))active++;
        else continued++;
        lines.push(range+match.text);
      } else {
        const refs=shot.reference_ids?.length||0;
        const reason=refs
          ? project.scene_plan?.reference_transport==="streamed"
            ? "No active H3 <Picture N> alias: this chunk's scene budget is exhausted or the shot was not allocated."
            : project.scene_plan?.conveyor_only
              ? "No active H3 <Picture N> alias. JPEG may be staged in Conveyor; connect its output through Media Resolution, then reimport."
              : "No active H3 <Picture N> alias. Check the reference budget and sampler input routing."
          : "No screenshot selected for this shot.";
        lines.push(range+reason);
      }
    }
  }
  const base=result?.prompt_source==="connected"
    ? "connected State Manager prompt"
    : result?.prompt_source==="connected-unavailable"
      ? "UNVERIFIED — connected State Manager text was not resolved"
      : "Studio prompt fields";
  const transport=project.scene_plan?.reference_transport==="streamed"
    ? " · CHUNK-STREAMED source JPEGs" : " · fixed Conveyor / scoped references";
  const header="SHOT REFERENCE CUES · Base: "+base+transport+
    "\n"+occurrences+" shot window(s) · "+active+" active Picture cue(s) · "+
    disabled+" disabled shot window(s) · "+continued+" carried-context shot window(s)";
  return header+"\n"+(lines.length?lines.join("\n"):
    "No analysed shot intervals in the selected chunk preview.")+
    "\nCue times are logical chunk-local; native physical continuation rebases for carried frames."+
    "\nFull VLM notes and authored dialogue are not repeated here.";
}

export class SmartReferences {
  constructor(editor) {
    this.editor=editor;this.api=editor.api;this.version=editor.editVersion;
    this.project=clone(editor.p);this.candidate=clone(editor.p);this.closed=false;
    this.cfg={sensitivity:1,detect_shots:true,within_shot:true,deduplicate:true,min_shot_frames:3,max_per_shot:2,
      h3_mp:.7,vlm_mp:.25,tone_mapping:"zscale",sdr_nits:125,downsample:"lanczos",scene_budget:3,
      reserved_identity_slots:1,global_budget:128,generate_descriptions:false,...editor.p.scene_plan?.settings};
    this.page=0;this.vlmStop=false;this.contextBudget=8192;
    this.cueRevision=0;this.cueTimer=null;
    this.root=el("dialog",{class:"h3ts-smart","aria-label":"Scene Analysis / Smart References"});
    this.root.addEventListener("cancel",e=>{e.preventDefault();this.close();});
    this.root.addEventListener("keydown",e=>e.stopPropagation());
    this.status=el("output",{text:"Analysis is separate from generation. Review the shot plan, then Apply."});
    this.controls=el("section",{class:"h3ts-smart-controls"});
    this.list=el("section",{class:"h3ts-smart-shots"});
    this.compiled=el("pre",{class:"h3ts-smart-compiled"});
    this.cueReceipt=el("pre",{class:"h3ts-smart-cue-receipt",text:"Timed <Picture N> cues appear here after shot-plan compilation. Unconnected Conveyor screenshots cannot be referenced by H3."});
    this.models=el("select",{"aria-label":"Prompt Writer vision model"});
    this.remoteConsent=el("input",{type:"checkbox","aria-label":"Allow sending extracted stills to remote VLM"});
    this.remoteConsentLabel=el("label",{class:"h3ts-remote-consent"},
      "Allow sharing extracted stills with the selected REMOTE vision API (subject to provider retention policies)",this.remoteConsent);
    this.remoteConsentLabel.hidden=true;
    this.models.onchange=()=>this.updateRemoteConsent();
    this.provider=el("output",{text:"Checking Prompt Writer…"});
    this.root.append(el("header",{},el("strong",{text:"Scene Analysis / Smart References"}),button("Close",()=>this.close())),
      this.status,this.controls,el("nav",{},this.provider,this.models,this.remoteConsentLabel,
        button("Refresh Prompt Writer",()=>this.guard(()=>this.loadModels())),
        button("Interpret selected evidence",()=>this.guard(()=>this.interpret())),
        button("Cancel VLM",()=>this.guard(()=>this.cancelVlm()))),
      el("p",{text:"Detected cuts use exact source PTS. H3 cut timing is advisory. Images are active for their whole chunk. Source faces remain visual composition evidence; dedicated character references control replacement identity."}),
      this.cueReceipt,this.list,el("details",{},el("summary",{text:"Compiled prompts and physical image allocation"}),this.compiled),
      el("footer",{},button("Preview compiled inputs",()=>this.guard(()=>this.refreshCuePreview({immediate:true,showDetails:true}))),
        button("Activate CHUNK-SCOPED H3 references",()=>this.guard(()=>this.activateChunkReferences())),
        button("Stage JPEGs in Conveyor shelf (optional)",()=>this.guard(()=>this.sendToConveyor())),
        el("p",{class:"h3ts-smart-resolution-note",text:"Chunk-streamed H3 references rotate per 7-second chunk. Original identity inputs stay active; only the current shots' JPEGs are loaded. Static Image Conveyor outputs cannot swap tensors inside one queued generation, so the shelf remains an optional library. H3 latent encoding still requires 32px alignment; static Media Resolution nodes do not process streamed images."}),
        button("Apply shot plan",()=>this.guard(()=>this.apply())),
        button("Undo applied plan",()=>this.guard(()=>{if(this.appliedVersion!==editor.editVersion)throw Error("Only the most recently applied shot-plan edit can be undone here. Use Timeline Undo for later edits.");editor.undo();this.appliedVersion=null;this.version=editor.editVersion;this.candidate=clone(editor.p);this.renderShots();})),
        button("Revert pending edits",()=>{this.candidate=clone(editor.p);this.renderShots();this.scheduleCuePreview();})));
    document.body.append(this.root);this.root.showModal();this.renderControls();this.renderShots();
    if(this.candidate.scene_plan)this.scheduleCuePreview();
    editor.smartReferences=this;
    this.guard(()=>this.loadModels());
  }
  async guard(fn){try{return await fn();}catch(error){this.status.textContent="ERROR · "+(error?.message||error);this.status.setAttribute("role","alert");console.error("Smart References",error);}}
  renderControls(){
    const sources=this.project.assets.filter(a=>a.video&&a.kind!=="image");
    this.source=el("select",{"aria-label":"Scene analysis source",title:"Source footage"},...sources.map(a=>el("option",{value:a.id,text:a.name})));
    const field=(name,key,type="number",extra={})=>{
      const input=type==="select"?el("select",{"aria-label":name},...extra.choices.map(value=>el("option",{value,text:value}))):
        el("input",{type,"aria-label":name,...extra});
      if(type==="checkbox")input.checked=this.cfg[key];else input.value=this.cfg[key];
      input.onchange=()=>{this.cfg[key]=type==="checkbox"?input.checked:type==="number"?Number(input.value):input.value;};
      return el("label",{class:"h3ts-smart-control"+(type==="checkbox"?" h3ts-smart-toggle":"")},name,input);
    };
    const context=el("input",{type:"number",min:2048,max:49152,step:1024,value:this.contextBudget,"aria-label":"VLM context token budget"});
    context.onchange=()=>{this.contextBudget=Number(context.value);};
    this.controls.replaceChildren(el("label",{class:"h3ts-smart-control"},"VLM context token budget",context),
      el("label",{class:"h3ts-smart-control h3ts-smart-source"},"Original source",this.source),
      field("Shot detection","detect_shots","checkbox"),field("Within-shot changes","within_shot","checkbox"),
      field("Reuse recurring setups","deduplicate","checkbox"),field("Detection sensitivity","sensitivity","number",{min:.25,max:4,step:.05}),
      field("Maximum screenshots per shot","max_per_shot","number",{min:1,max:4,step:1}),
      el("p",{class:"h3ts-smart-resolution-note",text:"H3 reference JPEG: full decoded source raster, 4:4:4 quality 95. Image Conveyor / Media Resolution handles subsequent resizing."}),
      field("VLM image MP ceiling","vlm_mp","number",{min:.01,max:4,step:.05}),
      field("Color conversion","tone_mapping","select",{choices:["zscale","libplacebo"]}),
      field("VLM preview downsampling","downsample","select",{choices:["lanczos","bicubic","spline"]}),
      field("Scene images per chunk","scene_budget","number",{min:0,max:8,step:1}),
      field("Reserved identity slots","reserved_identity_slots","number",{min:0,max:8,step:1}),
      field("Maximum distinct scene references","global_budget","number",{min:1,max:2048,step:1}),
      el("p",{class:"h3ts-smart-resolution-note",text:"VLM observations stay in the review editor. Only short, explicitly approved H3 shot cues are added to the base prompt."}),
      el("div",{class:"h3ts-smart-actions"},
        button("Analyze selected scene",()=>this.guard(()=>this.analyze("selection"))),
        button("Analyze full source",()=>this.guard(()=>this.analyze("source"))),
        button("Cancel analysis",()=>this.guard(()=>this.cancelAnalysis())),
        button("Load completed analysis",()=>this.guard(()=>this.preview()))));
  }
  async analyze(scope) {
    if(this.job)await this.cancelAnalysis();
    this.project=clone(this.editor.p);this.version=this.editor.editVersion;
    this.candidate=clone(this.project);this.page=0;
    this.clearCuePreview("Analysing source footage. Shot time/Picture mappings will update automatically when analysis completes.");
    const result=await json(this.api,BASE+"/scenes/analyze",{project:this.project,asset_id:this.source.value,scope,settings:this.cfg});
    if(this.closed){await json(this.api,BASE+"/scenes/"+result.id+"/cancel",{});return;}
    this.job=result.id;await this.poll();
  }
  async poll(){
    const id=this.job;
    const state=await json(this.api,BASE+"/scenes/"+id);
    if(this.closed||id!==this.job)return;
    this.status.textContent=[state.stage,state.frames!=null?state.frames+" frames":"",state.cached_frames!=null?state.cached_frames+" cached":"",
      state.shot?"shot "+state.shot+"/"+state.shots:"",state.elapsed_seconds!=null?state.elapsed_seconds+" s":"",state.error||""].filter(Boolean).join(" · ");
    if(state.stage==="complete")await this.preview();
    else if(!["failed","cancelled"].includes(state.stage))this.timer=setTimeout(()=>this.guard(()=>this.poll()),700);
  }
  async cancelAnalysis(){
    clearTimeout(this.timer);if(this.job)await json(this.api,BASE+"/scenes/"+this.job+"/cancel",{});
    this.status.textContent="Cancellation requested. Completed measurements remain reusable; the timeline is unchanged.";
  }
  async preview(){
    if(!this.job)throw Error("Analyze a registered source first");
    // A new preview intentionally rebases onto the latest project, then holds
    // that exact revision until Apply. No old snapshot replaces unrelated edits.
    const version=this.editor.editVersion;
    const result=await json(this.api,BASE+"/scenes/"+this.job+"/preview",{project:this.pendingInterpretation?this.candidate:this.editor.p,model_id:this.modelChoices?.find(m=>m.id===this.models.value)?.id||null});
    if(this.editor.editVersion!==version)throw Error("Timeline changed during preview. Preview again.");
    if(this.closed)return;
    this.version=version;this.candidate=result.project;this.analysis=result.analysis;
    this.renderShots();
    this.status.textContent=this.candidate.scene_plan.shots.length+" mapped shots · review before Apply"+(this.analysis.warnings?.length?" · "+this.analysis.warnings.join(" · "):"");
    this.scheduleCuePreview();
  }
  async loadModels(){
    try{
      const result=await json(this.api,"/h3studio/scene-analysis/capabilities");
      if(result.version!==1||result.schema!=="visual_observations_v1")throw Error("Prompt Writer scene API version is incompatible");
      this.modelChoices=result.models||[];
      this.models.replaceChildren(...this.modelChoices.map(m=>el("option",{value:m.id,text:m.name+" · "+m.api_preset+(m.remote?" · remote API":" · local")})));
      this.provider.textContent=this.modelChoices.length?"Prompt Writer connected vision models":"Connect a vision model in Prompt Writer → Settings → API providers";
      this.updateRemoteConsent();
    }catch(error){this.provider.textContent="VLM unavailable · "+error.message+". Deterministic analysis remains available.";}
  }
  updateRemoteConsent(){
    const model=this.modelChoices?.find(m=>m.id===this.models.value);
    this.remoteConsent.checked=false;
    this.remoteConsentLabel.hidden=!model?.remote;
  }
  async interpret(){
    if(this.vlmActive)throw Error("VLM interpretation is already running");
    if(!this.analysis)throw Error("Analyze footage before VLM interpretation");
    const model=this.modelChoices?.find(m=>m.id===this.models.value);
    if(!model)throw Error("Connect and select a vision model in Prompt Writer");
    if(model.remote&&!this.remoteConsent.checked){
      this.status.textContent="Remote VLM consent required: check the visible 'Allow sharing extracted stills' box before interpreting.";
      this.remoteConsentLabel.hidden=false;this.remoteConsent.focus();
      return;
    }
    this.vlmStop=false;
    this.status.textContent="Preparing source evidence for "+model.name+"…";
    const work=async()=>{
      const unique=new Map();let completed=0,skipped=0;
      for(const shot of this.analysis.shots){
        if(this.vlmStop||this.closed)break;
        if(!shot.reference_ids.length){skipped++;continue;}
        const group=shot.reference_ids.join(",");
        this.status.textContent="Loading evidence for shot "+(completed+skipped+1)+"/"+this.analysis.shots.length+" (source frames "+shot.source_frame_range.join("–")+")…";
        const evidence=await json(this.api,BASE+"/scenes/"+this.job+"/evidence",{shot_id:shot.id,model_id:model.id,context_budget:this.contextBudget});
        if(!evidence.images?.length){skipped++;continue;}
        if(this.vlmStop||this.closed)break;
        let observation=evidence.cached?.observation||unique.get(group);
        if(!observation){
          this.vlmToken=crypto.randomUUID();this.status.textContent="Interpreting shot "+shot.source_frame_range.join("–")+" through Prompt Writer…";
          const result=await json(this.api,"/h3studio/scene-analysis",{version:1,request_token:this.vlmToken,
            api_provider:{connection_id:model.api_connection_id,model_id:model.remote_model},images:evidence.images,context_budget:this.contextBudget});
          if(result.version!==1||result.model_id!==model.id)throw Error("Prompt Writer response identity mismatch");
          observation=result.observation;
        }
        if(this.vlmStop||this.closed)break;
        unique.set(group,observation);
        await json(this.api,BASE+"/scenes/"+this.job+"/observation",{shot_id:shot.id,model_id:model.id,observation,context_budget:this.contextBudget});
        completed++;this.status.textContent="Interpreted "+completed+" shot(s) · skipped "+skipped;
      }
      if(!this.vlmStop&&!this.closed){
        this.pendingInterpretation=true;try{await this.preview();}finally{this.pendingInterpretation=false;}
        this.status.textContent=completed+" shot(s) interpreted. Review observations; optionally write one concise H3 cue per shot. Full descriptions are NOT inserted into H3. Apply shot plan to update conditioning.";
      }
    };
    this.vlmActive=work();
    try{await this.vlmActive;}finally{this.vlmActive=null;this.vlmToken=null;}
  }
  async cancelVlm(){
    this.vlmStop=true;
    if(this.vlmToken)await json(this.api,"/h3studio/scene-analysis/cancel",{request_token:this.vlmToken});
    if(this.vlmActive)await this.vlmActive.catch(()=>{});
  }
  async edit(operation,shot,extra={}){
    const result=await json(this.api,BASE+"/scenes/edit",{project:this.candidate,operation,shot_id:shot.id,...extra});
    this.candidate=result.project;this.renderShots();this.scheduleCuePreview();
  }
  renderShots(){
    const plan=this.candidate.scene_plan;
    if(!plan){this.list.replaceChildren(el("p",{text:"No shot plan. Analyze a source or reopen an applied plan."}));return;}
    const total=plan.shots.length,pages=Math.max(1,Math.ceil(total/20));this.page=Math.min(this.page,pages-1);
    const nav=el("nav",{},button("Previous shots",()=>{this.page=Math.max(0,this.page-1);this.renderShots();}),
      el("output",{text:"Page "+(this.page+1)+"/"+pages+" · "+total+" occurrences · "+Object.keys(plan.references).length+" reusable references"}),
      button("Next shots",()=>{this.page=Math.min(pages-1,this.page+1);this.renderShots();}));
    this.list.replaceChildren(nav);
    for(const shot of plan.shots.slice(this.page*20,this.page*20+20)){
      const enabled=el("input",{type:"checkbox","aria-label":"Use shot "+shot.id});enabled.checked=shot.user?.enabled!==false;
      enabled.onchange=()=>{shot.user||={};shot.user.enabled=enabled.checked;this.scheduleCuePreview();};
      const area=el("textarea",{"aria-label":"Shot description "+shot.id,rows:3});
      area.value=shot.user?.description??shot.vlm?.description??"";
      area.oninput=()=>{shot.user||={};shot.user.description=area.value;shot.user.description_provenance=shot.vlm?"edited_model_observation":"user_authored";};
      const shortCue=el("input",{"aria-label":"Optional concise H3 instruction "+shot.id,
        placeholder:"Optional short framing override · not dialogue",
        maxLength:180});
      shortCue.value=shot.user?.prompt_cue||"";
      shortCue.oninput=()=>{shot.user||={};shot.user.prompt_cue=shortCue.value.trim();this.scheduleCuePreview();};
      const location=el("input",{"aria-label":"Physical scene identity "+shot.id,placeholder:"Unverified location"});
      location.value=shot.scene_id||"";
      location.onchange=()=>{shot.scene_id=location.value||null;shot.user||={};shot.user.scene_id=shot.scene_id;};
      const setup=el("input",{"aria-label":"Camera setup identity "+shot.id});setup.value=shot.setup_id;
      setup.onchange=()=>{shot.setup_id=setup.value;shot.user||={};shot.user.setup_id=setup.value;};
      const subjects=el("input",{"aria-label":"Visible subjects "+shot.id,placeholder:"Anonymous source subjects"});
      subjects.value=(shot.visible_subjects||[]).join("; ");
      subjects.onchange=()=>{shot.visible_subjects=subjects.value.split(";").map(x=>x.trim()).filter(Boolean);shot.user||={};shot.user.visible_subjects=shot.visible_subjects;};
      const refs=el("div",{class:"h3ts-smart-images"});
      for(const rid of shot.reference_ids){
        const ref=plan.references[rid],ex=ref.extraction;
        const h3=ex.assets.find(a=>a.representation==="h3"),master=ex.assets.find(a=>a.representation==="sdr_master");
        const url=(id,type)=>this.api.apiURL?.(BASE+"/media/"+id+"?type="+type)||BASE+"/media/"+id+"?type="+type;
        refs.append(el("figure",{},el("img",{src:url(h3.id,h3.name.endsWith(".jpg")?"image/jpeg":"image/png"),alt:"Source-resolution scene screenshot",loading:"lazy"}),
          el("figcaption",{text:"PTS "+ref.pts+" × "+ref.time_base+" · "+ex.source_dimensions.join("×")+" · screenshot "+h3.width+"×"+h3.height+" · "+h3.megapixels.toFixed(3)+" MP (unscaled)"}),
          el("details",{},el("summary",{text:"Compare source-native SDR conversion"}),el("img",{src:url(master.id,"image/png"),alt:"Source-native display-referred conversion",loading:"lazy"})),
          el("a",{href:this.api.apiURL?.(BASE+"/scenes/reference/"+rid+"/jpg")||BASE+"/scenes/reference/"+rid+"/jpg",download:"shot-"+rid.slice(0,16)+".jpg",text:"Download source-resolution JPG"}),
          el("a",{href:this.api.apiURL?.(BASE+"/scenes/reference/"+rid+"/master")||BASE+"/scenes/reference/"+rid+"/master",text:"Download FFV1 native-depth master"}),
          el("small",{text:ex.warnings.join(" ")})));
      }
      const events=el("details",{},el("summary",{text:"Measured events and confidence"}),
        ...((shot.events||[]).length?(shot.events||[]):[shot.event].filter(Boolean)).map(event=>
          el("p",{text:event.type+" · source PTS "+event.pts+" × "+event.time_base+
            " · confidence "+Math.round(event.confidence*100)+"% · "+(event.evidence?.reason||"Measured event")+
            (event.transition?" · transition PTS ["+event.transition.start_pts+", "+event.transition.end_pts+")":"")})));
      const picker=el("select",{"aria-label":"Replace scene reference "+shot.id},el("option",{value:"",text:"No scene screenshot"}),
        ...Object.entries(plan.references).map(([rid,ref])=>el("option",{value:rid,text:"PTS "+ref.pts+" × "+ref.time_base+" · "+rid.slice(0,8)})));
      picker.value=shot.reference_ids[0]||"";
      picker.onchange=()=>this.guard(()=>this.edit("references",shot,{reference_ids:picker.value?[picker.value]:[],setup_id:crypto.randomUUID()}));
      const split=el("input",{type:"number",min:shot.range[0]+1,max:shot.range[1]-1,value:Math.floor((shot.range[0]+shot.range[1])/2),"aria-label":"Split shot at project frame"});
      const next=plan.shots[plan.shots.indexOf(shot)+1];
      this.list.append(el("article",{},el("h3",{text:"Project ["+shot.range.join(",")+") · source ["+shot.source_start+", "+shot.source_end+")"}),
        el("p",{text:"Setup "+shot.setup_id.slice(0,12)+(shot.reused_reference?" · reused reference":" · distinct evidence")+" · "+(shot.event?.type||(shot.provenance==="user-split"?"user boundary":"source start"))+" · "+(shot.event?.evidence?.reason||"measured source interval")}),
        el("label",{},enabled,"Use this shot"),
        el("label",{},"Physical scene/location",location),el("label",{},"Camera setup",setup),el("label",{},"Visible subjects",subjects),events,refs,el("label",{},"Full VLM observation / user notes (review only; NOT sent to H3)",area),
        el("label",{},"Optional concise H3 shot cue (max 180 characters)",shortCue),
        el("p",{text:shot.vlm?"Model-generated observation · "+shot.vlm.model_id+" · review visual claims":"No semantic model observation. Facial visibility and acting are unmeasured."}),
        picker,split,button("Split shot",()=>this.guard(()=>this.edit("split",shot,{frame:Number(split.value)}))),
        button("Merge with next",()=>this.guard(()=>this.edit("merge",shot,{other_id:next?.id})))));
    }
  }
  updatePlanSettings(){
    const before=this.candidate.scene_plan.settings;
    const editable=new Set(["scene_budget","reserved_identity_slots","generate_descriptions"]);
    for(const [key,value] of Object.entries(this.cfg))
      if(!editable.has(key)&&before[key]!==value)
        throw Error("Analysis/extraction settings changed. Reanalyze before applying or compiling these references.");
    this.candidate.scene_plan.settings={...this.cfg};
  }
  clearCuePreview(message="No analysed shot plan available."){
    clearTimeout(this.cueTimer);this.cueRevision++;
    this.cueReceipt.textContent=message;
  }
  scheduleCuePreview(){
    clearTimeout(this.cueTimer);
    const revision=++this.cueRevision;
    if(this.closed||!this.candidate.scene_plan){
      this.cueReceipt.textContent="No analysed shot plan available.";return;
    }
    this.cueReceipt.textContent="Resolving selected shot timestamps, connected base prompt and active H3 Picture aliases…";
    this.cueTimer=setTimeout(()=>this.compile(revision).catch(error=>this.cueFailure(revision,error)),150);
  }
  cueFailure(revision,error){
    if(revision!==this.cueRevision||this.closed)return;
    this.cueReceipt.textContent="SHOT REFERENCE PREVIEW ERROR · "+(error?.message||error)+
      "\nAnalysis remains available. No H3 cue has been verified.";
    console.error("Smart References cue compilation",error);
  }
  async refreshCuePreview({immediate=false,showDetails=false}={}){
    clearTimeout(this.cueTimer);
    const revision=++this.cueRevision;
    this.cueReceipt.textContent="Resolving the effective prompt and active reference inputs…";
    const go=()=>this.compile(revision,showDetails).catch(error=>this.cueFailure(revision,error));
    if(immediate)return go();
    this.cueTimer=setTimeout(go,150);
  }
  async compile(revision=++this.cueRevision,showDetails=false){
    if(!this.candidate.scene_plan)throw Error("Analyze footage first");
    this.updatePlanSettings();
    const sources=referenceSocketSources(this.editor);
    const sizes=Array.from({length:8},(_,i)=>sources[i]?.connected&&!sources[i]?.unavailable?1:0);
    const connectedPrompt=connectedPromptForPreview(this.editor);
    const result=await json(this.api,BASE+"/scenes/compile",{project:this.candidate,slot_sizes:sizes,
      ...(connectedPrompt===null?{}:{external_prompt:connectedPrompt})});
    if(revision!==this.cueRevision||this.closed)return;
    this.cueReceipt.textContent=renderShotCueReceipt(result,this.candidate);
    this.compiled.textContent="Base prompt: "+(
      result.prompt_source==="connected" ? "CONNECTED State Manager Text Box" :
      result.prompt_source==="connected-unavailable"
        ? "UNVERIFIED — connected State Manager Text Box could not be read; Studio draft shown only for diagnostics"
        : "Studio prompt fields")+
      ".\nSocket counts are inferred from graph outputs; bundled/dynamic tensors are sampler-authoritative.\n"+
      "Cue times are per logical 7-second chunk here; physical H3 adds retained-context offset.\n"+
      "Full VLM descriptions stay in review; only a separately approved short H3 cue is allowed.\n\n"+result.windows.map(w=>
      "Chunk "+(w.index+1)+" ["+w.range.join(",")+")\n"+JSON.stringify({reference_budget:w.reference_budget,aliases:w.aliases,images:w.images,warnings:w.warnings},null,2)+"\n"+w.prompt).join("\n\n");
    if(showDetails)this.compiled.parentElement.open=true;
  }
  async activateChunkReferences(){
    if(!this.candidate.scene_plan)throw Error("Analyze shots before activating per-chunk references.");
    if(this.editor.editVersion!==this.version)
      throw Error("Timeline changed; reopen Smart References before activating images.");
    this.updatePlanSettings();
    const sources=referenceSocketSources(this.editor);
    const plan=planChunkReferenceStream(this.candidate,sources);
    // Use the whole authored scene, not a possibly one-chunk UI selection:
    // all scene refs are stored persistently and selected lazily at generation.
    const seen=plan.stats.slice(0,10).map(s=>
      "Chunk "+(s.index+1)+": "+s.identityEstimate+" identity + "+
      s.accepted+" screenshot(s) = "+(s.identityEstimate+s.accepted)+
      (s.omitted?" ("+s.omitted+" over budget)":"")).join("\n");
    const omitted=plan.stats.filter(s=>s.omitted).length;
    if(!window.confirm(
      "Activate dynamic per-chunk H3 screenshot references for "+plan.stats.length+
      " chunk(s)?\n"+plan.bank.length+" distinct selected source screenshots in the library.\n\n"+
      seen+(plan.stats.length>10?"\n…":"")+
      (omitted?"\nWARNING: "+omitted+" chunks exceed the current scene-reference budget.":"")+
      "\n\nExisting identity image sockets stay connected and selected. "+
      "Source JPEG files remain unmodified. H3 performs its own 32px latent-alignment preprocessing "+
      "(this is not the static Media Resolution node). No Image Conveyor shelf overwrite or new graph wiring."
    ))return;
    const updatedPlan=clone(this.candidate.scene_plan);
    updatedPlan.reference_transport="streamed";
    updatedPlan.conveyor_only=true;
    updatedPlan.conveyor_slots={};
    // The old fixed-shelf mapping is invalid for a dynamic source bank.
    // Each chunk keeps ONLY existing connected identity inputs; its unique
    // scene refs are loaded by TimelineInputs.materialize on that chunk.
    this.editor.edit(p=>{
      const registered=new Set(p.assets.map(a=>a.id));
      for(const asset of this.candidate.assets||[]){
        if(!registered.has(asset.id)){p.assets.push(clone(asset));registered.add(asset.id);}
      }
      p.scene_plan=updatedPlan;
      p.conveyor_reference_slots=clone(plan.perChunk);
    });
    this.version=this.editor.editVersion;
    this.appliedVersion=this.version;
    this.candidate=clone(this.editor.p);
    this.renderShots();
    this.editor.scheduleChunkCompile?.();
    this.editor.refreshSamplerInputs?.();
    this.status.textContent="CHUNK-STREAMING ACTIVE: "+plan.stats.length+
      " chunks, "+plan.bank.length+" unique screenshot assets, "+
      plan.identityEstimate+" persistent identity images. "+
      "Each chunk receives only its own selected stills. "+
      (omitted?omitted+" chunk(s) exceed budget; inspect the cue receipt. ":"")+
      "Unmodified JPEGs persist in Timeline's asset store; the Conveyor shelf is optional.";
    await this.refreshCuePreview({immediate:true});
  }
  async sendToConveyor(){
    if(!this.candidate.scene_plan)throw Error("Analyze and review a shot plan before importing screenshots.");
    if(this.editor.editVersion!==this.version)
      throw Error("Studio changed. Reopen Smart References to avoid replacing newer edits.");
    this.updatePlanSettings();
    const plan=this.candidate.scene_plan;
    // An extracted screenshot is a reusable asset, not one shot occurrence.
    // Export every reference belonging to the selected source/time region,
    // even when a shot is disabled for conditioning. Disabling a shot never
    // destroys its screencap. Do not silently drop useful extracted images.
    const range=this.candidate.selection||[0,this.candidate.duration_frames];
    const selection=screenshotIdsForSelection(plan,range,this.analysis?.source_asset_id);
    const relevant=selection.shots,ids=selection.ids;
    const activeIds=new Set(selection.enabledIds);
    if(!ids.length)throw Error("No enabled screenshot references to publish.");
    if(ids.length>8)throw Error("The selected scene plan uses "+ids.length+
      " unique images, but one Image Conveyor Reference Shelf holds 8. "+
      "Reduce the enabled shot/reference set or distribute references among multiple runs.");
    const graph=this.editor.app?.graph;
    const sources=referenceSocketSources(this.editor);
    const reachable=[...new Set(sources.flatMap(s=>[
      ...(s.conveyor&&s.nodeId!=null?[s.nodeId]:[]),
      ...(s.candidatePreviews||[]).map(x=>x.nodeId),
    ]).filter(x=>x!=null))];
    // If a custom Media Resolution intermediary hides provenance, still
    // permit *staging* into a single local Conveyor without asserting the
    // images flow to H3. Never invent physical routes from node presence.
    const allConveyors=(graph?._nodes||[]).filter(node=>
      ["ImageConveyor","SequentialBatchImageLoader"].includes(node.comfyClass||node.type));
    const uniqueFallback=allConveyors.length===1 ? allConveyors[0].id : null;
    const nodes=reachable.length?reachable:(uniqueFallback==null?[]:[uniqueFallback]);
    if(nodes.length!==1)
      throw Error(nodes.length===0
        ? "No unique Image Conveyor found in this graph. Found "+allConveyors.length+
          " Conveyor nodes and "+sources.filter(x=>x.connected).length+
          " linked H3 reference sockets; inspect the connection topology."
        : "Reference inputs originate from "+nodes.length+
          " Image Conveyors. A single import cannot safely choose one; select a unique source.");
    const node=graph.getNodeById?.(nodes[0])||
      graph._nodes.find(x=>x.id===nodes[0]);
    const widget=node?.widgets?.find(w=>w.name==="state_json");
    if(!widget)throw Error("Image Conveyor #"+nodes[0]+" is missing its state_json widget.");
    const before=JSON.parse(widget.value);
    const toggles=node.properties?.image_conveyor_reference_enabled;
    const installation=planConveyorPublication(ids,before,sources,node.id,toggles);
    const routesText=installation.knownRoutes+" traced H3 socket route(s)";
    if(!window.confirm("Found "+relevant.length+" shot occurrences, "+ids.length+" distinct selected screencaps, "+
      activeIds.size+" used by enabled shots.\n\n"+
      "Add all "+ids.length+" full-resolution JPG(s) to Image Conveyor #"+node.id+
      "? "+installation.routed.length+" screenshot(s) have connected H3 reference routes; "+
      installation.staged.length+" will be stored in unconnected shelf positions. "+
      "Existing character references stay untouched. "+
      (before.active_reference_preset_id?"The active named preset will be detached. ":"")+
      "Image Conveyor changes are not undone by Timeline Undo."))return;
    this.status.textContent="Publishing full-resolution JPEG screenshots to ComfyUI input…";
    const result=await json(this.api,BASE+"/scenes/conveyor/publish",{reference_ids:ids});
    const updated=JSON.parse(widget.value);
    if(updated.output_mode!==before.output_mode ||
       JSON.stringify(updated.reference_slots)!==JSON.stringify(before.reference_slots))
      throw Error("Image Conveyor changed during export. Files were saved, but the shelf was not modified.");
    const supplied=new Map((result.references||[]).map(x=>[x.id,x]));
    if(supplied.size!==ids.length)
      throw Error("Incomplete JPEG export; shelf was not modified.");
    for(const entry of installation.assignments){
      const image=supplied.get(entry.id);
      if(!image || image.annotated!==entry.annotated)
        throw Error("Exported screenshot identity mismatch; shelf was not modified.");
      const previous=updated.reference_slots[entry.shelfNumber-1];
      if(previous && previous.annotated!==entry.annotated)
        throw Error("Shelf Ref "+entry.shelfNumber+" became occupied; no references overwritten.");
      updated.reference_slots[entry.shelfNumber-1]={
        annotated:image.annotated,filename:image.filename,
        subfolder:image.subfolder,type:"input",
      };
    }
    // Never replace a user's saved character preset as a side effect.
    updated.active_reference_preset_id="";
    widget.value=JSON.stringify(updated);
    widget.callback?.(widget.value);
    if(node.__bil){
      node.__bil.state=updated;
      node.__bil.queueRevision=(node.__bil.queueRevision||0)+1;
      node.__bil.renderVersion=(node.__bil.renderVersion||0)+1;
      node.__bil.renderedRangeKey="";
    }
    node.setDirtyCanvas?.(true,true);
    node.graph?.change?.();
    if(plan.reference_transport==="streamed"){
      // Static Conveyor stage/preview state is independent of the virtual
      // per-chunk scene stream. NEVER add the same JPEG to the H3 reference
      // socket masks as well: that would silently double-condition it and
      // change Picture numbering mid-sequence.
      this.status.textContent="Staged "+ids.length+" screenshot JPEG(s) in Image Conveyor #"+node.id+
        " for visual inspection. Dynamic H3 chunk streaming remains active; "+
        "no static shelf slots were added to the sampler, and identities/masks are unchanged.";
      await this.refreshCuePreview({immediate:true});
      return;
    }
    // Source-shelf indices and physical sampler socket indices are different
    // for rerouted paths. Never infer that shelf 5 means reference_image_5.
    const mapped=Object.fromEntries(installation.routed.map(entry=>
      [entry.id,entry.physicalSlot]));
    const previousMap=plan.conveyor_slots||{};
    const preservedMap=Object.fromEntries(Object.entries(previousMap)
      .filter(([rid])=>!ids.includes(rid)));
    const activeSources=sources.filter(s=>s.connected&&!s.unavailable).map(s=>s.slot);
    const previousMasks=this.candidate.conveyor_reference_slots||{};
    const chunkSize=this.candidate.chunk_frames;
    const origin=this.candidate.chunk_origin_frame||0;
    const count=Math.ceil((this.candidate.duration_frames-origin)/chunkSize);
    const masks={};
    for(let index=0;index<count;index++){
      const low=origin+index*chunkSize,high=Math.min(this.candidate.duration_frames,low+chunkSize);
      const initial=Object.hasOwn(previousMasks,String(index))
        ? previousMasks[String(index)] : activeSources;
      const shotSockets=plan.shots.filter(s=>s.user?.enabled!==false&&s.range[0]<high&&low<s.range[1])
        .flatMap(s=>s.reference_ids.map(id=>mapped[id]).filter(Number.isInteger));
      masks[String(index)]=[...new Set([...initial,...shotSockets])].sort((a,b)=>a-b);
    }
    // Applying the shot plan must also register its extracted asset records.
    // Previously the direct Conveyor action skipped these, leaving a scene
    // plan that referenced IDs absent from project.assets.
    this.editor.edit(p=>{
      const known=new Set(p.assets.map(a=>a.id));
      for(const image of this.candidate.assets||[]){
        if(!known.has(image.id)){p.assets.push(clone(image));known.add(image.id);}
      }
      p.scene_plan=clone({...plan,conveyor_only:true,
        conveyor_slots:{...preservedMap,...mapped}});
      p.conveyor_reference_slots=masks;
    });
    this.version=this.editor.editVersion;
    this.appliedVersion=this.version;
    this.candidate=clone(this.editor.p);
    this.renderShots();
    this.editor.scheduleChunkCompile?.();
    this.editor.refreshSamplerInputs?.();
    this.status.textContent="Exported "+ids.length+" full-resolution JPEG(s) to Image Conveyor #"+node.id+
      " ("+routesText+"). Active H3 routes: "+
      installation.routed.map(x=>"shelf "+x.shelfNumber+" → reference_image_"+x.physicalSlot).join(", ")+
      (installation.staged.length
        ? ". STAGED ONLY (not fed to H3): shelf "+
          installation.staged.map(x=>x.shelfNumber).join(", ")+
          ". Connect those shelf outputs through Media Resolution to unused sampler reference inputs, then reopen this action to activate them. "+
          "Conveyor-only mode is active: staged screenshots are NOT silently injected through the native scoped-reference fallback."
        : ". Every installed screenshot has a connected H3 reference route.")+
      " JPEG files: ComfyUI/input/h3_timeline_screencaps/.";
    await this.refreshCuePreview({immediate:true});
  }
  apply(){
    if(!this.candidate.scene_plan)throw Error("Analyze footage first");
    this.updatePlanSettings();
    applySceneCandidate(this.editor,this.candidate,this.version);this.version=this.editor.editVersion;this.appliedVersion=this.version;
    this.editor.scheduleChunkCompile?.();
    this.scheduleCuePreview();
    this.status.textContent="Shot plan applied. The effective prompt merges connected State Manager text with short bracketed Picture cues. Inspect the automatically updated cue receipt. The authored State Manager widget is unchanged. No generation was queued.";
  }
  close(){
    if(this.closed)return;
    this.closed=true;clearTimeout(this.timer);clearTimeout(this.cueTimer);this.cueRevision++;
    this.root.close();this.root.remove();
    this.guard(()=>this.cancelAnalysis());this.guard(()=>this.cancelVlm());
    // Keep a live client registered until cancellation really finishes so a
    // simultaneous queue cannot bypass the handoff by closing this window.
    Promise.resolve(this.cancelVlm()).finally(()=>{if(this.editor.smartReferences===this)this.editor.smartReferences=null;});
  }
}

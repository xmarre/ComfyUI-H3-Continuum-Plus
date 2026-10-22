// Smart References is a detached analysis workspace. Apply uses the editor's
// existing History transaction and rejects results after concurrent edits.
import { clone } from "./core.js";
import { referenceSocketSources, connectedPromptForPreview } from "./chunk_ui.js?v=h3-timeline-smart-references-v31";
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
export class SmartReferences {
  constructor(editor) {
    this.editor=editor;this.api=editor.api;this.version=editor.editVersion;
    this.project=clone(editor.p);this.candidate=clone(editor.p);this.closed=false;
    this.cfg={sensitivity:1,detect_shots:true,within_shot:true,deduplicate:true,min_shot_frames:3,max_per_shot:2,
      h3_mp:.7,vlm_mp:.25,tone_mapping:"zscale",sdr_nits:125,downsample:"lanczos",scene_budget:3,
      reserved_identity_slots:1,global_budget:128,generate_descriptions:false,...editor.p.scene_plan?.settings};
    this.page=0;this.vlmStop=false;this.contextBudget=8192;
    this.root=el("dialog",{class:"h3ts-smart","aria-label":"Scene Analysis / Smart References"});
    this.root.addEventListener("cancel",e=>{e.preventDefault();this.close();});
    this.root.addEventListener("keydown",e=>e.stopPropagation());
    this.status=el("output",{text:"Analysis is separate from generation. Review the shot plan, then Apply."});
    this.controls=el("section",{class:"h3ts-smart-controls"});
    this.list=el("section",{class:"h3ts-smart-shots"});
    this.compiled=el("pre",{class:"h3ts-smart-compiled"});
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
      this.list,el("details",{},el("summary",{text:"Compiled prompts and physical image allocation"}),this.compiled),
      el("footer",{},button("Preview compiled inputs",()=>this.guard(()=>this.compile())),
        button("Send selected JPGs to Image Conveyor",()=>this.guard(()=>this.sendToConveyor())),
        button("Apply shot plan",()=>this.guard(()=>this.apply())),
        button("Undo applied plan",()=>this.guard(()=>{if(this.appliedVersion!==editor.editVersion)throw Error("Only the most recently applied shot-plan edit can be undone here. Use Timeline Undo for later edits.");editor.undo();this.appliedVersion=null;this.version=editor.editVersion;this.candidate=clone(editor.p);this.renderShots();})),
        button("Revert pending edits",()=>{this.candidate=clone(editor.p);this.renderShots();})));
    document.body.append(this.root);this.root.showModal();this.renderControls();this.renderShots();
    editor.smartReferences=this;
    this.guard(()=>this.loadModels());
  }
  async guard(fn){try{return await fn();}catch(error){this.status.textContent="ERROR · "+(error?.message||error);this.status.setAttribute("role","alert");console.error("Smart References",error);}}
  renderControls(){
    const sources=this.project.assets.filter(a=>a.video&&a.kind!=="image");
    this.source=el("select",{"aria-label":"Scene analysis source"},...sources.map(a=>el("option",{value:a.id,text:a.name})));
    const field=(name,key,type="number",extra={})=>{
      const input=type==="select"?el("select",{"aria-label":name},...extra.choices.map(value=>el("option",{value,text:value}))):
        el("input",{type,"aria-label":name,...extra});
      if(type==="checkbox")input.checked=this.cfg[key];else input.value=this.cfg[key];
      input.onchange=()=>{this.cfg[key]=type==="checkbox"?input.checked:type==="number"?Number(input.value):input.value;};
      return el("label",{},name,input);
    };
    const context=el("input",{type:"number",min:2048,max:49152,step:1024,value:this.contextBudget,"aria-label":"VLM context token budget"});
    context.onchange=()=>{this.contextBudget=Number(context.value);};
    this.controls.replaceChildren(el("label",{},"VLM context token budget",context),el("label",{},"Original source",this.source),
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
      button("Analyze selected scene",()=>this.guard(()=>this.analyze("selection"))),
      button("Analyze full source",()=>this.guard(()=>this.analyze("source"))),
      button("Cancel analysis",()=>this.guard(()=>this.cancelAnalysis())),
      button("Load completed analysis",()=>this.guard(()=>this.preview())));
  }
  async analyze(scope) {
    if(this.job)await this.cancelAnalysis();
    this.project=clone(this.editor.p);this.version=this.editor.editVersion;
    this.candidate=clone(this.project);this.page=0;
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
    this.renderShots();this.status.textContent=this.candidate.scene_plan.shots.length+" mapped shots · review before Apply"+(this.analysis.warnings?.length?" · "+this.analysis.warnings.join(" · "):"");
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
    this.candidate=result.project;this.renderShots();
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
      enabled.onchange=()=>{shot.user||={};shot.user.enabled=enabled.checked;};
      const area=el("textarea",{"aria-label":"Shot description "+shot.id,rows:3});
      area.value=shot.user?.description??shot.vlm?.description??"";
      area.oninput=()=>{shot.user||={};shot.user.description=area.value;shot.user.description_provenance=shot.vlm?"edited_model_observation":"user_authored";};
      const shortCue=el("input",{"aria-label":"Optional concise H3 instruction "+shot.id,
        placeholder:"Optional short framing override · not dialogue",
        maxLength:180});
      shortCue.value=shot.user?.prompt_cue||"";
      shortCue.onchange=()=>{shot.user||={};shot.user.prompt_cue=shortCue.value.trim();};
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
  async compile(){
    if(!this.candidate.scene_plan)throw Error("Analyze footage first");
    this.updatePlanSettings();
    const sources=referenceSocketSources(this.editor);
    const sizes=Array.from({length:8},(_,i)=>sources[i]?.connected&&!sources[i]?.unavailable?1:0);
    const connectedPrompt=connectedPromptForPreview(this.editor);
    const result=await json(this.api,BASE+"/scenes/compile",{project:this.candidate,slot_sizes:sizes,
      ...(connectedPrompt===null?{}:{external_prompt:connectedPrompt})});
    this.compiled.textContent="Base prompt: "+(result.prompt_source==="connected"?"CONNECTED State Manager Text Box":"Studio fields (connected text unavailable or explicitly disabled)")+
      ".\nSocket counts are inferred from graph outputs; bundled/dynamic tensors are sampler-authoritative.\n"+
      "Cue times are per logical 7-second chunk here; physical H3 adds retained-context offset.\n"+
      "Full VLM descriptions stay in review; only a separately approved short H3 cue is allowed.\n\n"+result.windows.map(w=>
      "Chunk "+(w.index+1)+" ["+w.range.join(",")+")\n"+JSON.stringify({reference_budget:w.reference_budget,aliases:w.aliases,images:w.images,warnings:w.warnings},null,2)+"\n"+w.prompt).join("\n\n");
    this.compiled.parentElement.open=true;
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
  }
  apply(){
    if(!this.candidate.scene_plan)throw Error("Analyze footage first");
    this.updatePlanSettings();
    applySceneCandidate(this.editor,this.candidate,this.version);this.version=this.editor.editVersion;this.appliedVersion=this.version;
    this.editor.scheduleChunkCompile?.();
    this.status.textContent="Shot plan applied. The effective prompt merges the connected State Manager source with short bracketed Picture cues; review each chunk's effective H3 prompt field. The authored State Manager widget remains unchanged. No generation was queued.";
  }
  close(){
    if(this.closed)return;
    this.closed=true;clearTimeout(this.timer);this.root.close();this.root.remove();
    this.guard(()=>this.cancelAnalysis());this.guard(()=>this.cancelVlm());
    // Keep a live client registered until cancellation really finishes so a
    // simultaneous queue cannot bypass the handoff by closing this window.
    Promise.resolve(this.cancelVlm()).finally(()=>{if(this.editor.smartReferences===this)this.editor.smartReferences=null;});
  }
}

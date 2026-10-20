import { linkedSampler } from "./chunk_ui.js";
import { promptSourceNodes, connectedTextSource, selectedPresetSource } from "./import_state.js";

const PREFIX="/h3_continuum/timeline";
const labels={global:"Global prompt",chunks:"Chunk prompts",video:"Video / pictures",soundtrack:"Soundtrack"};
function el(tag,props={},...children){const n=document.createElement(tag);for(const [k,v] of Object.entries(props)){if(k==="text")n.textContent=v;else if(k.startsWith("on"))n.addEventListener(k.slice(2),v);else n.setAttribute(k,String(v));}n.append(...children.filter(x=>x!=null));return n;}
function button(text,fn){return el("button",{type:"button",text,onclick:fn});}
async function request(editor,path,options={}){const r=await editor.api.fetchApi(PREFIX+path,options);const d=await r.json();if(!r.ok)throw Error(d.error||"Import request failed");return d;}

export function applyImportPreview(editor,preview,version){
  if(version!==editor.editVersion)throw Error("The timeline changed after preview. Preview the import again before applying.");
  editor.edit(project=>{
    for(const key of Object.keys(project))delete project[key];
    Object.assign(project,preview.project);
    if(preview.target==="replace"){
      editor.selected=[];editor.playhead=project.selection[0];editor.chunkPage=0;editor.sceneDraft=null;
    }
  });
  editor.message("Import applied in one Undo step · "+preview.source_ownership);
}

export class ImportDialog {
  constructor(editor,file=null){
    this.editor=editor;this.abort=new AbortController();this.epoch=0;this.inputVersion=0;
    this.token=null;this.provenance={kind:"paste"};this.preview=null;this.selected={};
    this.root=el("dialog",{class:"h3ts-import","aria-label":"Import timeline content"});
    this.root.addEventListener("keydown",e=>e.stopPropagation());
    this.root.addEventListener("cancel",e=>{e.preventDefault();this.close();});
    this.error=el("pre",{class:"h3ts-import-error",role:"alert"});
    this.status=el("output",{text:"Choose a source, select components, preview, then apply."});
    this.text=el("textarea",{rows:7,"aria-label":"Prompt text to import",placeholder:"Paste JSON, timed sections, chunk headings, a list, or a global prompt."});
    this.text.addEventListener("input",()=>{this.available=null;this.selected=null;this.changed();});
    this.file=el("input",{type:"file",accept:".zip,.json,.txt,.md,video/*,audio/*,image/*"});
    this.file.hidden=true;
    this.file.addEventListener("change",()=>this.guard(()=>this.loadFile(this.file.files?.[0])));
    const graph=linkedSampler(editor)?.graph||editor.app?.graph;
    this.sources=promptSourceNodes(graph,linkedSampler(editor));
    this.manager=el("select",{"aria-label":"State Manager preset source"},el("option",{value:"",text:"Choose State Manager…"}));
    for(const n of this.sources.managers)this.manager.append(el("option",{value:n.id,text:(n.title||"State Manager")+" #"+n.id}));
    if(this.sources.manager)this.manager.value=String(this.sources.manager.id);
    else if(this.sources.managers.length===1)this.manager.value=String(this.sources.managers[0].id);
    this.document=el("select",{"aria-label":"Prompt document in archive"});this.document.hidden=true;
    this.document.addEventListener("change",()=>{this.selected=null;this.changed();this.guard(()=>this.inspect());});
    this.components=el("div",{class:"h3ts-import-components"});
    this.target=el("select",{"aria-label":"Import target"},
      el("option",{value:"merge",text:"Merge into current scene"}),
      el("option",{value:"replace",text:"Replace scene"}));
    this.target.addEventListener("change",()=>this.changed());
    this.soundMode=el("select",{"aria-label":"Soundtrack import mode"},
      el("option",{value:"replace",text:"Replace current soundtrack"}),
      el("option",{value:"add",text:"Add soundtrack to current mix"}));
    this.soundMode.addEventListener("change",()=>this.changed());
    this.timeOrigin=el("select",{"aria-label":"Prompt timestamp origin"},
      el("option",{value:"auto",text:"Scene times (auto-detect clear movie timestamps)"}),
      el("option",{value:"scene",text:"Seconds from scene start"}),
      el("option",{value:"movie",text:"Seconds from movie start"}));
    this.timeOrigin.addEventListener("change",()=>this.changed());
    this.details=el("div",{class:"h3ts-import-preview"});
    this.apply=button("Apply import",()=>this.guard(async()=>{
      applyImportPreview(editor,this.preview,this.previewVersion);
      this.close();
    }));this.apply.disabled=true;
    const inspectButton=button("Use pasted / edited text",()=>this.guard(async()=>{
      await this.discardSource();this.provenance={kind:"paste"};this.selected=null;this.changed();await this.inspect();
    }));
    this.root.append(el("h2",{text:"Import timeline content"}),
      el("p",{text:"Read ZIP, JSON, text, media or State Manager. Only selected components change; apply is one Undo step."}),
      el("div",{class:"h3ts-import-toolbar"},button("Choose file…",()=>{this.file.value="";this.file.click();}),this.file,
        button("Read connected Text Box",()=>this.guard(()=>this.readState(false))),this.manager,
        button("Read selected preset",()=>this.guard(()=>this.readState(true)))),
      this.text,inspectButton,this.document,this.components,
      el("label",{},"Target ",this.target),el("label",{},"Soundtrack ",this.soundMode),el("label",{},"Prompt times ",this.timeOrigin),
      el("p",{text:"Confirmed prompt imports use the Studio fields. The connected sequence_prompt stays wired and is ignored until you explicitly choose Use connected prompts again."}),
      this.status,this.error,this.details,
      el("footer",{},button("Preview mapping",()=>this.guard(()=>this.showPreview())),this.apply,button("Cancel",()=>this.close())));
    document.body.append(this.root);this.root.showModal();
    this.parentClose=()=>this.close();editor.abort.signal.addEventListener("abort",this.parentClose,{once:true});
    if(file)this.guard(()=>this.loadFile(file));
  }
  changed(){this.inputVersion++;this.epoch++;this.preview=null;this.apply.disabled=true;this.error.textContent="";}
  async guard(fn){try{await fn();}catch(e){if(!this.abort.signal.aborted&&e.name!=="AbortError"){this.error.textContent=e.message;this.apply.disabled=true;}}}
  async discardSource(){const token=this.token;this.token=null;if(token)await request(this.editor,"/imports/source/"+token,{method:"DELETE"}).catch(()=>{});}
  async loadFile(file){
    this.loading=true;
    try {
    if(!file)return;
    this.changed();const epoch=this.epoch;
    this.status.textContent="Reading import source…";
    const source=await request(this.editor,"/imports/source?name="+encodeURIComponent(file.name),{method:"POST",body:file,signal:this.abort.signal});
    if(epoch!==this.epoch||this.abort.signal.aborted){request(this.editor,"/imports/source/"+source.token,{method:"DELETE"}).catch(()=>{});return;}
    await this.discardSource();
    if(epoch!==this.epoch||this.abort.signal.aborted){request(this.editor,"/imports/source/"+source.token,{method:"DELETE"}).catch(()=>{});return;}
    this.token=source.token;this.provenance={kind:"file",name:file.name};
    this.document.replaceChildren(...source.documents.map((d,i)=>el("option",{value:i,text:d.name})));
    this.document.hidden=!source.documents.length;
    this.document.value="0";this.text.value="";this.text.disabled=true;
    this.selected=null;await this.inspect();
    } finally {this.loading=false;}
  }
  async readState(preset){
    this.loading=true;
    try {
    this.changed();const epoch=this.epoch;
    const sampler=linkedSampler(this.editor),graph=sampler?.graph||this.editor.app?.graph;
    const manager=this.sources.managers.find(n=>String(n.id)===this.manager.value);
    const source=preset?await selectedPresetSource(this.editor.api,manager,this.sources.textBox):connectedTextSource(graph,sampler);
    if(epoch!==this.epoch||this.abort.signal.aborted)return;
    await this.discardSource();
    if(epoch!==this.epoch||this.abort.signal.aborted)return;
    this.text.disabled=false;this.text.value=source.text;this.provenance=source.provenance;
    this.document.hidden=true;this.selected=null;this.changed();await this.inspect();
    } finally {this.loading=false;}
  }
  payload(selected=this.selected){return {project:this.editor.p,token:this.token,text:this.token?undefined:this.text.value,
    document_index:Number(this.document.value)||0,selected,target:this.target.value,
    soundtrack_mode:this.soundMode.value,time_origin:this.timeOrigin.value,provenance:this.provenance};}
  async inspect(){
    const epoch=this.epoch;
    const result=await request(this.editor,"/imports/preview",{method:"POST",body:JSON.stringify({...this.payload({global:false,chunks:false,video:false,soundtrack:false}),target:"merge"}),signal:this.abort.signal});
    if(epoch!==this.epoch||this.abort.signal.aborted)return;
    this.available=result.available;this.components.replaceChildren();
    if(result.prompt_error)this.error.textContent=result.prompt_error;
    this.selected ||= {...result.available};
    for(const [key,label] of Object.entries(labels)){
      const c=el("input",{type:"checkbox","aria-label":"Import "+label});c.checked=!!this.selected[key];c.disabled=!result.available[key];
      c.addEventListener("change",()=>{this.selected[key]=c.checked;this.changed();this.updateTarget();});
      this.components.append(el("label",{},c,label+(result.available[key]?"":" · not present")));
    }
    this.text.disabled=!!this.token;this.updateTarget();
    this.status.textContent="Detected "+result.format+" · "+result.detected_media.length+" media file(s). Choose components and preview.";
    this.details.replaceChildren();
    if(result.detected_media.length)this.details.append(el("pre",{text:result.detected_media.map(m=>m.component+": "+m.name).join("\n")}));
  }
  updateTarget(){const media=!!(this.selected?.video||this.selected?.soundtrack);this.target.options[1].disabled=!media;if(!media)this.target.value="merge";this.soundMode.disabled=!this.selected?.soundtrack;}
  async showPreview(){
    if(this.loading)throw Error("Wait for the new source to finish loading before previewing.");
    if(!this.available)await this.inspect();
    if(!Object.values(this.selected||{}).some(Boolean))throw Error("Select at least one available component");
    this.changed();const epoch=this.epoch,version=this.editor.editVersion;
    this.status.textContent="Resolving mapping and selected media…";
    const result=await request(this.editor,"/imports/preview",{method:"POST",body:JSON.stringify(this.payload()),signal:this.abort.signal});
    if(epoch!==this.epoch||this.abort.signal.aborted)return;
    this.preview=result;this.previewVersion=version;
    this.details.replaceChildren(el("h3",{text:"Resolved import"}),el("p",{text:result.source_ownership}),
      el("h4",{text:this.selected.global?"Global prompt to import":"Global prompt detected · not selected"}),el("pre",{text:result.global}));
    const table=el("table",{},el("thead",{},el("tr",{},...['Chunk','Timeline frames / seconds','Prompt sections'].map(text=>el("th",{text})))));
    const body=el("tbody");
    for(const row of result.mapping.rows)body.append(el("tr",{},el("td",{text:row.chunk}),
      el("td",{text:row.range.join("–")+" / "+row.seconds.join("–")}),
      el("td",{text:this.selected.chunks?(row.parts.map(p=>"["+p.range.join("–")+") "+p.text).join("\n")||"Uncovered · existing prompt retained"):"Chunk prompts not selected · existing prompt retained"})));
    table.append(body);this.details.append(table);
    for(const [title,value] of [["Selected media",result.media],["Uncovered chunks",result.mapping.uncovered],["Gaps",result.mapping.gaps],["Surplus entries (outside current scene)",result.mapping.surplus],["Timing / import notes",result.notes]])
      this.details.append(el("details",{open:""},el("summary",{text:title+" · "+value.length}),el("pre",{text:JSON.stringify(value,null,2)})));
    this.apply.disabled=version!==this.editor.editVersion;
    this.status.textContent="Preview ready · Apply import confirms these changes.";
  }
  close(){
    if(this.abort.signal.aborted)return;
    this.abort.abort();this.epoch++;
    this.editor.abort.signal.removeEventListener("abort",this.parentClose);
    this.root.close();this.root.remove();this.discardSource();
    if(this.editor.importDialog===this)this.editor.importDialog=null;
  }
}

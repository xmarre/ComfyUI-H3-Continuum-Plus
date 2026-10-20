// Read-only State Manager adapter. Presets are read through the existing GET
// endpoint; imports never invoke Save/Load, widget callbacks or queue hooks.
const MANAGERS=new Set(["State Manager","DoRA State Manager"]);
const TEXT_BOXES=new Set(["State Manager Text Box","State Text Box"]);
const kind=n=>String(n?.comfyClass||n?.type||"");
const widget=(node,name)=>node?.widgets?.find(w=>w.name===name)?.value;
const link=(graph,id)=>graph?.links?.[id]??graph?.links?.get?.(id);
const node=(graph,id)=>graph?.getNodeById?.(id)||graph?._nodes?.find(n=>n.id===id);

export function promptSourceNodes(graph,sampler) {
  let edge=link(graph,sampler?.inputs?.find(i=>i.name==="sequence_prompt")?.link);
  const visited=new Set();
  let textBox=null,directManager=null;
  for(let depth=0;edge&&depth<48;depth++) {
    const current=node(graph,edge.origin_id);
    if(!current||visited.has(current.id))break;
    visited.add(current.id);
    if(MANAGERS.has(kind(current))){directManager=current;break;}
    if(TEXT_BOXES.has(kind(current))){textBox=current;break;}
    const incoming=(current.inputs||[]).filter(i=>{
      const e=link(graph,i.link);
      if(!e)return false;
      return /^(STRING|TEXT|\*)$/i.test(String(e.type||i.type||"")) &&
        !/state|control|model|clip|seed/i.test(i.name||"");
    });
    if(incoming.length!==1)break;
    edge=link(graph,incoming[0].link);
  }
  let controlled=null;
  if(textBox) {
    const control=textBox.inputs?.find(i=>/^(state_control|state|control)$/.test(i.name));
    const e=link(graph,control?.link);
    const candidate=e&&node(graph,e.origin_id);
    if(candidate&&MANAGERS.has(kind(candidate)))controlled=candidate;
  }
  const managers=(graph?._nodes||[]).filter(n=>MANAGERS.has(kind(n)));
  return {textBox,manager:directManager||controlled,managers};
}

export function connectedTextSource(graph,sampler) {
  const found=promptSourceNodes(graph,sampler);
  if(!found.textBox)throw Error("No uniquely connected State Manager Text Box was found. Select a preset source or paste the text.");
  const value=widget(found.textBox,"text");
  if(typeof value!=="string")throw Error("The connected Text Box has no readable text widget");
  return {text:value,provenance:{kind:"state-manager-text-box",text_node:String(found.textBox.id),manager_node:found.manager?String(found.manager.id):null}};
}

export async function selectedPresetSource(api,manager,textBox=null) {
  if(!manager||!MANAGERS.has(kind(manager)))throw Error("Choose a State Manager node");
  const response=await api.fetchApi("/dora_dynamic_lora/state-library",{method:"GET"});
  const library=await response.json();
  if(!response.ok)throw Error(library?.error||"Cannot read State Manager presets");
  const characterId=String(widget(manager,"selected_character_id")||"");
  const promptId=String(widget(manager,"selected_prompt_id")||"");
  const character=library.characters?.find(c=>c.id===characterId);
  const preset=character?.prompts?.find(p=>p.id===promptId);
  if(!preset)throw Error("The selected State Manager preset is unavailable; select a saved preset in State Manager");
  const role=String(widget(textBox,"role")||"positive");
  const slot=String(widget(textBox,"state_slot")||widget(textBox,"slot")||textBox?.properties?.dora_state_text_slot||"default");
  const boxes=preset.text_boxes||[];
  let box=boxes.find(b=>b.role===role&&b.slot===slot);
  if(!box)box=boxes.find(b=>b.role==="positive"&&b.slot==="default");
  if(!box) {
    const candidates=boxes.filter(b=>b.role==="positive");
    if(candidates.length===1)box=candidates[0];
    else if(candidates.length>1)throw Error("Preset has several positive Text Box slots; connect the intended Text Box to the sampler");
  }
  const value=box?.text??preset.positive;
  if(typeof value!=="string")throw Error("The selected preset has no positive prompt text");
  return {text:value,provenance:{kind:"state-manager-preset",manager_node:String(manager.id),
    character_id:characterId,prompt_id:promptId,slot:box?.slot||"default",library_revision:library.revision}};
}

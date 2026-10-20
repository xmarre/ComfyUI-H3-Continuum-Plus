import test from "node:test";
import assert from "node:assert/strict";
import {promptSourceNodes,connectedTextSource,selectedPresetSource} from "../web/timeline/import_state.js";
import {importedRegions,setImportedRegion,migrateChunkPrompts,editablePromptSections,editPromptSection} from "../web/timeline/import_regions.js";
import {History,setFinalAudioSource} from "../web/timeline/core.js";
import {applyImportPreview} from "../web/timeline/import_dialog.js";

function graphFixture(){
  const manager={id:1,comfyClass:"State Manager",widgets:[{name:"selected_character_id",value:"char"},{name:"selected_prompt_id",value:"preset"}]};
  const text={id:2,comfyClass:"State Manager Text Box",widgets:[{name:"text",value:"Local identity\n[0-7s]\nAction"},{name:"role",value:"positive"},{name:"state_slot",value:"actor"}],inputs:[{name:"state_control",link:1}]};
  const middle={id:3,comfyClass:"ImpactWildcardProcessor",inputs:[{name:"text",type:"STRING",link:2}]};
  const sampler={id:4,inputs:[{name:"sequence_prompt",type:"STRING",link:3}]};
  const graph={_nodes:[manager,text,middle,sampler],links:{1:{origin_id:1},2:{origin_id:2,type:"STRING"},3:{origin_id:3,type:"STRING"}}};
  graph.getNodeById=id=>graph._nodes.find(n=>n.id===id);
  return {graph,manager,text,sampler};
}
test("connected State Manager Text Box is read without executing transformations or changing graph",()=>{
  const {graph,manager,text,sampler}=graphFixture();
  const before=JSON.stringify(graph);
  const found=promptSourceNodes(graph,sampler);
  assert.equal(found.textBox,text);assert.equal(found.manager,manager);
  const result=connectedTextSource(graph,sampler);
  assert.equal(result.text,"Local identity\n[0-7s]\nAction");
  assert.equal(result.provenance.kind,"state-manager-text-box");
  assert.equal(JSON.stringify(graph),before);
});
test("selected preset uses only GET and the connected Text Box slot",async()=>{
  const {manager,text}=graphFixture();const calls=[];
  const preset={id:"preset",positive:"Fallback",text_boxes:[{role:"positive",slot:"actor",text:"Saved identity"}]};
  const library={revision:8,characters:[{id:"char",prompts:[preset]}]};const before=JSON.stringify(library);
  const api={async fetchApi(path,options){calls.push([path,options]);return {ok:true,json:async()=>library};}};
  const result=await selectedPresetSource(api,manager,text);
  assert.equal(result.text,"Saved identity");
  assert.equal(result.provenance.slot,"actor");
  assert.deepEqual(calls,[["/dora_dynamic_lora/state-library",{method:"GET"}]]);
  assert.equal(JSON.stringify(library),before);
});
test("preset ambiguity is diagnosed instead of choosing another positive slot",async()=>{
  const {manager}=graphFixture();const api={async fetchApi(){return {ok:true,json:async()=>({characters:[{id:"char",prompts:[{id:"preset",text_boxes:[{role:"positive",slot:"a",text:"A"},{role:"positive",slot:"b",text:"B"}]}]}]})};}};
  await assert.rejects(selectedPresetSource(api,manager),/several positive/);
});
test("editing one imported inner section preserves adjacent source text",()=>{
  const p={prompts:{regions:[{id:"a",start:0,end:336,text:"Original"}]}};
  setImportedRegion(p,120,168,"Edited");
  assert.deepEqual(p.prompts.regions.map(({start,end,text})=>({start,end,text})),[
    {start:0,end:120,text:"Original"},{start:120,end:168,text:"Edited"},{start:168,end:336,text:"Original"}]);
  assert.deepEqual(importedRegions(p,0,168).map(r=>r.text),["Original","Edited"]);
});
for(const target of ["merge","replace"]){
  test(target+" import confirmation is exactly one Undo step",()=>{
    const before={selection:[0,336],prompts:{global:"Old"},tracks:[{id:"keep",clips:[]}]};const history=new History(before);
    const editor={history,editVersion:4,edit(fn){history.commit(fn);},message(){}};
    const after={...before,prompts:{global:"New",regions:[{start:0,end:168,text:"Action"}]}};
    applyImportPreview(editor,{project:after,target,source_ownership:"Studio"},4);
    assert.equal(history.undoStack.length,1);history.undo();assert.deepEqual(history.value,before);
    history.redo();assert.deepEqual(history.value,after);
  });
}

test("legacy typed chunk text and imported partial text share one editable store",()=>{
  const media={id:"media",role:"retained",clips:[{id:"video",start:0,end:336}]};
  const p={tracks:[media,{id:"typed",role:"prompt",chunk_workspace:true,clips:[{id:"a",start:0,end:168,text:"A",chunk_prompt:true,chunk_index:0}]}]};
  migrateChunkPrompts(p);setImportedRegion(p,48,120,"B");
  assert.deepEqual(p.tracks,[media]);
  assert.deepEqual(importedRegions(p,0,168).map(r=>[r.start,r.end,r.text]),[[0,48,"A"],[48,120,"B"],[120,168,"A"]]);
});
test("a saved preset never silently substitutes another connected Text Box slot",async()=>{
  const {manager,text}=graphFixture();
  const api={async fetchApi(){return {ok:true,json:async()=>({characters:[{id:"char",prompts:[{id:"preset",text_boxes:[{role:"positive",slot:"default",text:"Wrong identity"}]}]}]})};}};
  await assert.rejects(selectedPresetSource(api,manager,text),/matching the connected role and slot/);
});

test("uncovered parts of a partially imported chunk can be authored and clearing restores inheritance",()=>{
  const p={prompts:{regions:[{id:"b",start:48,end:120,text:"B"}]}};
  let sections=editablePromptSections(p,0,168);
  assert.deepEqual(sections.map(r=>[r.start,r.end,!!r.inherited]),[[0,48,true],[48,120,false],[120,168,true]]);
  editPromptSection(p,sections[0],"New first action");
  assert.deepEqual(importedRegions(p,0,168).map(r=>r.text),["New first action","B"]);
  sections=editablePromptSections(p,0,168);
  editPromptSection(p,sections[1],"  ");
  assert.deepEqual(importedRegions(p,0,168).map(r=>r.text),["New first action"]);
  assert.equal(editablePromptSections(p,0,168)[1].inherited,true);
});

test("output audio choice is Undoable and preserves guidance and existing takes",()=>{
  const p={tracks:[{role:"retained",clips:[{audio_role:"both"}]}],takes:[{id:"old",final_audio_source:"original"}]};
  const history=new History(p);
  history.commit(next=>setFinalAudioSource(next,"generated"));
  assert.equal(history.value.final_audio_source,"generated");
  assert.deepEqual(history.value.tracks,p.tracks);assert.deepEqual(history.value.takes,p.takes);
  history.undo();assert.deepEqual(history.value,p);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {History} from '../web/timeline/core.js';
import {applySceneCandidate,SmartReferences,planConveyorPublication,screenshotIdsForSelection} from '../web/timeline/smart_references.js';
import {referenceSocketSources} from '../web/timeline/chunk_ui.js';

test('scene Apply uses one History edit and rejects a concurrent timeline edit',()=>{
 const p={assets:[],prompts:{global:'Authored dialogue'},audio_generation_mode:'source_locked'};
 const history=new History(p);
 const editor={history,editVersion:1,closed:false,edit(fn){this.history.commit(fn);this.editVersion++;}};
 const candidate={assets:[{id:'scene'}],scene_plan:{shots:[{id:'shot'}]}};
 applySceneCandidate(editor,candidate,1);
 assert.deepEqual(history.value.prompts,p.prompts);assert.equal(history.value.audio_generation_mode,'source_locked');
 history.undo();assert.equal(history.value.scene_plan,undefined);history.redo();assert.equal(history.value.scene_plan.shots.length,1);
 assert.throws(()=>applySceneCandidate(editor,candidate,1),/timeline changed/);
});

test('scene queue protection is scoped to Studio, not global app queue',()=>{
 const app={queuePrompt:async()=>7},original=app.queuePrompt;
 const editor={smartReferences:{cancelVlm:async()=>{}}};
 assert.equal(app.queuePrompt,original);
 assert.equal(typeof editor.smartReferences.cancelVlm,'function');
});

test('runtime budgeting may change without relabelling already extracted pixels',()=>{
 const cfg={h3_mp:.7,vlm_mp:.25,scene_budget:2};
 const workspace={cfg:{...cfg,scene_budget:3},candidate:{scene_plan:{settings:cfg}}};
 SmartReferences.prototype.updatePlanSettings.call(workspace);assert.equal(workspace.candidate.scene_plan.settings.scene_budget,3);
 workspace.cfg.h3_mp=.1;assert.throws(()=>SmartReferences.prototype.updatePlanSettings.call(workspace),/Reanalyze/);
});


function conveyorGraph({hops=1,ambiguous=false,occupied=[]}={}) {
  const id='a'.repeat(64),ref=id+'.jpg';
  const state={version:2,output_mode:'persistent_refs',
    active_reference_preset_id:'character-portrait',
    reference_slots:Array.from({length:8},(_,i)=>
      occupied.includes(i+1)?{annotated:'characters/identity'+(i+1)+'.png [input]',
      filename:'identity'+(i+1)+'.png',type:'input',subfolder:'characters'}:null)};
  const nodes=[];
  const conveyor={id:11,comfyClass:'ImageConveyor',
    outputs:Array.from({length:14},(_,i)=>({
      name:i>=6&&i<=13?'ref_image_'+(i-5):'other',type:i>=6?'IMAGE':'*'
    })),widgets:[{name:'state_json',value:JSON.stringify(state)}],properties:{}};
  nodes.push(conveyor);
  const links={5:{origin_id:3,origin_slot:0}};
  let prev=conveyor,output=10;
  for(let i=0;i<hops;i++) {
    const linkId=100+i*2,otherId=linkId+1;
    const processor={id:22+i,comfyClass:'MediaResolution',
      title:'Media Resolution '+i,
      inputs:[{name:'image',type:'IMAGE',link:linkId}],
      outputs:[{name:'processed',type:'IMAGE'}]};
    links[linkId]={origin_id:prev.id,origin_slot:output,type:'IMAGE'};
    if(ambiguous && i===hops-1) {
      const extra={id:80,comfyClass:'OtherImageSource',outputs:[{name:'IMAGE',type:'IMAGE'}]};
      nodes.push(extra);
      processor.inputs.push({name:'other_image',type:'IMAGE',link:otherId});
      links[otherId]={origin_id:extra.id,origin_slot:0,type:'IMAGE'};
    }
    nodes.push(processor);prev=processor;output=0;
  }
  links[500]={origin_id:prev.id,origin_slot:output,type:'IMAGE'};
  const sampler={id:9,comfyClass:'H3ContinuumTimelineSampler',
    inputs:[{name:'timeline',link:5},{name:'reference_image_2',link:500}]};
  nodes.push(sampler);
  const graph={_nodes:nodes,links,getNodeById(id){return nodes.find(n=>n.id===id)}};
  for(const node of nodes)node.graph=graph;
  const editor={node:{id:3},app:{graph},api:{apiURL:x=>x},layout:{thumb:100}};
  return {editor,state,conveyor,id,ref};
}

test('Media Resolution lineage preserves shelf ordinal versus H3 socket number',()=>{
  const {editor,conveyor,state,id}=conveyorGraph();
  const source=referenceSocketSources(editor);
  assert.equal(source[1].connected,true);
  assert.equal(source[1].conveyor,true);
  assert.equal(source[1].shelfNumber,5);
  assert.equal(source[1].slot,2);
  assert.equal(source[1].nodeId,conveyor.id);
  assert.equal(source[1].reason,'Shelf empty');
  const plan=planConveyorPublication([id],state,source,conveyor.id);
  assert.equal(plan.routed.length,1);
  assert.equal(plan.assignments[0].shelfNumber,5);
  assert.equal(plan.assignments[0].physicalSlot,2);
  assert.equal(plan.staged.length,0);
});

test('a deep but uniquely traced Media Resolution route is detected via provenance fallback',()=>{
  const {editor,conveyor,state,id}=conveyorGraph({hops:26});
  const source=referenceSocketSources(editor);
  assert.equal(source[1].conveyor,true);
  assert.equal(source[1].shelfNumber,5);
  assert.equal(source[1].provenanceFallback,true);
  const assigned=planConveyorPublication([id],state,source,conveyor.id);
  assert.equal(assigned.routed[0].physicalSlot,2);
});

test('multi-image intermediaries never claim a unique shelf-to-sampler mapping',()=>{
  const {editor,conveyor,state,id}=conveyorGraph({ambiguous:true});
  const source=referenceSocketSources(editor);
  assert.equal(source[1].conveyor,false);
  assert.equal(source[1].ambiguousPath,true);
  assert.equal(source[1].candidatePreviews.length,1);
  const plan=planConveyorPublication([id],state,source,conveyor.id);
  assert.equal(plan.routed.length,0);
  assert.equal(plan.staged.length,1);
});

test('occupied connected identity shelf remains untouched; JPGs stage into spare slots',()=>{
  const {editor,conveyor,state}=conveyorGraph({occupied:[5]});
  const sources=referenceSocketSources(editor);
  const screenshotIds=['a'.repeat(64),'b'.repeat(64)];
  const before=JSON.stringify(state);
  const staged=planConveyorPublication(screenshotIds,state,sources,conveyor.id);
  assert.deepEqual(staged.assignments.map(x=>x.shelfNumber),[1,2]);
  assert.deepEqual(staged.assignments.map(x=>x.physicalSlot),[null,null]);
  assert.equal(JSON.stringify(state),before); // planning never mutates the graph
  assert.equal(state.reference_slots[4].filename,'identity5.png');
});

test('existing imported screenshot is reused rather than duplicated or overwritten',()=>{
  const {editor,conveyor,state,id}=conveyorGraph();
  state.reference_slots[4]={annotated:'h3_timeline_screencaps/'+id+'.jpg [input]',
    filename:id+'.jpg',subfolder:'h3_timeline_screencaps',type:'input'};
  conveyor.widgets[0].value=JSON.stringify(state);
  const source=referenceSocketSources(editor);
  const plan=planConveyorPublication([id],state,source,conveyor.id);
  assert.equal(plan.routed[0].physicalSlot,2);
  assert.equal(plan.routed[0].shelfNumber,5);
  assert.throws(()=>planConveyorPublication(Array(9).fill('a'.repeat(64)),state,source,conveyor.id),
    /1 and 8 distinct/);
});


test('export counts all chosen scene screenshots, not only enabled shot occurrences',()=>{
 const id=n=>n.toString(16).padStart(64,'0');
 const plan={shots:[
  {range:[17400,17467],source_asset_id:'movie',reference_ids:[id(1),id(2)],user:{enabled:true}},
  {range:[17467,17533],source_asset_id:'movie',reference_ids:[id(3)],user:{enabled:false}},
  {range:[17533,17560],source_asset_id:'movie',reference_ids:[id(4)],user:{enabled:true}},
  {range:[17560,17568],source_asset_id:'movie',reference_ids:[id(1)],user:{enabled:true}},
  {range:[17568,17600],source_asset_id:'different',reference_ids:[id(5)],user:{enabled:true}}
 ]};
 const result=screenshotIdsForSelection(plan,[17400,17568],'movie');
 assert.equal(result.shots.length,4);
 assert.deepEqual(result.ids,[id(1),id(2),id(3),id(4)]);
 assert.deepEqual(result.enabledIds,[id(1),id(2),id(4)]);
 // Four distinct source JPEGs; three appear in active cues.
 const subset=screenshotIdsForSelection(plan,[17400,17467],'movie');
 assert.deepEqual(subset.ids,[id(1),id(2)]);
});

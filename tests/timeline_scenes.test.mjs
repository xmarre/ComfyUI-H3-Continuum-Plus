import test from 'node:test';
import assert from 'node:assert/strict';
import {History} from '../web/timeline/core.js';
import {applySceneCandidate,SmartReferences,planConveyorPublication,planChunkReferenceStream,screenshotIdsForSelection,renderShotCueReceipt} from '../web/timeline/smart_references.js';
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

test('cue receipt displays exact shot intervals even before Conveyor routing',()=>{
  const p={scene_plan:{conveyor_only:true,shots:[
    {id:'a',range:[17400,17467],reference_ids:['x'],user:{enabled:true}},
    {id:'b',range:[17467,17553],reference_ids:['y'],user:{enabled:true}},
    {id:'c',range:[17553,17568],reference_ids:['z'],user:{enabled:false}}
  ]}};
  const windows=[{index:0,range:[17400,17568],scene_shots:[
    {range:[17400,17467],text:'Match <Picture 4> for camera framing and staging.'}
  ]}];
  const text=renderShotCueReceipt({windows,prompt_source:'connected'},p);
  assert.match(text,/Base: connected State Manager prompt/);
  assert.match(text,/3 shot window\(s\) · 1 active Picture cue\(s\) · 1 disabled/);
  assert.match(text,/Chunk 1 \[0-2\.791667s\] Match <Picture 4>/);
  assert.match(text,/Chunk 1 \[2\.791667-6\.375s\] No active H3 <Picture N> alias/);
  assert.match(text,/connect its output through Media Resolution/);
  assert.match(text,/Chunk 1 \[6\.375-7s\] Disabled shot/);
  assert.doesNotMatch(text,/<Picture 5>/);
});

test('cue receipt does not fabricate image aliases when all references are staged',()=>{
  const p={scene_plan:{conveyor_only:true,shots:[{id:'a',range:[0,168],
    reference_ids:['screenshot'],user:{enabled:true}}]}};
  const text=renderShotCueReceipt({
    windows:[{index:0,range:[0,168],scene_shots:[]}],
    prompt_source:'connected-unavailable',
  },p);
  assert.match(text,/UNVERIFIED/);
  assert.match(text,/0 active Picture cue/);
  assert.match(text,/No active H3 <Picture N> alias/);
  assert.doesNotMatch(text,/Match <Picture/);
});

test('completed VLM analysis preview automatically requests compiled shot cues',async()=>{
  let compiled=0,rendered=0;
  const data={project:{scene_plan:{shots:[{id:'a'}]},prompts:{}},
    analysis:{id:'analysis',shots:[{id:'a'}],warnings:[]}};
  const ui={
    job:'job',pendingInterpretation:false,closed:false,version:2,
    editor:{editVersion:2,p:{selection:[0,168]}},
    candidate:{},analysis:null,modelChoices:[],models:{value:'none'},
    api:{fetchApi:async()=>({ok:true,json:async()=>data})},
    renderShots(){rendered++},scheduleCuePreview(){compiled++},
    status:{textContent:''}
  };
  await SmartReferences.prototype.preview.call(ui);
  assert.equal(rendered,1);
  assert.equal(compiled,1);
  assert.equal(ui.candidate,data.project);
  assert.match(ui.status.textContent,/mapped shots/);
});

test('smart-reference form source select and help text cannot overlap controls',async()=>{
  const {readFileSync}=await import('node:fs');
  const css=readFileSync(new URL('../web/timeline/studio.css',import.meta.url),'utf8');
  assert.match(css,/\.h3ts-smart-controls > \.h3ts-smart-source \{ grid-column:span 2;/);
  assert.match(css,/\.h3ts-smart-controls select,\.h3ts-smart-controls input \{ box-sizing:border-box;min-width:0;max-width:100%/);
  assert.match(css,/\.h3ts-smart-controls > \.h3ts-smart-resolution-note \{ grid-column:1\/-1;/);
  assert.match(css,/\.h3ts-smart-controls \.h3ts-smart-actions \{ grid-column:1\/-1;/);
});

test('dynamic stream uses 3 existing identity references plus only each chunk scene shots',()=>{
 const picture=n=>n.toString(16).padStart(64,'0');
 const p={duration_frames:336,chunk_origin_frame:0,chunk_frames:168,
   scene_plan:{shots:[
     {range:[0,67],reference_ids:[picture(1)]},
     {range:[67,153],reference_ids:[picture(2)]},
     {range:[153,168],reference_ids:[picture(3)]},
     {range:[168,336],reference_ids:[picture(4)]},
   ]}};
 const sources=[1,2,3].map(slot=>({slot,connected:true,unavailable:false,dynamic:false,label:'Identity'}));
 const result=planChunkReferenceStream(p,sources);
 assert.deepEqual(result.sockets,[1,2,3]);
 assert.equal(result.bank.length,4);
 assert.deepEqual(result.stats.map(x=>x.sourceImageCount),[3,1]);
 assert.deepEqual(result.stats.map(x=>x.identityEstimate+x.accepted),[6,4]);
 assert.deepEqual(result.perChunk,{'0':[1,2,3],'1':[1,2,3]});
 assert.deepEqual(result.stats.map(x=>x.omitted),[0,0]);
});

test('long scene stream reuses fixed 5 scene capacity over 100 chunks',()=>{
 const p={duration_frames:168*100,chunk_frames:168,chunk_origin_frame:0,
   scene_plan:{shots:Array.from({length:100},(_,i)=>({
     range:[168*i,168*(i+1)],reference_ids:[i.toString(16).padStart(64,'0')]
   }))}};
 const sources=[1,2,3].map(slot=>({slot,connected:true,unavailable:false,dynamic:false,label:'Identity'}));
 const x=planChunkReferenceStream(p,sources);
 assert.equal(x.bank.length,100);
 assert.equal(x.stats.length,100);
 assert(x.stats.every(s=>s.accepted===1&&s.omitted===0));
 assert(Object.values(x.perChunk).every(mask=>mask.join(',')==='1,2,3'));
});

test('streamed per-chunk planner enforces all eight H3 picture positions',()=>{
 const p={duration_frames:168,chunk_frames:168,
   scene_plan:{settings:{scene_budget:5},shots:[{range:[0,168],reference_ids:Array.from({length:8},(_,i)=>String(i).padStart(64,'0'))}]}};
 const sources=[1,2,3,4].map(slot=>({slot,connected:true,unavailable:false,dynamic:false,label:'Identity'}));
 const plan=planChunkReferenceStream(p,sources);
 assert.equal(plan.stats[0].accepted,4);
 assert.equal(plan.stats[0].omitted,4);
 const all=Array.from({length:8},(_,i)=>({slot:i+1,connected:true,unavailable:false,dynamic:false,label:'Identity'}));
 assert.throws(()=>planChunkReferenceStream(p,all),/All eight H3 Picture positions/);
});

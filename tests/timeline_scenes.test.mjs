import test from 'node:test';
import assert from 'node:assert/strict';
import {History} from '../web/timeline/core.js';
import {applySceneCandidate,cancelSceneVlm,setShotDescription,SmartReferences} from '../web/timeline/smart_references.js';

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

test('Studio queue handoff waits for its workers without modifying app or API queue methods',async()=>{
 const order=[];const queue=async()=>{order.push('sample');return 7;};
 const editor={app:{queuePrompt:queue},api:{queuePrompt:queue},sceneVlmClients:new Set()};
 let finish;editor.sceneVlmClients.add({cancelVlm:()=>new Promise(resolve=>{order.push('cancel');finish=()=>{order.push('provider-stopped');resolve();};})});
 const pending=(async()=>{await cancelSceneVlm(editor);return editor.api.queuePrompt();})();
 await Promise.resolve();assert.deepEqual(order,['cancel']);finish();
 assert.equal(await pending,7);assert.deepEqual(order,['cancel','provider-stopped','sample']);
 assert.equal(editor.app.queuePrompt,queue);assert.equal(editor.api.queuePrompt,queue);
});

test('editing model text preserves model provenance and the original description',()=>{
 const shot={vlm:{description:'Model description.',model_id:'vision',cache_key:'key'}};
 setShotDescription(shot,'Model description!');setShotDescription(shot,'A second edit.');
 assert.deepEqual(shot.user.description_provenance,{kind:'edited_model',model_id:'vision',cache_key:'key',original_description:'Model description.'});
 const authored={user:{description:'Authored.'},vlm:shot.vlm};setShotDescription(authored,'Edited authored.');
 assert.equal(authored.user.description_provenance.kind,'authored');
});

test('runtime budgeting may change without relabelling already extracted pixels',()=>{
 const cfg={h3_mp:.7,vlm_mp:.25,scene_budget:2};
 const workspace={cfg:{...cfg,scene_budget:3},candidate:{scene_plan:{settings:cfg}}};
 SmartReferences.prototype.updatePlanSettings.call(workspace);assert.equal(workspace.candidate.scene_plan.settings.scene_budget,3);
 workspace.cfg.h3_mp=.1;assert.throws(()=>SmartReferences.prototype.updatePlanSettings.call(workspace),/Reanalyze/);
});

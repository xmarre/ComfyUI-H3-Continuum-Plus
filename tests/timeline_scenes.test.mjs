import test from 'node:test';
import assert from 'node:assert/strict';
import {History} from '../web/timeline/core.js';
import {applySceneCandidate,installSceneQueueHandoff,SmartReferences} from '../web/timeline/smart_references.js';

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

test('normal ComfyUI queue waits for the owned VLM request to finish cancellation',async()=>{
 const order=[];const app={queuePrompt:async()=>{order.push('sample');return 7;}};
 installSceneQueueHandoff(app);const state=app[Symbol.for('h3.timeline.sceneVlmQueue')];
 let finish;state.clients.add({cancelVlm:()=>new Promise(resolve=>{order.push('cancel');finish=()=>{order.push('provider-stopped');resolve();};})});
 const pending=app.queuePrompt();await Promise.resolve();assert.deepEqual(order,['cancel']);finish();
 assert.equal(await pending,7);assert.deepEqual(order,['cancel','provider-stopped','sample']);
});

test('runtime budgeting may change without relabelling already extracted pixels',()=>{
 const cfg={h3_mp:.7,vlm_mp:.25,scene_budget:2};
 const workspace={cfg:{...cfg,scene_budget:3},candidate:{scene_plan:{settings:cfg}}};
 SmartReferences.prototype.updatePlanSettings.call(workspace);assert.equal(workspace.candidate.scene_plan.settings.scene_budget,3);
 workspace.cfg.h3_mp=.1;assert.throws(()=>SmartReferences.prototype.updatePlanSettings.call(workspace),/Reanalyze/);
});

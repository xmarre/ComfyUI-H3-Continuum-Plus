// Runs against the real production backend and editor served by scene_test_server.
export async function runSceneSmoke(editor) {
  const check=(condition,message)=>{if(!condition)throw Error(message);};
  const wait=async predicate=>{
    for(let i=0;i<600;i++){if(predicate())return;await new Promise(r=>setTimeout(r,100));}
    throw Error('Scene workspace acceptance timed out');
  };
  const before=JSON.parse(editor.widget.value);
  const opener=[...editor.root.querySelectorAll('button')].find(n=>n.textContent==='Smart references');
  check(opener,'Smart references action missing');opener.click();
  const workspace=editor.smartReferences;
  await workspace.analyze('selection');
  await wait(()=>workspace.candidate.scene_plan||workspace.status.textContent.includes('failed'));
  check(workspace.candidate.scene_plan,'Analysis failed: '+workspace.status.textContent);
  const shots=workspace.candidate.scene_plan.shots;
  check(JSON.stringify(shots.map(s=>s.range))==='[[0,67],[67,153],[153,168]]','Incorrect production source cut mapping');
  check(shots[0].reference_ids[0]===shots[2].reference_ids[0],'Return setup was not reused');
  check(workspace.root.querySelectorAll('article').length===3,'Shot cards not rendered');
  await workspace.compile();
  check(workspace.compiled.textContent.includes('<Picture 1>'),'Physical scene aliases missing');
  check(workspace.compiled.textContent.includes('S2 <d>[English] We both do.</d>'),'Dialogue changed during compilation');
  check(!editor.p.scene_plan,'Analysis mutated active project');
  workspace.candidate.scene_plan.shots[0].user.description='Keep the measured camera setup.';
  workspace.apply();
  check(editor.p.scene_plan.shots.length===3,'Apply did not reach project');
  check(editor.p.audio_generation_mode===before.audio_generation_mode&&editor.p.final_audio_source===before.final_audio_source&&editor.p.video_guidance_enabled===before.video_guidance_enabled,'Media routing changed');
  check(JSON.stringify(editor.p.prompts)===JSON.stringify(before.prompts),'Authored prompts changed');
  check(window.queueCount===0,'Analysis queued sampling');
  editor.undo();check(!editor.p.scene_plan,'Shot plan was not one undoable edit');
  editor.redo();check(editor.p.scene_plan.shots.length===3,'Redo lost the shot plan');
  workspace.version=editor.editVersion;
  editor.edit(p=>{p.name='Concurrent edit';});
  let rejected=false;try{workspace.apply();}catch{rejected=true;}
  check(rejected&&editor.p.name==='Concurrent edit','Stale candidate overwrote a newer edit');
  check(workspace.provider.textContent.includes('unavailable'),'Missing Prompt Writer fallback was not displayed');
  window.sceneDone({shots:shots.map(s=>s.range),references:Object.keys(workspace.candidate.scene_plan.references).length,queueCount:window.queueCount});
  return window.h3SceneSmoke;
}

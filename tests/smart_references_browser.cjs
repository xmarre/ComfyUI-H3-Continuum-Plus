const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const path=require('node:path');
const fs=require('node:fs/promises');
const {chromium}=require(process.env.H3_PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const child=spawn(process.env.PYTHON||'python',[path.join(__dirname,'scene_test_server.py')],{stdio:['ignore','pipe','pipe']});
 let errors='';child.stderr.on('data',x=>errors+=x);
 let timer;
 const ready=new Promise((resolve,reject)=>{
  let data='';child.stdout.on('data',x=>{data+=x;if(data.includes('\n'))try{resolve(JSON.parse(data.split('\n')[0]).url);}catch(error){reject(error);}});
  child.on('exit',code=>reject(Error('Production media host exited '+code+' '+errors)));
  timer=setTimeout(()=>reject(Error('Production media host did not start '+errors)),30000);
 });
 let browser;
 try {
  const url=await ready;clearTimeout(timer);
  browser=await chromium.launch({executablePath:process.env.H3_TIMELINE_CHROMIUM||undefined,headless:true,args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});const pageErrors=[];page.on('pageerror',e=>pageErrors.push(e.message));
  await page.goto(url);await page.waitForFunction(()=>window.sceneReady);
  const result=await page.evaluate(()=>window.h3RunSceneSmoke());
  assert.deepEqual(result.shots,[[0,67],[67,153],[153,168]]);assert.equal(result.references,2);assert.equal(result.queueCount,0);
  await page.getByLabel('VLM image MP ceiling').fill('0.1');
  await page.getByLabel('VLM image MP ceiling').press('Tab');
  const refused=await page.evaluate(()=>{try{editor.smartReferences.updatePlanSettings();return false;}catch{return true;}});
  assert.equal(refused,true);
  assert.deepEqual(pageErrors,[]);
  if(process.env.H3_TIMELINE_SCREENSHOTS){await fs.mkdir(process.env.H3_TIMELINE_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.H3_TIMELINE_SCREENSHOTS,'smart-references.png')});}
  console.log('Production backend/Chromium scene analysis, exact PTS, reuse, aliases, Apply/Undo/Redo, stale edit and missing Writer fallback passed.');
 }finally{clearTimeout(timer);if(browser)await browser.close();child.kill();}
})().catch(error=>{console.error(error);process.exitCode=1;});

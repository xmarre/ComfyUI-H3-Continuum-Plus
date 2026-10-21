// Real Chromium/DOM acceptance of the editor with mocked HTTP responses.
// This deliberately does not claim ComfyUI/GPU/runtime acceptance.
const assert=require('node:assert/strict');
const http=require('node:http');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require(process.env.H3_PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const project={schema:'h3_continuum_timeline_project_v1',project_id:'layout-fixture',name:'Dialogue scene',
  fps:'24/1',duration_frames:159090,chunk_frames:168,chunk_origin_frame:17400,width:768,height:512,
  operation:'generate',final_audio_source:'original',audio_generation_mode:'source_locked',video_guidance_enabled:false,
  assets:[{id:'source',name:'Original movie.mkv',kind:'video',storage:'linked',duration:'6628.75',video:{width:1920,height:1080,start_time:'0'},audio:{sample_rate:'48000'}}],
  tracks:[{id:'movie',role:'retained',name:'Original footage',clips:[{id:'clip',asset_id:'source',start:0,end:159090,source_in:'0',speed:'1',audio_role:'both'}]}],
  prompts:{global:'Preserve character identity and the room. Maekar replaces Clark. Mary keeps her original appearance.',mode:'Fixed',script:''},
  prompt_source:{mode:'studio'},selection:[17400,19080],markers:[],takes:[]};
const image='<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540"><rect width="960" height="540" fill="#263f45"/><rect x="50" y="50" width="240" height="230" fill="#79a29b"/><rect x="90" y="330" width="790" height="170" rx="40" fill="#71594c"/><circle cx="480" cy="225" r="65" fill="#c49b79"/><path d="M375 460V320Q480 265 585 320V460" fill="#746d8a"/><text x="40" y="520" fill="white" font-size="20">Preview fixture • original frame</text></svg>';
function graphFixture(node) {
  const conveyor={id:8,title:'Image Conveyor',comfyClass:'ImageConveyor',outputs:Array.from({length:8},(_,i)=>({name:'ref_image_'+(i+1),type:'IMAGE'})),widgets:[{name:'state_json',value:JSON.stringify({output_mode:'persistent_refs',reference_slots:Array.from({length:8},(_,i)=>({annotated:'refs/portrait'+i+'.png [input]',type:'input',filename:'portrait'+i+'.png',enabled:true}))})}]};
  const sampler={id:2,comfyClass:'H3ContinuumTimelineSampler',inputs:[{name:'timeline',link:1},...Array.from({length:8},(_,i)=>({name:'reference_image_'+(i+1),link:i+2}))],widgets:[{name:'steps',value:10},{name:'seed',value:42}]};
  const graph={_nodes:[node,sampler,conveyor],links:{1:{origin_id:1,target_id:2}},getNodeById(id){return this._nodes.find(n=>n.id===id)}};
  for(let i=0;i<8;i++)graph.links[i+2]={origin_id:8,origin_slot:i,target_id:2};
  node.graph=sampler.graph=conveyor.graph=graph;return graph;
}
(async()=>{
 const server=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://localhost');
  if(url.pathname==='/'){
   res.setHeader('Content-Type','text/html');res.end(`<!doctype html><link rel="stylesheet" href="/web/timeline/studio.css"><script type="module">
   import {TimelineEditor} from '/web/timeline/editor.js';
   const node={id:1,properties:{h3_timeline_view:{layout:{preview:110,chunk:150},playhead:17400}},setDirtyCanvas(){}};
   const widget={value:JSON.stringify(${JSON.stringify(project)})};
   const graph=(${graphFixture.toString()})(node);
   const api={apiURL:p=>p,fetchApi:(p,o)=>fetch(p,o)};
   window.editor=new TimelineEditor({node,widget,api,app:{graph}});
   window.initialProject=widget.value;
   </script>`);return;
  }
  if(url.pathname.includes('/thumbnail/')||url.pathname==='/image-conveyor/thumbnail'){
   res.setHeader('Content-Type','image/svg+xml');res.end(image);return;
  }
  if(url.pathname.startsWith('/h3_continuum/timeline/')){
   res.setHeader('Content-Type','application/json');
   if(url.pathname.endsWith('/compile'))res.end(JSON.stringify({jobs:[],capabilities:{generation_hash:'fixture'}}));
   else if(url.pathname.includes('/waveform/'))res.end(JSON.stringify({peaks:[],duration:6628.75}));
   else res.end('{}');return;
  }
  try {
   const file=path.resolve(root,'.'+decodeURIComponent(url.pathname));
   if(!file.startsWith(root+path.sep))throw Error('Invalid path');
   res.setHeader('Content-Type',file.endsWith('.css')?'text/css':'text/javascript');res.end(await fs.readFile(file));
  }catch{res.statusCode=404;res.end();}
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({executablePath:process.env.H3_TIMELINE_CHROMIUM||undefined,headless:true,args:['--no-sandbox','--disable-gpu']});
 const errors=[];
 try {
  const page=await browser.newPage({viewport:{width:1920,height:1200},acceptDownloads:true});
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.waitForFunction(()=>window.editor && document.querySelector('.h3ts-image').naturalWidth>0);
  const dimensions=()=>page.evaluate(()=>Object.fromEntries(['.h3ts','.h3ts > main','.h3ts-viewer','.h3ts-chunk-workspace','.h3ts-chunk-cards','.h3ts-scroll'].map(selector=>{const r=document.querySelector(selector).getBoundingClientRect();return [selector,{width:r.width,height:r.height}]})));
  for(const [width,height] of [[1920,1200],[1366,768],[1000,700]]) {
   await page.setViewportSize({width,height});
   await page.getByRole('button',{name:'Reset layout',exact:true}).click();
   await page.waitForTimeout(100);
   const d=await dimensions();
   assert.ok(d['.h3ts-chunk-cards'].height>=270,JSON.stringify({width,d}));
   assert.ok(d['.h3ts-viewer'].width>=270,JSON.stringify({width,d}));
   assert.ok(d['.h3ts-chunk-workspace'].width>=320,JSON.stringify({width,d}));
   assert.ok(d['.h3ts-scroll'].height>=100,JSON.stringify({width,d}));
   assert.equal(await page.locator('.h3ts-chunk-card').count(),1);
   assert.ok(await page.evaluate(()=>document.querySelector('.h3ts-chunk-cards').scrollWidth<=document.querySelector('.h3ts-chunk-cards').clientWidth));
   if(process.env.H3_TIMELINE_SCREENSHOTS){await fs.mkdir(process.env.H3_TIMELINE_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.H3_TIMELINE_SCREENSHOTS,`workspace-${width}.png`)});}
  }
  // Reclaim timeline height without losing the project or navigation state.
  const withTimeline=(await page.locator('.h3ts-chunk-cards').boundingBox()).height;
  await page.getByRole('button',{name:'Timeline',exact:true}).click();
  assert.ok((await page.locator('.h3ts-chunk-cards').boundingBox()).height>withTimeline+140);
  assert.equal(await page.locator('.h3ts-scroll').isVisible(),false);
  await page.getByRole('button',{name:'Timeline',exact:true}).click();
  // Focus/resize/settings are view-only, including migration from legacy sizes.
  await page.getByLabel('Workspace focus').selectOption('chunks');
  assert.equal(await page.locator('.h3ts-viewer').isVisible(),false);
  assert.ok((await page.locator('.h3ts-chunk-workspace').boundingBox()).width>900);
  await page.getByLabel('Workspace focus').selectOption('both');
  await page.getByRole('button',{name:'Global prompt',exact:true}).click();
  await page.getByRole('tab',{name:'Media',exact:true}).click();
  await page.getByRole('tab',{name:'Inspect',exact:true}).click();
  await page.getByRole('tab',{name:'Scene',exact:true}).click();
  await page.getByRole('separator',{name:'Resize media library width'}).focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.evaluate(()=>editor.widget.value===initialProject),true);
  // A chunk page is one real logical chunk; changing pages must not edit inputs.
  await page.getByRole('button',{name:'Next chunk',exact:true}).click();
  assert.equal(await page.locator('.h3ts-chunk-card').getAttribute('data-chunk-index'),'1');
  assert.equal(await page.evaluate(()=>editor.widget.value===initialProject),true);
  await page.getByLabel('Go to scene chunk').fill('10');
  await page.getByLabel('Go to scene chunk').press('Tab');
  assert.equal(await page.locator('.h3ts-chunk-card').getAttribute('data-chunk-index'),'9');
  const prompt=page.getByLabel('Prompt for chunk 10',{exact:true});
  await prompt.fill('Mary speaks calmly.');
  assert.equal(await prompt.inputValue(),'Mary speaks calmly.');
  assert.equal(await page.evaluate(()=>document.activeElement===document.querySelector('[aria-label="Prompt for chunk 10"]')),true);
  assert.ok(await page.evaluate(()=>editor.widget.value.includes('Mary speaks calmly.')));
  // Reference and media controls remain reachable in the chunk's own scroller.
  const media=page.locator('.h3ts-chunk-media-details > summary');
  await media.click();await media.scrollIntoViewIfNeeded();
  assert.ok(await media.isVisible());
  const frameResult=page.waitForEvent('download');
  await page.getByRole('button',{name:'Save frame',exact:true}).click();
  const frameDownload=await frameResult;
  assert.match(frameDownload.suggestedFilename(),/continuum-frame-.*\.png/);
  const buffer=await fs.readFile(await frameDownload.path());
  assert.equal(buffer.subarray(1,4).toString(),'PNG');
  // Native screenshot is scoped to the visible app; no server call or project change.
  await page.evaluate(()=>{window.h3NativeMedia={captureEditor:async()=>{window.captureCalls=(window.captureCalls||0)+1;const c=document.createElement('canvas');c.width=c.height=10;return c.toDataURL('image/png')}}});
  const screenshot=page.waitForEvent('download');
  await page.getByRole('button',{name:'Screenshot',exact:true}).click();
  assert.equal((await screenshot).suggestedFilename(),'continuum-editor.png');
  assert.equal(await page.evaluate(()=>captureCalls),1);
  // The browser chooser path stops its stream immediately after capture.
  await page.evaluate(()=>{
    delete window.h3NativeMedia;
    const canvas=document.createElement('canvas');canvas.width=200;canvas.height=100;
    navigator.mediaDevices.getDisplayMedia=async()=>{
      const stream=canvas.captureStream(24);
      for(const track of stream.getTracks()) {
        const stop=track.stop.bind(track);
        track.stop=()=>{window.stoppedCaptureTracks=(window.stoppedCaptureTracks||0)+1;stop()};
      }
      setTimeout(()=>canvas.getContext('2d').fillRect(0,0,100,100),50);
      return stream;
    };
  });
  const browserCapture=page.waitForEvent('download');
  await page.getByRole('button',{name:'Screenshot',exact:true}).click();
  assert.equal((await browserCapture).suggestedFilename(),'continuum-editor.png');
  assert.equal(await page.evaluate(()=>stoppedCaptureTracks),1);
  assert.deepEqual(errors,[]);
  console.log('Workspace browser acceptance passed: 3 viewport sizes, focus, tabs, splitters, chunk navigation/editing, media scrolling and PNG capture.');
 }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1});

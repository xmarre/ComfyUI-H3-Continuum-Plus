"use strict";
// Execute the packaged app against production Python routes and the actual
// served frontend. Only ComfyUI's graph and the native path-link reply are
// stand-ins; shot analysis/extraction/compiler/History use production code.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const http=require('node:http');
const os=require('node:os');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {toWSLPath}=require('./native_link.cjs');
async function main(){
 if(process.platform!=='win32')throw Error('Packaged desktop smoke requires Windows');
 const dist=path.join(__dirname,'dist');
 const filename=fs.readdirSync(dist).find(name=>/^Continuum-Timeline-Desktop-.*\.exe$/i.test(name));
 if(!filename)throw Error('Windows portable executable not found');
 const temp=fs.mkdtempSync(path.join(os.tmpdir(),'h3-native-scene-'));
 const file=path.join(temp,'native-path-smoke.mkv');fs.writeFileSync(file,'Native file bridge identity fixture');
 const expectedPath=toWSLPath(file);let linked=0;let timer;let appChild;let proxy;
 const mediaHost=spawn(process.env.PYTHON||'python',[path.resolve(__dirname,'../../tests/scene_test_server.py')],{stdio:['ignore','pipe','pipe'],windowsHide:true});
 const output=[];mediaHost.stderr.on('data',x=>output.push(String(x)));
 try{
  const host=await new Promise((resolve,reject)=>{
   let data='';mediaHost.stdout.on('data',x=>{data+=x;if(data.includes('\n'))try{resolve(JSON.parse(data.split('\n')[0]).url);}catch(error){reject(error);}});
   mediaHost.on('error',reject);mediaHost.on('exit',code=>reject(Error('Production media host exited '+code+' '+output.join(''))));
   timer=setTimeout(()=>reject(Error('Production media host timed out '+output.join(''))),30000);
  });clearTimeout(timer);
  proxy=http.createServer((req,res)=>{
   if(req.url==='/h3_continuum/timeline/link'){
    const chunks=[];req.on('data',data=>chunks.push(data));req.on('end',()=>{
     assert.deepEqual(JSON.parse(Buffer.concat(chunks).toString()),{path:expectedPath});linked++;
     res.writeHead(200,{'content-type':'application/json'});res.end(JSON.stringify({id:'h3-smoke-asset',storage:'linked'}));
    });return;
   }
   const forward=http.request(new URL(req.url,host),{method:req.method,headers:req.headers},upstream=>{
    res.writeHead(upstream.statusCode,upstream.headers);upstream.pipe(res);
   });forward.on('error',error=>{res.writeHead(502);res.end(error.message);});req.pipe(forward);
  });await new Promise(resolve=>proxy.listen(0,'127.0.0.1',resolve));
  appChild=spawn(path.join(dist,filename),['--comfy-url=http://127.0.0.1:'+proxy.address().port+'/', '--h3-smoke-file='+file],
   {cwd:__dirname,windowsHide:true,env:{...process.env,H3_TIMELINE_DESKTOP_SMOKE:'1'},stdio:['ignore','pipe','pipe']});
  appChild.stdout.on('data',x=>output.push(String(x)));appChild.stderr.on('data',x=>output.push(String(x)));
  const exit=new Promise((resolve,reject)=>{appChild.on('error',reject);appChild.on('exit',(code,signal)=>resolve({code,signal}));});
  const timeout=new Promise((_resolve,reject)=>{timer=setTimeout(()=>{appChild.kill();reject(Error('Packaged production-scene smoke timed out '+output.join('').slice(-8000)));},120000);});
  const result=await Promise.race([exit,timeout]);
  if(result.code!==0||linked!==1||!output.join('').includes('H3_SCENE_WORKSPACE_SMOKE_PASSED'))
   throw Error('Packaged production-scene/native-file smoke failed '+JSON.stringify({result,linked,output:output.join('').slice(-8000)}));
  console.log('Packaged Windows app: real backend/frontend contract, scene detection/extraction, reference aliases, Apply/Undo/Redo, stale-edit rejection and native File IPC passed.');
 }finally{
  clearTimeout(timer);if(appChild&&appChild.exitCode===null)appChild.kill();mediaHost.kill();
  if(proxy)await new Promise(resolve=>proxy.close(resolve));fs.rmSync(temp,{recursive:true,force:true});
 }
}
main().catch(error=>{console.error(error);process.exitCode=1;});

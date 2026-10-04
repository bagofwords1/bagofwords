// Real Chromium: insecure HTTP, opaque srcdoc messaging, asset policies and UUIDs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
const root=new URL('../../',import.meta.url);
const source=fs.readFileSync(process.env.ARTIFACT_IFRAME_SOURCE || new URL('utils/artifactIframe.ts',root),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const browser=await chromium.launch({headless:true});
try{
 for(const resourceApp of [false,true]){
  const page=await browser.newPage();let remoteImages=0;
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.hostname==='external-image.example.test'){remoteImages++;return route.fulfill({status:200,contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jA1sAAAAASUVORK5CYII=','base64')})}
   if(u.pathname.startsWith('/libs/')){
    const file=new URL('public'+u.pathname,root);
    if(fs.existsSync(file))return route.fulfill({path:file.pathname,headers:{'Access-Control-Allow-Origin':'*'}});
   }
   return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
  });
  await page.goto('http://artifact-review.test/');
  assert.equal(await page.evaluate(()=>isSecureContext),false);
  await page.addScriptTag({content:'window.exports={};'+compiled});
  await page.evaluate(({resourceApp})=>{
   const html=exports.buildArtifactIframeHtml({resourceApp,data:{visualizations:[]},code:`<p id="probe">Opaque artifact</p><script>window.violations=[];document.addEventListener('securitypolicyviolation',e=>window.violations.push(e.effectiveDirective));</script>`});
   const f=document.createElement('iframe');f.sandbox='allow-scripts allow-downloads allow-forms';f.srcdoc=html;document.body.appendChild(f);
  },{resourceApp});
  const frame=page.frames().find(f=>f.parentFrame());
  await frame.waitForFunction(()=>typeof bow!=='undefined' && typeof __setArtifactData==='function');
  assert.match(await frame.evaluate(()=>crypto.randomUUID()),/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  // Use the real shared-page sender, not a mock of postMessage's origin rules.
  const shared=fs.readFileSync(new URL('pages/r/[id]/index.vue',root),'utf8');
  const script=shared.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)[1];
  const ast=ts.createSourceFile('shared.ts',script,ts.ScriptTarget.Latest,true);
  const node=ast.statements.find(s=>ts.isFunctionDeclaration(s)&&s.name?.text==='postToArtifactIframe');
  const sender=ts.transpileModule(node.getText(ast),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
  await page.evaluate(sender=>{
   window.artifactIframeRef={value:document.querySelector('iframe')};window.artifactIframeLoaded={value:true};
   (0,eval)(sender);
   postToArtifactIframe({type:'ARTIFACT_DATA',payload:{visualizations:[],testMarker:'fresh'}});
  },sender);
  await frame.waitForFunction(()=>window.ARTIFACT_DATA?.testMarker==='fresh');
  await page.evaluate(()=>document.querySelector('iframe').contentWindow.postMessage({type:'ARTIFACT_SET_COLOR_MODE',mode:'dark'},'*'));
  await frame.waitForFunction(()=>document.documentElement.classList.contains('dark'));
  // Verify an arbitrary sender cannot override theme (the source must be parent).
  await frame.evaluate(()=>window.dispatchEvent(new MessageEvent('message',{data:{type:'ARTIFACT_SET_COLOR_MODE',mode:'light'},source:window})));
  assert.equal(await frame.evaluate(()=>document.documentElement.classList.contains('dark')),true);
  await frame.evaluate(()=>{const img=document.createElement('img');img.src='https://external-image.example.test/avatar.png';img.id='remote-avatar';document.body.appendChild(img)});
  if(resourceApp){await frame.waitForFunction(()=>window.violations.includes('img-src'));assert.equal(remoteImages,0)}
  else {await frame.waitForFunction(()=>document.querySelector('#remote-avatar').naturalWidth>0);assert.equal(remoteImages,1)}
  await page.close();
 }
 console.log('PASS: plain-HTTP rendering and UUIDs, real shared-page data delivery, parent-only theme updates, legacy images and resource-app image isolation');
}finally{await browser.close()}

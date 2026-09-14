import { chromium, expect } from '@playwright/test';
import { writeFileSync, existsSync, unlinkSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const preview = fileURLToPath(new URL('../../pages/users/agent-count-evidence.vue', import.meta.url));
const out = fileURLToPath(new URL('../../../media/pr/agent-instruction-count/', import.meta.url));
if (existsSync(preview)) throw Error('Preview exists');
mkdirSync(out, { recursive: true });
writeFileSync(preview, `<template><KnowledgeExplorer /></template><script setup lang="ts">definePageMeta({auth:false,layout:false});</script>`);
let browser, page;
try {
 browser = await chromium.launch();
 page = await browser.newPage({ viewport: { width: 1200, height: 850 }, recordVideo: { dir: out + 'video' } });
 const errors = []; page.on('pageerror', e => errors.push(e.message));
 let calls = [], release;let listRequests=0;let countTotal=0;let failCounts=false;let countRequests=0;
 const connections = [
  {id:'pbi',name:'Power BI',type:'powerbi',table_count:5,user_status:{connection:'success',effective_auth:'user',has_user_credentials:true,last_checked_at:'2026-09-01T12:00:00Z'}},
  {id:'fabric',name:'Fabric',type:'ms_fabric',table_count:1234,user_status:{connection:'success',effective_auth:'user',has_user_credentials:true}},
  {id:'monday',name:'Monday',type:'monday',table_count:0,indexing:{status:'completed',stats:{table_count:100,item_noun_plural:'boards'}},user_status:{connection:'offline',effective_auth:'none',has_user_credentials:false}}
 ].map(c=>({...c,auth_policy:'user_required',allowed_user_auth_modes:['oauth'],is_active:true}));
 const agent = {id:'agent-card',name:'pbi',description:'Explore financial reports and business operations.',type:'powerbi',status:'active',publish_status:'published',reliability_status:'training',connections};
 await page.route('**/api/**', async r => {
  const path = new URL(r.request().url()).pathname; if (!path.startsWith('/api/')) return r.continue(); let body=[];
  if(path.endsWith('/data_sources/active')) body=[agent];
  else if(path.endsWith('/data_sources/agent-card')) body=agent;
  else if(path.endsWith('/connections')) body=connections;
  else if(path.endsWith('/oauth/authorize')) { calls.push(path); await new Promise(resolve=>release=resolve); body={}; }
  else if(path.endsWith('/whoami')) body={id:'preview',organizations:[]};
  else if(path.endsWith('/instructions')){listRequests++;body={items:[1,2].map(i=>({id:'instruction-'+i,title:'Rule '+i,status:'published',data_sources:[{id:'agent-card',name:'pbi'}]})),total:2};}
  else if(path.includes('counts')) {countRequests++;if(failCounts)return r.fulfill({status:503,json:{detail:'Temporarily unavailable'}});body={by_agent:{'agent-card':countTotal}};}
  await r.fulfill({json:body});
 });
 async function load() { for(let i=0;i<3;i++) { await page.goto('http://localhost:3100/users/agent-count-evidence'); try {await expect(page.getByRole('button',{name:'Sign in',exact:true})).toHaveCount(1,{timeout:10000});return;}catch(e){if(i===2)throw e;} } }
 await load();await page.getByText('pbi',{exact:true}).click();await expect.poll(()=>listRequests).toBeGreaterThan(0);
 await (process.env.BEFORE==='1' ? page : page.getByTestId('agent-landing')).getByRole('button',{name:'Sign in',exact:true}).click();
 const dialog=page.getByRole('dialog');await expect(dialog.getByText('Power BI',{exact:true})).toBeVisible();await page.waitForTimeout(350);
 if(process.env.BEFORE==='1') {await expect(dialog.getByText('0 instructions',{exact:true})).toBeVisible();await page.screenshot({path:out+'before.png'});console.log('BASELINE: loaded instruction list has 2; card shows 0.');}
 else {
  await expect(dialog.getByText('2 instructions',{exact:true})).toBeVisible();await page.screenshot({path:out+'after.png'});
  await dialog.getByRole('button',{name:'Close',exact:true}).click();await expect(page.getByRole('button',{name:'2 instructions',exact:true})).toBeVisible();
  countTotal=2;await load();const requestsBefore=countRequests;await page.getByRole('button',{name:'Sign in',exact:true}).click();await expect(dialog.getByText('2 instructions',{exact:true})).toBeVisible();expect(countRequests).toBeGreaterThan(requestsBefore);
  failCounts=true;await load();await page.getByRole('button',{name:'Sign in',exact:true}).click();await expect(dialog.getByText('— Instructions',{exact:true})).toBeVisible();await expect(dialog.getByText('0 instructions',{exact:true})).toHaveCount(0);
  expect(errors).toEqual([]);console.log('PASS: loaded-list consistency, fresh count on card open, and unavailable counts never become zero.');
 }
 await page.context().close();
} catch(e) {await page?.screenshot({path:out+'failure.png'});throw e;} finally {await browser?.close();unlinkSync(preview);}

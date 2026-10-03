import {chromium,expect} from '@playwright/test';
import {writeFileSync,existsSync,unlinkSync,mkdirSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
const preview=fileURLToPath(new URL('../../pages/users/multi-signin-evidence.vue',import.meta.url));
const out=fileURLToPath(new URL('../../../media/pr/knowledge-multi-signin/',import.meta.url));
if(existsSync(preview))throw Error('Preview exists');mkdirSync(out,{recursive:true});
writeFileSync(preview,`<template><KnowledgeExplorer /></template><script setup lang="ts">definePageMeta({auth:false,layout:false});</script>`);
let browser;let debugPage;
try{
browser=await chromium.launch();const page=await browser.newPage({viewport:{width:1250,height:850},recordVideo:{dir:out+'video'}});page.on('pageerror',e=>console.log('PAGE ERROR',e.message));debugPage=page;let calls=[];let release;let signed=0;
const connections=()=>Array.from({length:process.env.SINGLE==='1'?1:5},(_,i)=>({id:'conn-'+i,name:'Power BI '+(i+1),type:'powerbi',auth_policy:'user_required',is_active:true,table_count:i<signed?23:0,tool_count:i<signed?5:0,file_count:i<signed?10:0,allowed_user_auth_modes:['oauth'],user_status:{has_user_credentials:i<signed,effective_auth:i<signed?'user':'none',connection:i<signed?'success':'offline'}}));
const agent=()=>({id:'multi-agent',name:'Five connections',description:'Explore financial reports and business documents.',type:'powerbi',status:'active',connections:connections()});
await page.route('**/api/**',async route=>{let path=new URL(route.request().url()).pathname;if(!path.startsWith('/api/'))return route.continue();let body=[];
if(path.endsWith('/data_sources/active'))body=[agent()];else if(path.endsWith('/data_sources/multi-agent'))body=agent();else if(path.endsWith('/connections'))body=connections();else if(path.endsWith('/oauth/authorize')){calls.push(path);await new Promise(r=>release=r);body={};}else if(path.endsWith('/whoami'))body={id:'preview',organizations:[]};else if(path.includes('counts'))body={};await route.fulfill({json:body});});
const load=async()=>{for(let attempt=0;attempt<3;attempt++){await page.goto('http://localhost:3100/users/multi-signin-evidence');try{await expect(page.getByRole('button',{name:'Sign in',exact:true})).toHaveCount(1,{timeout:10000});return;}catch(e){if(attempt===2)throw e;}}};
await load();await page.getByRole('button',{name:'Sign in',exact:true}).click();
if(process.env.BEFORE==='1'){await expect.poll(()=>calls.length).toBe(1);await page.waitForTimeout(350);await page.screenshot({path:out+'before.png'});release();console.log('BASELINE: five pending connections directly authorize the first.');}
else if(process.env.SINGLE==='1'){
 const dialog=page.getByRole('dialog');await expect(dialog.getByText('Power BI 1',{exact:true})).toBeVisible();await expect(dialog.getByRole('button',{name:'Sign in',exact:true})).toHaveCount(1);expect(calls).toEqual([]);await page.waitForTimeout(350);await page.screenshot({path:out+'single.png'});await dialog.getByRole('button',{name:'Sign in',exact:true}).click();await expect.poll(()=>calls.length).toBe(1);expect(calls[0]).toContain('/conn-0/');release();console.log('PASS: single connection opens agent modal before targeted sign-in.');
}
else{
 const dialog=page.getByRole('dialog');await expect(dialog.getByText('Power BI 1',{exact:true})).toBeVisible();await expect(dialog.getByRole('button',{name:'Sign in',exact:true})).toHaveCount(5);expect(calls).toEqual([]);await page.waitForTimeout(350);await page.screenshot({path:out+'after.png'});
 await dialog.getByRole('button',{name:'Sign in',exact:true}).nth(2).click();await expect.poll(()=>calls.length).toBe(1);expect(calls[0]).toContain('/conn-2/');await expect(dialog.locator('button[aria-busy="true"]')).toHaveCount(1);await page.waitForTimeout(350);await page.screenshot({path:out+'loading.png'});release();
 signed=1;await load();await page.getByText('Five connections',{exact:true}).click();await expect(page.getByText('Tables',{exact:true}).first()).toBeVisible();await expect(page.getByRole('button',{name:'Sign in',exact:true})).toHaveCount(1);await page.getByRole('button',{name:'Sign in',exact:true}).click();await expect(page.getByRole('dialog').getByRole('button',{name:'Sign in',exact:true})).toHaveCount(4);await expect(page.getByRole('dialog').getByText('23 tables',{exact:true})).toBeVisible();await page.waitForTimeout(350);await page.screenshot({path:out+'partial.png'});
 await page.evaluate(()=>localStorage.setItem('bow.locale','he'));await page.goto('http://localhost:3100/users/multi-signin-evidence');await expect(page.locator('html')).toHaveAttribute('dir','rtl');await page.getByRole('button',{name:'התחבר',exact:true}).first().click();await expect(page.getByRole('dialog').getByText('Power BI 1',{exact:true})).toBeVisible();await page.waitForTimeout(350);await page.screenshot({path:out+'he.png'});
 await page.setViewportSize({width:390,height:844});await page.waitForTimeout(350);await page.screenshot({path:out+'mobile.png'});expect(await page.getByRole('dialog').evaluate(el=>el.scrollWidth<=window.innerWidth)).toBe(true);
 console.log('PASS: chooser, targeted authorization, row spinner, partial-access tree and four remaining actions, Hebrew.');
}
await page.context().close();
}catch(e){await debugPage?.screenshot({path:out+'failure.png'});console.log('URL',debugPage?.url());throw e;}finally{await browser?.close();unlinkSync(preview);}

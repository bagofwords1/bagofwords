import {chromium,expect} from '@playwright/test';
import {writeFileSync,existsSync,unlinkSync,mkdirSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
const preview=fileURLToPath(new URL('../../pages/users/knowledge-signin-evidence.vue',import.meta.url));
const out=fileURLToPath(new URL('../../../media/pr/knowledge-signin-spinner/',import.meta.url));
if(existsSync(preview))throw Error('Preview exists');mkdirSync(out,{recursive:true});
writeFileSync(preview,`<template><KnowledgeExplorer /></template><script setup lang="ts">definePageMeta({auth:false,layout:false});</script>`);
let browser;
try{
 browser=await chromium.launch();const context=await browser.newContext({viewport:{width:1200,height:850},recordVideo:{dir:out+'video'}});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 let release;let calls=0;let outcome='error';
 const agents=['pbi','pbi2'].map((name,i)=>({id:'agent-'+i,name,type:'powerbi',status:'active',connections:[{id:'conn-'+i,type:'powerbi',auth_policy:'user_required',allowed_user_auth_modes:['oauth'],user_status:{has_user_credentials:false}}]}));
 await page.route('**/api/**',async route=>{const path=new URL(route.request().url()).pathname;if(!path.startsWith('/api/'))return route.continue();let body=[];
 if(path.endsWith('/data_sources/active'))body=agents;
 else if(path.endsWith('/oauth/authorize')){calls++;await new Promise(r=>release=r);body=outcome==='redirect'?{authorization_url:'http://localhost:3100/users/spinner-oauth-target'}:{};}
 else if(path.endsWith('/whoami'))body={id:'preview',organizations:[]};
 else if(path.includes('counts'))body={};
 else if(path.endsWith('/config/i18n'))body={default_locale:'en',enabled_locales:['en','he']};
 await route.fulfill({json:body});});
 await page.route('**/users/spinner-oauth-target',async route=>{await route.fulfill({contentType:'text/html',body:'<p>OAuth redirect reached</p>'});});
 const load=async()=>{await page.goto('http://localhost:3100/users/knowledge-signin-evidence');await expect(page.getByRole('button',{name:'Sign in',exact:true})).toHaveCount(2,{timeout:20000});};
 await load();const first=page.getByRole('button',{name:'Sign in',exact:true,includeHidden:true}).first();const second=page.getByRole('button',{name:'Sign in',exact:true}).nth(1);
 await first.click();await expect.poll(()=>calls).toBe(1);
 if(process.env.BEFORE==='1'){await page.screenshot({path:out+'before.png'});console.log('BASELINE: busy attribute',await first.getAttribute('aria-busy'),'spinner count',await first.locator('animateTransform').count());release();}
 else{
  await expect(first).toBeDisabled();await expect(first).toHaveAttribute('aria-busy','true');await expect(first.locator('animateTransform')).toHaveCount(1);await expect(second.locator('animateTransform')).toHaveCount(0);
  await first.evaluate(el=>el.click());expect(calls).toBe(1);await page.screenshot({path:out+'after.png'});
  release();await expect(first).toBeEnabled();await expect(first.locator('animateTransform')).toHaveCount(0);
  await load();await page.evaluate(()=>window.addEventListener('pagehide',()=>localStorage.setItem('spinner-at-redirect',String(!!document.querySelector('button[aria-busy="true"]')))));outcome='redirect';await page.getByRole('button',{name:'Sign in',exact:true}).first().click();await expect.poll(()=>calls).toBe(2);release();await page.waitForURL('**/spinner-oauth-target');expect(await page.evaluate(()=>localStorage.getItem('spinner-at-redirect'))).toBe('true');
  outcome='error';await page.evaluate(()=>localStorage.setItem('bow.locale','he'));await page.goto('http://localhost:3100/users/knowledge-signin-evidence');await expect(page.locator('html')).toHaveAttribute('dir','rtl');const he=page.locator('button.bg-blue-50').first();await he.click();await expect.poll(()=>calls).toBe(3);await expect(he).toHaveAttribute('aria-busy','true');await page.screenshot({path:out+'he.png'});release();await expect(he).toBeEnabled();
  expect(errors).toEqual([]);console.log('PASS: clicked-row spinner, disabled repeat clicks, failure cleanup, spinner through redirect, Hebrew RTL.');
 }
 await context.close();
}finally{await browser?.close();unlinkSync(preview);}

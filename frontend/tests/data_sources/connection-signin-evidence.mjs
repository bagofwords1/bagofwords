import {chromium,expect} from '@playwright/test';
import {writeFileSync,existsSync,unlinkSync,mkdirSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
const preview=fileURLToPath(new URL('../../pages/users/connection-status-evidence.vue',import.meta.url));
const out=fileURLToPath(new URL('../../../media/pr/connection-signin-status/',import.meta.url));
if(existsSync(preview))throw Error('Preview exists');mkdirSync(out,{recursive:true});
writeFileSync(preview,`<template><AgentConnectionsModal v-model="open" ds-id="preview" :connections="connections" /></template><script setup lang="ts">
definePageMeta({auth:false,layout:false});const open=ref(true);const connections=[
{id:'pbi',name:'Power BI — Me',type:'powerbi',auth_policy:'user_required',user_status:{connection:'offline',effective_auth:'none',has_user_credentials:false}},
{id:'sp',name:'SharePoint — Me',type:'sharepoint',auth_policy:'user_required',user_status:{connection:'offline',effective_auth:'user',has_user_credentials:true}},
{id:'sql',name:'PostgreSQL — Service account',type:'postgresql',user_status:{connection:'success',effective_auth:'system',has_user_credentials:false}}
];</script>`);
let browser;
try{browser=await chromium.launch();const page=await browser.newPage({viewport:{width:1100,height:700}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.route('**/api/**',async r=>{if(!new URL(r.request().url()).pathname.startsWith('/api/'))return r.continue();await r.fulfill({json:r.request().url().includes('whoami')?{id:'preview',organizations:[]}:[]});});
await page.goto('http://localhost:3100/users/connection-status-evidence');await expect(page.getByText('Power BI — Me')).toBeVisible({timeout:30000});
const badge=page.getByText('Power BI — Me').locator('../../..').locator('span.border');
if(process.env.BEFORE==='1'){await expect(badge).toContainText('Not connected');await page.screenshot({path:out+'before.png'});}
else{await expect(badge).toContainText('Sign in required');await expect(badge).toHaveClass(/gray/);await page.screenshot({path:out+'after.png'});await page.evaluate(()=>localStorage.setItem('bow.locale','he'));await page.reload();await expect(page.locator('html')).toHaveAttribute('dir','rtl');await expect(page.getByText('נדרשת כניסה')).toBeVisible();await page.screenshot({path:out+'he.png'});expect(errors).toEqual([]);}
console.log('PASS: status badge evidence');
}finally{await browser?.close();unlinkSync(preview);}

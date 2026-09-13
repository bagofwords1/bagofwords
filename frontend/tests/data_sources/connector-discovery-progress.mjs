// Replays old/new discovery payloads through the real progress component.
// Synthetic names only; live credentials and tenant metadata never enter UI fixtures.
import {chromium, expect} from '@playwright/test';
import {mkdirSync, writeFileSync, unlinkSync, existsSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
const preview=fileURLToPath(new URL('../../pages/users/connector-progress-evidence.vue',import.meta.url));
const out=fileURLToPath(new URL('../../../media/pr/connector-discovery-progress/',import.meta.url));
if(existsSync(preview))throw Error('Preview already exists');
mkdirSync(out,{recursive:true});
writeFileSync(preview,`<template><main class="min-h-screen bg-gray-50 p-8"><section class="mx-auto max-w-md rounded-xl bg-white p-8 border border-gray-200"><h1 class="text-lg font-semibold mb-6">Power BI · Schema discovery</h1><ConnectionIndexingProgress v-if="data" :indexing="data" /></section></main></template>
<script setup lang="ts">
definePageMeta({auth:false,layout:false});const data=ref(null);onMounted(async()=>{data.value=await $fetch('/api/discovery-evidence');});
</script>`);
let browser;
try {
 browser=await chromium.launch();
 const context=await browser.newContext({viewport:{width:720,height:900},recordVideo:{dir:out+'video',size:{width:720,height:900}}});
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const ts=new Date().toISOString();
 const event=(phase,done,total)=>({ts,level:'info',phase,done,total,message:`Phase: ${phase} (${done}/${total})`});
 let status={id:'synthetic-run',status:'running',phase:null,progress_done:0,progress_total:0,events:[{ts,level:'info',message:'Indexing started'}]};
 await page.route('**/api/**',async route=>{
  const path=new URL(route.request().url()).pathname;if(!path.startsWith('/api/'))return route.continue();
  await route.fulfill({json:path.endsWith('/discovery-evidence')?status:path.endsWith('/config/i18n')?{default_locale:'en',enabled_locales:['en','es','he','fr','de','it','pt','sv','ru','ar']}: {}});
 });
 const load=async()=>{await page.goto('http://localhost:3100/users/connector-progress-evidence');try{await expect(page.getByRole('progressbar')).toBeVisible({timeout:15000});}catch(e){console.log('PREVIEW ERRORS',errors,await page.locator('body').innerText());await page.screenshot({path:out+'fixture-error.png'});throw e;}};
 await load();await page.getByRole('button',{name:/Show logs/}).click();await page.screenshot({path:out+'before.png'});
 status={...status,phase:'model_introspection',current_item:'Sales model',progress_done:3,progress_total:9,events:[event('workspace_models',3,3),event('workspace_reports',3,3),event('admin_scan',0,0),event('model_metadata',9,9),event('model_introspection',3,9)]};
 await load();await expect(page.getByRole('progressbar')).toHaveAttribute('aria-valuenow','33');await expect(page.locator('section')).toContainText('Processing models');await page.getByRole('button',{name:/Show logs/}).click();await page.screenshot({path:out+'powerbi-stages.png'});
 for(const locale of ['en','es','he','fr','de','it','pt','sv','ru','ar']){
  await page.evaluate(locale=>localStorage.setItem('bow.locale',locale),locale);await load();
  await expect(page.locator('section')).not.toContainText('model_introspection');
  await expect(page.locator('section')).not.toContainText('data.setup');
  if(locale==='he'){await expect(page.locator('html')).toHaveAttribute('dir','rtl');await page.screenshot({path:out+'powerbi-he.png'});}
 }
 await page.evaluate(()=>localStorage.setItem('bow.locale','en'));
 status={...status,phase:'columns',progress_done:31500,progress_total:35000,current_item:'PUBLIC.ORDERS.ID',events:Array.from({length:200},(_,i)=>event('columns',175*(i+1),35000))};
 await load();await expect(page.getByRole('progressbar')).toHaveAttribute('aria-valuenow','90');await page.getByRole('button',{name:/Show logs/}).click();
 const logHeight=await page.locator('.max-h-48').evaluate(el=>el.getBoundingClientRect().height);expect(logHeight).toBeLessThanOrEqual(200);await page.screenshot({path:out+'large-catalog.png'});
 await page.setViewportSize({width:390,height:844});await load();await expect(page.getByRole('progressbar')).toBeInViewport();expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);await page.screenshot({path:out+'mobile.png'});
 expect(errors).toEqual([]);console.log('PASS: Power BI stage labels in all ten locales, Hebrew RTL, 35k catalog counts, bounded 200-event log viewport, mobile.');await context.close();
}finally{await browser?.close();unlinkSync(preview);}

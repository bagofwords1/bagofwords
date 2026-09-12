import { chromium, expect } from '@playwright/test';
import { writeFileSync, existsSync, unlinkSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const preview = fileURLToPath(new URL('../../pages/users/agent-landing-evidence.vue', import.meta.url));
const out = fileURLToPath(new URL('../../../media/pr/agent-landing/', import.meta.url));
const base = process.env.BASE_URL || 'http://localhost:3100';
if (existsSync(preview)) throw Error('Preview already exists');
mkdirSync(out, {recursive:true});
writeFileSync(preview, `<template><KnowledgeExplorer /></template><script setup lang="ts">
definePageMeta({auth:false,layout:false});
const role = localStorage.getItem('landing-role') || 'manager';
useState('permissions').value = role === 'manager' ? ['full_admin_access'] : role === 'viewer' ? ['view_reports'] : ['create_reports'];
useState('permissionsLoaded').value = true;
useState('resourcePermissions').value = {'data_source:landing-agent':['view']};
</script>`);
let browser, page;
const agentId = 'landing-agent';
const instructions = [1,2].map(i=>({id:'instruction-'+i,title:'Rule '+i,status:'published',text:'Rule text',data_sources:[{id:agentId,name:'Revenue'}]}));
const primaryText = 'Use the published semantic models to explore sales and performance. Keep the original metric definitions, and include the reporting period and filters with each result.\n\n' + Array.from({length:6},(_,i)=>`### ${i+1}. Keep comparisons consistent\nUse the same date range, explain missing data, and link results to the source model. Respect the fiscal calendar and always show units with amounts.`).join('\n\n');
let empty=false, blocked=false, partial=false, noActivity=false, connectionOverride=null;
let reportCalls=[], exportCalls=0, updates=[], releaseReport;
function connections() { return connectionOverride ?? [{id:'pbi',name:'Power BI',type:'powerbi',table_count:29,is_active:true,auth_policy:'user_required',allowed_user_auth_modes:['oauth'],user_status:{connection:blocked?'offline':'success',effective_auth:blocked?'none':'user',has_user_credentials:!blocked}},...(partial?[{id:'monday',name:'Monday',type:'monday',is_active:true,auth_policy:'user_required',allowed_user_auth_modes:['oauth'],user_status:{connection:'offline',effective_auth:'none',has_user_credentials:false}}]:[])]; }
function agent(){return {id:agentId,name:'Revenue',description:empty?'':'Explore Power BI models and turn business questions into reports.',type:'powerbi',status:'active',publish_status:'published',reliability_status:'training',is_public:false,connections:connections(),primary_instruction:empty?null:{id:'instruction-1',title:'Analysis guidelines',text:primaryText,references:[]}};}
try {
 browser=await chromium.launch();
 page=await browser.newPage({viewport:{width:1280,height:900},recordVideo:{dir:out+'video'}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/api/**', async r=>{
  const req=r.request();const u=new URL(req.url());const path=u.pathname;
  if(!path.startsWith('/api/'))return r.continue();let body=[];
  if(path.endsWith('/data_sources/active'))body=[agent()];
  else if(path.endsWith('/data_sources/'+agentId)){if(req.method()==='PUT')updates.push(req.postDataJSON());body=agent();}
  else if(path.endsWith('/connections'))body=connections();
  else if(path.endsWith('/connections/pbi/my-credentials')&&req.method()==='DELETE'){blocked=true;body={};}
  else if(/\/connections\/[^/]+$/.test(path))body=connections().find(c=>c.id===path.split('/').at(-1)) || {};
  else if(path.endsWith('/accessible-agents'))body=[{id:agentId,name:'Revenue'}];
  else if(path.endsWith('/indexing'))body=null;
  else if(path.endsWith('/full_schema'))body={tables:blocked?[]:Array.from({length:29},(_,i)=>({id:String(i),name:'Table '+i,is_active:true,columns:[]})),total:blocked?0:29};
  else if(path.endsWith('/whoami'))body={id:'preview',organizations:[]};
  else if(path.endsWith('/instructions/counts'))body={by_agent:{[agentId]:2},global:6};
  else if(path.endsWith('/instructions'))body={items:instructions,total:2};
  else if(path.endsWith('/prompts'))body={prompts:empty?[]:[{id:'sales',text:'What changed in sales?\nCompare sales with the previous month.'},{id:'region',text:'Revenue by region\nShow revenue by region.'}]};
  else if(path.endsWith('/timeseries'))body={activity_metrics:{messages:(noActivity?[0,0,0]:[2,4,3,7]).map(value=>({value}))}};
  else if(path.endsWith('/comparison'))body={current:{total_messages:noActivity?0:16}};
  else if(path.endsWith('/instructions/export')){exportCalls++;return r.fulfill({body:Buffer.from('synthetic export'),contentType:'application/zip'});}
  else if(path.endsWith('/reports')&&req.method()==='POST'){reportCalls.push(req.postDataJSON());await new Promise(resolve=>releaseReport=resolve);return r.fulfill({status:503,json:{detail:'Synthetic unavailable report service'}});}
  await r.fulfill({json:body});
 });
 async function load(role='manager',locale='en') {
  await page.goto(base+'/users/agent-landing-evidence');
  await page.evaluate(({role,locale})=>{localStorage.setItem('landing-role',role);localStorage.setItem('bow.locale',locale)},{role,locale});
  for(let i=0;i<3;i++){
   await page.goto(base+'/users/agent-landing-evidence');
   try{await expect(page.getByText('Revenue',{exact:true})).toBeVisible({timeout:10000});break;}catch(e){if(i===2)throw e;}
  }
  await page.getByText('Revenue',{exact:true}).click();
  await expect(page.getByRole('heading',{name:'Revenue',exact:true})).toBeVisible();
  if (!blocked && locale === 'en') await expect(page.getByText('29 tables',{exact:true})).toBeVisible();
  await page.waitForTimeout(350);
 }
 if(process.env.CONNECTIONS_ONLY==='1') {
  const landing=page.getByTestId('agent-landing');
  const row=page.getByTestId('agent-connections');
  if(process.env.CONNECTIONS_BEFORE==='1') {
   blocked=true;await load('member');
   await expect(row).toHaveCount(0);
   await page.screenshot({path:out+'connections-before.png'});
   console.log('BASELINE: linked Power BI connection is absent from the signed-out landing page.');
  } else {
   await load('member');
   await expect(row.getByRole('button')).toHaveCount(1);
   const pbi=row.getByRole('button',{name:'Power BI Connected',exact:true});
   await expect(pbi).toBeVisible();
   await expect(pbi.locator('[data-connection-status]')).toHaveClass(/bg-green-500/);
   await page.screenshot({path:out+'connections-after.png'});
   await pbi.click();
   const dialog=page.getByRole('dialog');
   await expect(dialog.getByRole('heading',{name:'Power BI',exact:true})).toBeVisible();
   await expect(dialog.getByRole('button',{name:'Manage connection',exact:true})).toHaveCount(0);
   await dialog.getByRole('button',{name:'Sign out',exact:true}).click();
   const unsigned=row.getByRole('button',{name:'Power BI Sign in required',exact:true});
   await expect(unsigned).toBeVisible();
   await expect(unsigned.locator('[data-connection-status]')).toHaveClass(/bg-gray-400/);
   await expect(page.getByTestId('agent-instruction-preview')).toHaveCount(0);
   await unsigned.click();
   await expect(dialog.getByRole('button',{name:'Sign in',exact:true})).toBeVisible();
   await page.screenshot({path:out+'connections-detail.png'});
   await dialog.getByRole('button',{name:'Close',exact:true}).click();
   await page.screenshot({path:out+'connections-signed-out.png'});
   blocked=false;partial=true;
   connectionOverride=[...connections(),
    {id:'snow',name:'Snowflake',type:'snowflake',user_status:{connection:'error'}},
    {id:'sql',name:'SQL Server',type:'sqlserver',indexing:{status:'running'}},
    {id:'long',name:'A very long warehouse connection name for the international finance department',type:'postgresql'}];
   await load('member');
   await expect(row.getByRole('button')).toHaveCount(5);
   await expect(row.getByRole('button',{name:'Monday Sign in required',exact:true}).locator('[data-connection-status]')).toHaveClass(/bg-gray-400/);
   await expect(row.getByRole('button',{name:'Snowflake Not connected',exact:true}).locator('[data-connection-status]')).toHaveClass(/bg-red-500/);
   await expect(row.getByRole('button',{name:'SQL Server Indexing',exact:true}).locator('[data-connection-status]')).toHaveClass(/animate-pulse/);
   await page.screenshot({path:out+'connections-many.png'});
   await page.setViewportSize({width:390,height:844});await load('member','he');
   await expect(page.locator('html')).toHaveAttribute('dir','rtl');
   await expect(row.getByRole('button')).toHaveCount(5);
   expect(await landing.evaluate(el=>el.scrollWidth<=el.clientWidth)).toBe(true);
   for(const button of await row.getByRole('button').all()){
    const b=await button.boundingBox();expect(b.x).toBeGreaterThanOrEqual(0);expect(b.x+b.width).toBeLessThanOrEqual(390);
   }
   await page.screenshot({path:out+'connections-he-mobile.png'});
   await page.evaluate(()=>document.documentElement.classList.add('dark'));
   await page.screenshot({path:out+'connections-dark-mobile.png'});
   connectionOverride=[];await load('member');
   await expect(row).toHaveCount(0);
   expect(errors).toEqual([]);
   console.log('PASS: linked connections visible before sign-in; exact detail opens; sign-out updates status and instruction access; one/many/none, real failure versus missing sign-in, Hebrew/mobile/dark verified.');
  }
 } else if(process.env.SIGNIN_ONLY==='1') {
  blocked=true;await load('manager');
  const landing=page.getByTestId('agent-landing');
  const instruction=page.getByTestId('agent-instruction-preview');
  if(process.env.SIGNIN_BEFORE==='1') {
   await expect(instruction).toBeVisible();
   await expect(landing.getByRole('button',{name:'Sign in',exact:true})).toBeVisible();
   await page.screenshot({path:out+'signin-before.png'});
   console.log('BASELINE: signed-out agent still displays primary instruction and editing controls.');
  } else {
   await expect(instruction).toHaveCount(0);
   await expect(page.getByTestId('agent-resource-counts')).toHaveCount(0);
   await expect(landing.getByRole('button',{name:'Read more',exact:true})).toHaveCount(0);
   await expect(landing.getByRole('button',{name:'Edit',exact:true})).toHaveCount(0);
   await expect(landing.getByRole('button',{name:'Change',exact:true})).toHaveCount(0);
   await expect(landing.getByRole('button',{name:'Sign in',exact:true})).toBeVisible();
   await page.screenshot({path:out+'signin-after.png'});
   empty=true;await load('manager');
   await expect(page.getByTestId('agent-instruction-empty')).toHaveCount(0);
   await expect(landing.locator('.tiptap')).toHaveCount(0);
   empty=false;await load('member','he');
   await expect(instruction).toHaveCount(0);
   await page.screenshot({path:out+'signin-he.png'});
   await page.setViewportSize({width:390,height:844});
   await page.screenshot({path:out+'signin-mobile.png'});
   blocked=false;await load('member');
   await expect(instruction).toBeVisible();
   partial=true;await load('member');
   await expect(instruction).toBeVisible();
   await expect(page.getByTestId('agent-new-report')).toBeVisible();
   expect(errors).toEqual([]);
   console.log('PASS: instruction content, editing and empty-state controls hidden while sign-in blocks access; restored for signed-in and partially usable agents; Hebrew/mobile verified.');
  }
 } else {
 await load();
 if(process.env.BEFORE==='1'){
  await page.screenshot({path:out+'before.png'});
  await expect(page.getByRole('heading',{name:'6. Keep comparisons consistent'})).toBeVisible();
  console.log('BASELINE: action-heavy header and full long instruction dominate the overview.');
 }else{
  const landing=page.getByTestId('agent-landing');
  const previewBody=page.getByTestId('agent-instruction-preview');
  await expect(landing.getByRole('button',{name:'2 instructions',exact:true})).toBeVisible();
  await expect(landing.getByText(/0 tools|0 files/)).toHaveCount(0);
  await expect(landing.getByRole('button',{name:'Read more',exact:true})).toBeVisible();
  await expect(page.getByTestId('agent-starters')).toBeVisible();
  const heroBox=await page.getByTestId('agent-hero').boundingBox();
  const landingBox=await landing.boundingBox();
  expect(Math.abs(heroBox.x+heroBox.width/2-landingBox.x-landingBox.width/2)).toBeLessThan(2);
  expect(await landing.getByRole('heading',{name:'Revenue',exact:true}).evaluate(el=>getComputedStyle(el).fontSize)).toBe(await page.locator('h1').evaluate(el=>getComputedStyle(el).fontSize));
  await page.screenshot({path:out+'after.png'});
  const more=landing.getByRole('button',{name:'Read more',exact:true});
  const collapsed=(await previewBody.boundingBox()).height;
  await more.click();
  await expect(landing.getByRole('button',{name:'Read less',exact:true})).toHaveAttribute('aria-expanded','true');
  expect((await previewBody.boundingBox()).height).toBeGreaterThan(collapsed+200);
  await landing.getByRole('button',{name:'Read less',exact:true}).click();
  await previewBody.getByRole('button',{name:'Edit',exact:true}).click();
  await expect(landing.locator('.tiptap')).toBeVisible();
  await landing.getByRole('button',{name:'Cancel',exact:true}).click();
  await landing.getByLabel('Agent actions',{exact:true}).click();
  await expect(page.getByRole('menuitem',{name:'Edit conversation starters',exact:true})).toBeVisible();
  const download=page.waitForEvent('download');
  await page.getByRole('menuitem',{name:'Download agent data',exact:true}).click();
  expect((await download).suggestedFilename()).toBe('Revenue-agent-export.zip');
  expect(exportCalls).toBe(1);
  await page.getByTestId('agent-new-report').evaluate(el=>{el.click();el.click()});
  await expect.poll(()=>reportCalls.length).toBe(1);
  expect(reportCalls[0].data_sources).toEqual([agentId]);
  await expect(page.getByTestId('agent-new-report')).toBeDisabled();
  releaseReport();await expect(page.getByTestId('agent-new-report')).toBeEnabled();
  await page.getByTestId('agent-starters').getByRole('button',{name:'What changed in sales?',exact:true}).click();
  await expect.poll(()=>reportCalls.length).toBe(2);
  expect(reportCalls[1].new_message).toBe('Compare sales with the previous month.');
  expect(reportCalls[1].data_sources).toEqual([agentId]);releaseReport();
  await expect(page.getByTestId('agent-new-report')).toBeEnabled();
  empty=true;noActivity=true;await load();
  await expect(page.getByTestId('agent-starters')).toHaveCount(0);
  await expect(page.getByTestId('agent-activity')).toHaveCount(0);
  await expect(page.getByTestId('agent-instruction-empty')).toBeVisible();
  await expect(landing.getByText('No primary instruction',{exact:true})).toHaveCount(0);
  await page.screenshot({path:out+'empty-manager.png'});
  await landing.getByRole('button',{name:'Add instruction',exact:true}).click();
  await expect(landing.locator('.tiptap')).toBeVisible();
  await landing.getByRole('button',{name:'Cancel',exact:true}).click();
  await load('member');await expect(page.getByTestId('agent-instruction-empty')).toHaveCount(0);
  await expect(page.getByTestId('agent-new-report')).toBeVisible();
  await landing.getByLabel('Agent actions',{exact:true}).click();
  await expect(page.getByRole('menuitem',{name:'Download agent data',exact:true})).toHaveCount(0);
  await page.keyboard.press('Escape');await page.waitForTimeout(300);
  await page.screenshot({path:out+'empty-member.png'});
  await load('viewer');await expect(page.getByTestId('agent-new-report')).toHaveCount(0);
  blocked=true;await load('member');
  await expect(page.getByTestId('agent-new-report')).toHaveCount(0);
  await landing.getByRole('button',{name:'Sign in',exact:true}).click();
  await expect(page.getByRole('dialog').getByText('Power BI',{exact:true})).toBeVisible();
  blocked=false;partial=true;empty=false;noActivity=false;await load('member');
  await expect(page.getByTestId('agent-new-report')).toBeVisible();
  await expect(landing.getByRole('button',{name:'Sign in',exact:true})).toBeVisible();
  await page.screenshot({path:out+'partial-access.png'});
  partial=false;await load('manager','he');
  await expect(page.locator('html')).toHaveAttribute('dir','rtl');
  await expect(landing.getByText('באימון',{exact:true})).toBeVisible();
  await page.screenshot({path:out+'he.png'});
  await page.setViewportSize({width:390,height:844});await page.waitForTimeout(200);
  await page.screenshot({path:out+'mobile.png'});
  await landing.getByRole('button',{name:'באימון',exact:true}).last().click();
  const stageMenu=page.getByRole('menu');await expect(stageMenu).toBeVisible();
  let menuBox=await stageMenu.boundingBox();expect(menuBox.x).toBeGreaterThanOrEqual(0);expect(menuBox.x+menuBox.width).toBeLessThanOrEqual(390);
  await page.keyboard.press('Escape');await page.waitForTimeout(200);
  await previewBody.getByRole('button',{name:'שנה',exact:true}).click();
  const pickerSearch=page.getByPlaceholder('Search instructions…');await expect(pickerSearch).toBeVisible();
  const pickerBox=await pickerSearch.boundingBox();expect(pickerBox.x).toBeGreaterThanOrEqual(0);expect(pickerBox.x+pickerBox.width).toBeLessThanOrEqual(390);
  await landing.getByRole('heading',{name:'Revenue',exact:true}).click();
  await page.getByTestId('agent-hero').scrollIntoViewIfNeeded();

  expect(await landing.evaluate(el=>el.scrollWidth<=el.clientWidth)).toBe(true);
  await page.evaluate(()=>document.documentElement.classList.add('dark'));
  await page.screenshot({path:out+'dark-mobile.png'});
  expect(await previewBody.locator('.instruction-prose').evaluate(el=>getComputedStyle(el).color)).toBe('rgb(156, 163, 175)');
  expect(errors).toEqual([]);
  console.log('PASS: centered compact layout, counts, expansion, editing, export, report payloads/duplicate prevention, empty states, role gates, full/partial sign-in, Hebrew, mobile and dark mode.');
 }
 }
 await page.context().close();
} catch(e){await page?.screenshot({path:out+'failure.png'});throw e;} finally {releaseReport?.();await browser?.close();unlinkSync(preview);}

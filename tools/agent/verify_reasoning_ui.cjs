// Synthetic report fixture; run with a local Nuxt server (default port 3100).
const path=require('path');
const root=path.resolve(__dirname,'../..');
const { chromium,expect }=require(path.join(root,'frontend/node_modules/@playwright/test'));
const fs=require('fs');
let browser;
const dir=path.join(root,'media/pr/reasoning-effort-streaming');
const reportPath=path.join(root,'frontend/pages/reports/[id]/index.vue');
const saved=fs.readFileSync(reportPath,'utf8');
if(process.env.SHOT==='before')fs.writeFileSync(reportPath,saved.replace("block.reasoning || block.plan_decision?.reasoning || ''","block.plan_decision?.reasoning || block.reasoning || ''"));
(async()=>{
 fs.mkdirSync(dir,{recursive:true});
 browser=await chromium.launch({headless:true});
 const context=await browser.newContext({viewport:{width:1400,height:1000},recordVideo:{dir:dir+'/video'}});
 await context.addCookies([{name:'auth.token',value:'synthetic-test-token',domain:'localhost',path:'/'}]);
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text().slice(0,250))});
 await page.route('**/api/**',async route=>{
 const u=new URL(route.request().url()),p=u.pathname;if(!p.startsWith('/api/'))return route.continue();let data=[];
 const org={id:'synthetic-org',name:'Reasoning verification',role:'admin',permissions:['view_reports','create_reports','manage_reports']};
 if(p.includes('whoami'))data={id:'synthetic-user',email:'demo@example.test',is_verified:true,organizations:[org]};
 else if(p.includes('onboarding')) data={completed:true,dismissed:true};
 else if(p.includes('organization/locale'))data={effective_locale:'en'};
 else if(p.includes('organization/settings'))data={};
 else if(p.includes('permissions'))data={permissions:org.permissions};
 else if(p.includes('/completions'))data={completions:[{id:'synthetic-completion',role:'system',status:'success',model:{name:'Synthetic model',model_id:'gpt-5-mini',provider:{provider_type:'openai'}},completion_blocks:[{id:'synthetic-block',block_index:0,loop_index:0,status:'success',content:'The table is ready.',reasoning:'I will check the requested columns.\n\nThe code will preserve the renamed columns and validate the result.',plan_decision:{reasoning:'I will check the requested columns.',assistant:'The table is ready.',metrics:{thinking_ms:1500}}}]}],has_more:false};
 else if(p==='/api/reports/reasoning-probe')data={id:'reasoning-probe',title:'Reasoning verification',status:'success',data_sources:[],files:[],user_id:'synthetic-user'};
 else if(p.includes('/context'))data={};
 else if(p.includes('/llm/models'))data=[{id:'synthetic-model',model_id:'gpt-5-mini',name:'Synthetic model',is_enabled:true,provider:{provider_type:'openai'}}];
 else if(p.includes('/config'))data={};
 await route.fulfill({json:data});});
 await page.goto((process.env.REASONING_PREVIEW_URL || 'http://localhost:3100')+'/reports/reasoning-probe');
 try { await page.locator('.thinking-header').first().click({timeout:20000}); } catch(e) { console.log(JSON.stringify({url:page.url(),body:(await page.locator('body').innerText()).slice(0,1500),errors})); throw e; }
 await expect(page.locator('.thinking-content')).toContainText('I will check the requested columns.');
 if(process.env.SHOT==='before')await expect(page.locator('.thinking-content')).not.toContainText('The code will preserve');
 if(process.env.SHOT!=='before')await expect(page.locator('.thinking-content')).toContainText('The code will preserve');
 if(process.env.FLOW==='1') {
   await page.evaluate(async()=>{
     let vm=document.querySelector('.thinking-box').__vueParentComponent;
     while(vm && !vm.setupState.handleStreamingEvent)vm=vm.parent;
     if(!vm)throw new Error('Report stream handler not found');
     const handle=vm.setupState.handleStreamingEvent;
     await handle('block.delta.text',{block_id:'synthetic-block',field:'reasoning',text:'I will check the requested columns.'},0);
     for(const token of ['\n\nThe code will ', 'preserve the renamed columns ', 'and validate the result.']) {
       await handle('block.delta.token',{block_id:'synthetic-block',field:'reasoning',token},0);
       await new Promise(resolve=>setTimeout(resolve,300));
     }
   });
   await expect(page.locator('.thinking-content')).toContainText('The code will preserve');
   await page.reload();
   await page.locator('.thinking-header').first().click();
   await expect(page.locator('.thinking-content')).toContainText('The code will preserve');
 }
 await expect(page.locator('.thinking-content')).toHaveCSS('opacity','1');
 await page.screenshot({path:dir+'/'+(process.env.SHOT||'after')+'.png',fullPage:true,animations:'disabled'});
 expect(errors).toEqual([]);
 console.log(JSON.stringify({url:page.url(),reasoning:await page.locator('.thinking-content').innerText(),errors}));
 await context.close();await browser.close();
})().catch(e=>{console.error(e.message);process.exitCode=1}).finally(async()=>{if(process.env.SHOT==='before')fs.writeFileSync(reportPath,saved);if(browser)await browser.close()});

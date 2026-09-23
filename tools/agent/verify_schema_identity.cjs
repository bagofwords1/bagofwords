// Run from the repository root with a local Nuxt dev server at SCHEMA_PREVIEW_URL.
// Only synthetic API responses are used. No upstream services or saved data.
const { chromium, expect } = require('../../frontend/node_modules/@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const preview = path.join(root, 'frontend/pages/users/schema-preview.vue');
const evidence = path.join(root, 'media/pr/powerbi-schema-identity');
const url = process.env.SCHEMA_PREVIEW_URL || 'http://localhost:3100/users/schema-preview';
const fixture = `<template><div class="min-h-screen bg-gray-50 p-12"><div class="mx-auto max-w-5xl rounded-xl border bg-white p-6"><h1 class="text-lg font-semibold mb-1">Sales analytics</h1><p class="text-sm text-gray-500 mb-6">Tables available to this agent</p><TablesSelector ds-id="preview-agent" schema="full" :show-stats="false" /></div></div></template>
<script setup lang="ts">
import TablesSelector from '~/components/datasources/TablesSelector.vue'
definePageMeta({layout:false,auth:false})
usePermissions().value = ['manage_connections']
usePermissionsLoaded().value = true
</script>`;
(async () => {
  if (fs.existsSync(preview)) throw new Error('Preview path already exists; remove the previous temporary harness first.');
  fs.writeFileSync(preview, fixture);
  fs.mkdirSync(evidence, { recursive: true });
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    for (const scenario of ['personal', 'system', 'partial', 'failed', 'disconnected', 'reader', 'he']) {
      const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, recordVideo: scenario === 'partial' ? { dir: path.join(evidence, 'video'), size: {width:1280,height:900} } : undefined });
      if (scenario === 'he') await context.addInitScript(() => localStorage.setItem('bow.locale', 'he'));
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      let posts = [], refreshing = false, polls = 0;
      let identity = scenario === 'system' ? 'system' : scenario === 'disconnected' ? 'none' : 'user';
      const connection = () => ({ id:'demo-pbi',name:'Sales semantic model',type:'powerbi',auth_policy:'user_required',user_status:{effective_auth:identity,has_user_credentials:identity !== 'none',can_switch_identity:scenario !== 'reader' && identity !== 'none',query_identity:identity === 'system'?'service_account':'self'} });
      let reads = 0;
      await page.route('**/api/**', async route => {
        const u = new URL(route.request().url());
        if (!u.pathname.startsWith('/api/')) return route.continue();
        let data = {};
        const org = {id:'demo-org',name:'Demo workspace',role:'admin',permissions:['manage_connections']};
        if(u.pathname.includes('organizations')) data = [org];
        if(u.pathname.includes('whoami')) data = {id:'demo-user',email:'demo@example.test',organizations:[org]};
        if(u.pathname.endsWith('/connections')) data = [connection()];
        if(u.pathname.endsWith('/connections/demo-pbi')) data = connection();
        if(u.pathname.endsWith('/accessible-agents')) data = [];
        if(u.pathname.endsWith('/query-identity')) {
          identity=route.request().postDataJSON().query_identity==='self'?'user':'system'; data=connection().user_status;
        }
        if(u.pathname.includes('full_schema')) {
          reads++;
          data = {tables:['Orders','Customers','Products'].map((name,i)=>({id:String(i),name:'Sales/'+name,is_active:true,columns:[{name:i?'Name':'OrderDate',dtype:'string'},{name:i?'Category':'Revenue',dtype:'number'}],fks:[],pks:[],metadata_json:{}})),total:3,total_tables:3,page:1,page_size:100,total_pages:1,has_more:false,schemas:[],selected_count:3};
        }
        if(u.pathname.includes('/indexing')) {
          if (refreshing) polls++;
          data={id:'demo-job',status:refreshing && polls<2?'running':scenario==='failed' && refreshing?'failed':'completed',finished_at:'2026-09-22T12:20:00Z',scope:identity==='system'?'org':'user',stats:{table_count:3,...(scenario==='partial' && refreshing?{unreadable_dataset_count:1}:{})},events:[]};
          if (identity==='user') expect(u.searchParams.get('scope')).toBe('user');
          if (identity==='system') expect(u.searchParams.get('scope')).toBe('org');
        }
        if(route.request().method()==='POST') {
          posts.push(u.pathname+u.search); refreshing=true;
          data={indexing:{id:'demo-job',status:'running',scope:identity==='system'?'org':'user'}};
        }
        await route.fulfill({json:data});
      });
      await page.goto(url);
      const strip=page.getByTestId('schema-identity');
      await expect(strip).toBeVisible({timeout:60000});
      await expect(page.getByText('Sales/Orders',{exact:true})).toBeVisible();
      if(scenario==='disconnected') {
        await expect(strip).toContainText('Connect your account');
        await expect(strip.getByRole('button',{name:'Refresh schema',exact:true})).toHaveCount(0);
      } else if(scenario==='he') {
        await expect(page.locator('html')).toHaveAttribute('dir','rtl');
        await expect(strip).toContainText('החשבון שלי');
        await page.screenshot({path:path.join(evidence,'after-he.png')});
      } else {
        await expect(strip).toContainText(identity==='system'?'Service account':'My account');
        if(scenario==='reader') await expect(strip.getByRole('button',{name:'My account',exact:true})).toHaveCount(0);
        const beforeReads=reads;
        await strip.getByRole('button',{name:'Refresh schema',exact:true}).click();
        await expect(strip).toContainText(scenario==='partial'?'Some models could not be refreshed':scenario==='failed'?'Schema refresh did not complete':'Schema updated.',{timeout:15000});
        expect(posts).toEqual([identity==='system'?'/api/connections/demo-pbi/refresh':'/api/connections/demo-pbi/my-schema/refresh?background=true']);
        await expect.poll(()=>reads).toBeGreaterThan(beforeReads);
        await page.screenshot({path:path.join(evidence,scenario==='personal'?'after.png':`after-${scenario}.png`)});
        if(scenario==='personal') {
          await strip.getByRole('button',{name:'My account',exact:true}).click();
          await expect(page.getByRole('radio')).toHaveCount(2);
          await page.getByRole('radio').nth(1).check();
          await expect(strip).toContainText('Service account');
        }
      }
      expect(errors).toEqual([]);
      await context.close();
      console.log(`PASS ${scenario}`);
    }
  } finally {
    await browser?.close();
    fs.unlinkSync(preview);
  }
})().catch(e=>{console.error(e);process.exitCode=1});

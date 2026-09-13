import { chromium, expect } from '@playwright/test';
import { writeFileSync, existsSync, unlinkSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { loadConnectionRegistry } from './connection-registry.mjs';
const registry = loadConnectionRegistry();
const routePath = fileURLToPath(new URL('../../pages/users/connection-fields-evidence.vue', import.meta.url));
const out = fileURLToPath(new URL('../../../media/pr/connection-setup-corrections/', import.meta.url));
if (existsSync(routePath)) throw new Error('Refusing to overwrite an existing preview route');
mkdirSync(out, {recursive:true});
writeFileSync(routePath, `<template><main class="min-h-screen bg-gray-50 dark:bg-gray-950 p-8"><div v-if="mode" class="max-w-xl mx-auto"><ConnectForm :initial-type="type" :mode="mode" :connection-id="mode === 'edit' ? 'demo' : undefined" :initial-values="mode === 'edit' ? {name:'Analytics',has_credentials:true} : undefined" :show-test-button="true" :show-l-l-m-toggle="true" :force-show-system-credentials="true" :show-require-user-auth-toggle="true" :allow-name-edit="true" /></div><AddConnectionModal v-else v-model="open" :initial-selected-type="type" /></main></template>
<script setup lang="ts">
import ConnectForm from '~/components/datasources/ConnectForm.vue';
definePageMeta({auth:false,layout:false});
const route=useRoute(); const type=String(route.query.type || 'postgresql'); const mode=route.query.mode as any;
const open=ref(false); onMounted(()=>{open.value=true});
</script>`);
let browser;
try {
  browser=await chromium.launch();
  const context=await browser.newContext({viewport:{width:1440,height:1100},recordVideo:{dir:out+'video',size:{width:1440,height:1100}}});
  const page=await context.newPage();
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  let testResponse={success:true,message:'Connection successful'};
  let testCalls=[];let saves=[];
  await page.route('**/api/**',async route=>{
    const url=new URL(route.request().url()); if(!url.pathname.startsWith('/api/'))return route.continue();
    let body={};
    if(url.pathname.endsWith('/available_data_sources'))body=registry.available;
    else if(url.pathname.endsWith('/fields'))body=registry.fields[decodeURIComponent(url.pathname.split('/').at(-2))];
    else if(url.pathname.endsWith('/test_connection')||url.pathname.endsWith('/test')) {testCalls.push(route.request().postDataJSON());body=testResponse;}
    else if(url.pathname.endsWith('/connections')&&route.request().method()==='POST') {saves.push(route.request().postDataJSON());body={id:'demo',...saves.at(-1),indexing:{id:'run',status:'completed',progress_done:27,progress_total:27,stats:{table_count:27}}};}
    else if(url.pathname.endsWith('/indexing'))body={id:'run',status:'completed',progress_done:27,progress_total:27,stats:{table_count:27}};
    else if(url.pathname.endsWith('/license'))body={licensed:true,tier:'enterprise',features:[]};
    else if(url.pathname.endsWith('/config/i18n'))body={default_locale:'en',enabled_locales:['en','es','he']};
    else if(/organizations|catalog|presets|demos/.test(url.pathname))body=[];
    await route.fulfill({json:body});
  });
  const visit=async(type,mode='')=>{
    await page.goto(`${process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3100'}/users/connection-fields-evidence?type=${type}${mode?'&mode='+mode:''}`);
    await page.locator('form').waitFor();
    await expect(page.locator('form input').first()).toBeVisible();
  };
  await visit('postgresql');
  if(process.env.CAPTURE_BEFORE){
    await page.getByRole('button',{name:'Advanced options',exact:true}).click();
    await page.screenshot({path:out+'before-postgres.png'});
    const count=await page.locator('#sslmode option').count();
    await visit('powerbi');await page.screenshot({path:out+'before-powerbi.png'});
    expect(count,'PostgreSQL must render all six actual SSL options').toBe(6);
  } else {
    await expect(page.locator('#sslmode option')).toHaveCount(6);
    await expect(page.locator('#sslmode')).toHaveValue('prefer');
    await expect(page.getByRole('button',{name:'Advanced options',exact:true})).toHaveCount(0);
    const buttonStyle = el => {
      const s=getComputedStyle(el);return {background:s.backgroundColor,fontSize:s.fontSize,height:s.height,padding:s.padding,borderRadius:s.borderRadius};
    };
    const primaryStyle=await page.getByRole('button',{name:'Connect',exact:true}).evaluate(buttonStyle);
    await page.locator('#sslmode').selectOption('verify-full');
    await page.screenshot({path:out+'after-postgres.png'});
    await page.locator('#host').fill('db.example.com');
    await page.locator('#database').fill('analytics');
    await page.locator('#user').fill('readonly');
    await page.locator('#password').fill('synthetic-example');
    await page.getByRole('button',{name:'Connect',exact:true}).click();
    await expect(page.locator('aside')).toContainText('Connection saved');
    expect(testCalls.at(-1).config.sslmode).toBe('verify-full');
    expect(saves.at(-1).config.sslmode).toBe('verify-full');
    await page.screenshot({path:out+'saved.png'});
    // Shared legacy callers: real schema, both onboarding and edit mode.
    for(const mode of ['onboarding','edit']) {
      await visit('postgresql',mode);
      await expect(page.locator('#sslmode option')).toHaveCount(6);
      await page.getByRole('button',{name:/^Test connection$/i}).click();
      const save=page.getByRole('button',{name:/^Save and Continue$/i});
      await expect(save).toBeEnabled();
      expect(await save.evaluate(buttonStyle)).toEqual(primaryStyle);
      await page.locator('#connection-name').fill('Renamed connection');
      await expect(save).toBeEnabled();
      await page.locator('#sslmode').selectOption('require');
      await expect(save).toBeDisabled();
    }
    await visit('teradata');
    await expect(page.locator('#logmech option')).toHaveCount(4);
    await expect(page.locator('#logmech')).toHaveValue('TD2');
    await page.locator('#logmech').selectOption('LDAP');
    await page.screenshot({path:out+'after-teradata.png'});
    await visit('MSSQL');
    await expect(page.locator('#odbc_driver option')).toHaveCount(2);
    await page.locator('#odbc_driver').selectOption('17');
    await page.getByRole('button',{name:'Connect',exact:true}).click();
    await expect.poll(()=>saves.at(-1)?.type).toBe('MSSQL');
    expect(saves.at(-1).config.odbc_driver).toBe(17);
    await visit('powerbi');
    await page.screenshot({path:out+'after-powerbi.png'});
    // Identical spacing between sections and between credential fields.
    const gaps=await page.locator('form').evaluate(form=>{
      const inputs=[...form.querySelectorAll('input')].filter(e=>e.getBoundingClientRect().height);
      return inputs.slice(1).map((input,i)=>input.closest('div').querySelector('label')?.getBoundingClientRect().top-inputs[i].getBoundingClientRect().bottom).filter(Number.isFinite);
    });
    expect(Math.max(...gaps)-Math.min(...gaps)).toBeLessThanOrEqual(2);
    testResponse={success:false,connectivity:true,message:'The service principal cannot query these datasets.'};
    await page.getByRole('switch').first().click();
    await page.getByRole('button',{name:'Connect',exact:true}).click();
    await expect(page.locator('aside')).toContainText('service principal');
    await expect(page.locator('aside')).toContainText('Connection saved');
    await page.screenshot({path:out+'powerbi-warning.png'});
    await page.evaluate(()=>localStorage.setItem('bow.locale','he'));
    await visit('postgresql');await expect(page.locator('html')).toHaveAttribute('dir','rtl');
    await page.screenshot({path:out+'after-he.png'});
    await page.evaluate(()=>{localStorage.setItem('bow.locale','en');localStorage.setItem('nuxt-color-mode','dark')});
    await visit('postgresql');await page.screenshot({path:out+'after-dark.png'});
    await page.evaluate(()=>localStorage.setItem('nuxt-color-mode','light'));
    await page.setViewportSize({width:390,height:844});await visit('postgresql');
    await expect(page.getByRole('button',{name:'Connect',exact:true})).toBeInViewport();
    await page.locator('#sslmode').scrollIntoViewIfNeeded();await page.screenshot({path:out+'after-mobile.png'});
    expect(errors).toEqual([]);
    console.log('PASS: actual PostgreSQL/Teradata/numeric choices, submitted values, edit/onboarding test invalidation, Power BI spacing and warning, saved state, RTL, dark mode and mobile.');
  }
  await context.close();
} finally {await browser?.close();unlinkSync(routePath);}

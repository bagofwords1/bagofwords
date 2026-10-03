import { chromium, expect } from '@playwright/test';
import { writeFileSync, existsSync, unlinkSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { loadConnectionRegistry } from './connection-registry.mjs';
const registry=loadConnectionRegistry();
const routePath=fileURLToPath(new URL('../../pages/users/connection-edit-evidence.vue',import.meta.url));
const out=fileURLToPath(new URL('../../../media/pr/connection-edit/',import.meta.url));
if(existsSync(routePath))throw Error('Preview already exists');
mkdirSync(out,{recursive:true});
writeFileSync(routePath,`<template><EditConnectionModal v-model="open" :connection="connection" :initial-values="values" :last-test="lastTest" @success="saved++"/><div :data-saved="saved" /></template>
<script setup lang="ts">
definePageMeta({auth:false,layout:false});const open=ref(false);onMounted(()=>open.value=true);const saved=ref(0);
const connection={id:'demo',type:'postgresql',user_status:{connection:'not_connected',last_checked_at:'2026-09-12T10:00:00Z'}};
const values={name:'Analytics',config:{host:'db.example.com',port:5432,database:'analytics',sslmode:'require'},has_credentials:true,credentials:{user:'reader'},auth_policy:'system_only'};
const lastTest={success:false,message:'Role "missing_user" does not exist'};
</script>`);
let browser;
try {
 browser=await chromium.launch();const context=await browser.newContext({viewport:{width:1440,height:1100},recordVideo:{dir:out+'video'}});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));let ok=false, saves=0, tests=0;
 await page.route('**/api/**',async route=>{const path=new URL(route.request().url()).pathname;if(!path.startsWith('/api/'))return route.continue();let body={};
 if(path.endsWith('/fields'))body=registry.fields.postgresql;
 else if(path.endsWith('/available_data_sources'))body=registry.available;
 else if(path.endsWith('/test')){tests++;body={success:ok,message:ok?'Connection successful':'Permission denied'};expect(route.request().postDataJSON().config.sslmode).toBe('require');}
 else if(path.endsWith('/indexing'))body={id:'run',status:'failed',progress_done:5,progress_total:27,error:'Schema discovery permission denied',events:[]};
 else if(path==='/api/connections/demo' && route.request().method()==='PUT'){saves++;body={id:'demo'};expect(route.request().postDataJSON()).not.toHaveProperty('credentials');}
 else if(path.endsWith('/config/i18n'))body={default_locale:'en',enabled_locales:['en','he']};
 await route.fulfill({json:body});});
 await page.goto('http://localhost:3100/users/connection-edit-evidence');await page.locator('#host').waitFor();await page.waitForTimeout(350);
 await expect(page.locator('#sslmode option')).toHaveCount(6);await expect(page.locator('aside')).toContainText('Role "missing_user" does not exist');await expect(page.locator('aside')).toContainText('Schema discovery permission denied');expect(tests).toBe(0);await expect(page.locator('#user')).toHaveValue('reader');
 await page.getByRole('button',{name:'Change',exact:true}).click();await expect(page.locator('#user')).toHaveValue('reader');await expect(page.locator('#password')).toHaveValue('');await page.locator('#user').fill('changed-reader');await expect(page.locator('aside')).toContainText('Settings changed');await page.getByRole('button',{name:'Cancel',exact:true}).click();await expect(page.locator('#user')).toHaveValue('reader');
 await page.screenshot({path:out+'initial-error.png'});
 await page.getByRole('button',{name:'Test again',exact:true}).click();await expect(page.locator('aside')).toContainText('Permission denied');expect(saves).toBe(0);
 await page.getByRole('button',{name:'Save and Continue',exact:true}).click();await expect.poll(()=>tests).toBe(2);expect(saves).toBe(0);
 ok=true;await page.getByRole('button',{name:'Test again',exact:true}).click();await expect(page.locator('aside')).toContainText('Connection successful');await expect(page.locator('aside')).toContainText('Schema discovery permission denied');await page.screenshot({path:out+'tested.png'});
 await page.getByRole('button',{name:'Save and Continue',exact:true}).click();await expect.poll(()=>saves).toBe(1);await expect(page.locator('[data-saved]')).toHaveAttribute('data-saved','1');
 await page.evaluate(()=>localStorage.setItem('bow.locale','he'));await page.reload();await page.locator('#host').waitFor();await page.waitForTimeout(350);await expect(page.locator('html')).toHaveAttribute('dir','rtl');await page.screenshot({path:out+'he.png'});
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:out+'mobile.png'});await expect(page.locator('button').last()).toBeInViewport();expect(errors).toEqual([]);
 console.log('PASS: previous test and discovery errors; no test on open; test without save; failed test blocks save; saved credentials preserved; one update; RTL and mobile.');await context.close();
}finally{await browser?.close();unlinkSync(routePath);}

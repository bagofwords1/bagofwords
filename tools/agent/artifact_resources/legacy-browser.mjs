import {createRequire} from 'node:module';import fs from 'node:fs/promises';import assert from 'node:assert/strict';
const {chromium}=createRequire(new URL('../../../frontend/package.json',import.meta.url))('playwright');
const fixture=JSON.parse(await fs.readFile('/tmp/artifact-legacy-fixture.json','utf8'));
const browser=await chromium.launch({headless:true});
for(const [port,label] of [[3119,'before-main'],[3118,'after']]){
 const context=await browser.newContext({viewport:{width:1440,height:1000}});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${port}/users/sign-in`);
 await page.locator('input[type=email]').fill('artifact-author@example.com');await page.locator('input[type=password]').fill('Password123!');await page.locator('button[type=submit]').click();await page.waitForURL(u=>!u.pathname.includes('sign-in'));
 await page.goto(`http://127.0.0.1:${port}/reports/${fixture.report.id}`);
 const frame=page.frameLocator('iframe').first();await frame.getByText('Weekly progress',{exact:true}).waitFor({timeout:90000});
 await frame.getByLabel('Week').selectOption('Last week');await frame.getByText('63%',{exact:true}).waitFor();
 await page.screenshot({path:`media/pr/artifact-resources/legacy-${label}.png`});
 assert.deepEqual(errors,[]);await context.close();
}
await browser.close();console.log('PASS: unchanged main and new frontend both render the legacy artifact and respond to its existing control');

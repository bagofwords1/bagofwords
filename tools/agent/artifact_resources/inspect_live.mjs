import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
const {chromium}=createRequire(new URL('../../../frontend/package.json',import.meta.url))('playwright');
const rows=JSON.parse(await fs.readFile('/tmp/artifact-live-browser.json','utf8'));
const browser=await chromium.launch({headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1050}});
const page=await context.newPage();
await page.goto('http://127.0.0.1:3118/users/sign-in');
await page.locator('input[type=email]').fill('artifact-author@example.com');
await page.locator('input[type=password]').fill('Password123!');
await page.locator('button[type=submit]').click();
await page.waitForURL(u=>!u.pathname.includes('sign-in'));
await context.storageState({path:'/tmp/artifact-live-browser-state.json'});
for (const r of rows) {
 await page.goto('http://127.0.0.1:3118/reports/'+r.report_id);
 const iframe=page.frameLocator('iframe').first();
 try {
  await iframe.locator('main').waitFor({timeout:30000});
  console.log(r.model,await iframe.locator('body').innerText());
  await page.screenshot({path:'/tmp/'+r.model+'-live-initial.png'});
 }catch {console.log(r.model,'not rendered yet')}
}
await browser.close();

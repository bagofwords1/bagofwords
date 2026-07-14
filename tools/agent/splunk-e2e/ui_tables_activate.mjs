import { chromium } from '@playwright/test';
const OUT = '/home/user/bagofwords/media/pr/ai-ecstatic-sagan-84i4pc';
const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const ctx = await b.newContext({ viewport: { width: 1512, height: 950 }, storageState: '/home/user/bagofwords/media/pr/state.json' });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const shot = async (n) => { await page.screenshot({ path: `${OUT}/${n}.png` }); console.log('shot', n); };

await page.goto('http://localhost:3000/agents/661c8398-6069-4562-b1ca-5c0c7f0cd37f/tables', { waitUntil: 'commit' });
await page.getByText(/web::access_combined/).waitFor();
await page.getByText('Select all', { exact: true }).click();
await page.waitForTimeout(800);
await page.getByText('Save', { exact: true }).click();
await page.waitForTimeout(3000);
await shot('13-tables-activated');
const body = await page.locator('body').innerText();
const m = body.match(/\d\/\d active/);
console.log('active state:', m && m[0]);
await ctx.close(); await b.close();

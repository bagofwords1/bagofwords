// Reload the Splunk schema with 503 sourcetypes in the catalog; time it.
import { chromium } from '@playwright/test';
const OUT = '/home/user/bagofwords/media/pr/ai-ecstatic-sagan-84i4pc';
const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const ctx = await b.newContext({ viewport: { width: 1512, height: 950 }, storageState: '/home/user/bagofwords/media/pr/state.json' });
const page = await ctx.newPage();
page.setDefaultTimeout(60000);
const shot = async (n) => { await page.screenshot({ path: `${OUT}/${n}.png` }); console.log('shot', n); };

await page.goto('http://localhost:3000/agents/661c8398-6069-4562-b1ca-5c0c7f0cd37f/tables', { waitUntil: 'commit' });
await page.getByText(/web::access_combined/).first().waitFor();
const t0 = Date.now();
await page.getByText('Reload tables', { exact: true }).click();
console.log('clicked reload...');
// wait until the table count reflects the new catalog (Showing 1-N of 503)
let count = '';
for (let i = 0; i < 240; i++) {
  await page.waitForTimeout(5000);
  const body = await page.locator('body').innerText().catch(() => '');
  const m = body.match(/Showing [\d-]+ of (\d+)/);
  count = m ? m[1] : count;
  const t = Math.round((Date.now() - t0) / 1000);
  if (t % 30 < 6) console.log(`t=${t}s tables=${count}`);
  if (m && Number(m[1]) > 100) { console.log(`DONE in ~${t}s, tables=${m[1]}`); break; }
  // reload page state every 30s in case the list needs a refresh
  if (i > 0 && i % 6 === 0) { await page.reload({ waitUntil: 'commit' }); await page.waitForTimeout(3000); }
}
await page.waitForTimeout(1000);
await shot('14-tables-503');
const body = await page.locator('body').innerText();
console.log('active state:', (body.match(/\d+\/\d+ active/) || [])[0]);
await ctx.close(); await b.close();

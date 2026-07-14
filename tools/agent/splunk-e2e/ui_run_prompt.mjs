// Run a real completion from the home prompt box and wait for it via the API.
// Usage: node ui_run_prompt.mjs "<prompt>" <shot-prefix>
import { chromium } from '@playwright/test';
const OUT = '/home/user/bagofwords/media/pr/ai-ecstatic-sagan-84i4pc';
const PROMPT = process.argv[2];
const PREFIX = process.argv[3] || 'q';
const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const ctx = await b.newContext({ viewport: { width: 1512, height: 950 }, storageState: '/home/user/bagofwords/media/pr/state.json' });
const page = await ctx.newPage();
page.setDefaultTimeout(45000);
const shot = async (n) => { await page.screenshot({ path: `${OUT}/${n}.png` }); console.log('shot', n); };

await page.goto('http://localhost:3000/', { waitUntil: 'commit' });
const editor = page.locator('[contenteditable="true"]').first();
await editor.waitFor();
await page.waitForTimeout(2500);
await editor.click();
await page.keyboard.type(PROMPT, { delay: 5 });
await page.waitForTimeout(600);
await shot(`${PREFIX}-1-prompt`);
await page.keyboard.press('Enter');
await page.waitForURL(/reports\//, { timeout: 30000 });
const reportId = page.url().match(/reports\/([0-9a-f-]+)/)[1];
console.log('report:', reportId);
await page.waitForTimeout(4000);
await shot(`${PREFIX}-2-started`);

const cookies = await ctx.cookies();
const jwt = cookies.find(c => c.name === 'auth.token')?.value;
const AH = { 'Authorization': `Bearer ${jwt}` };
const orgs = await (await ctx.request.get('http://localhost:8000/api/organizations', { headers: AH })).json().catch(() => []);
const orgId = orgs[0]?.id;
console.log('orgId:', orgId);
const status = async () => {
  try {
    const r = await ctx.request.get(`http://localhost:8000/api/reports/${reportId}/completions`, { headers: { ...AH, 'X-Organization-Id': orgId } });
    if (!r.ok()) return `http-${r.status()}`;
    const d = await r.json();
    const sys = (d.completions || []).filter(c => c.role === 'system');
    const last = sys[sys.length - 1];
    return last ? last.status : 'none';
  } catch (e) { return `err:${String(e).slice(0, 60)}`; }
};

const started = Date.now();
let midShot = false;
let st = 'none';
while (Date.now() - started < 600000) {
  await page.waitForTimeout(6000);
  st = await status();
  const t = Math.round((Date.now() - started) / 1000);
  console.log(`t=${t}s status=${st}`);
  if (!midShot && t >= 18) { midShot = true; await shot(`${PREFIX}-3-in-progress`); }
  if (st === 'success' || st === 'error' || st === 'failed') break;
}
await page.waitForTimeout(2500);
await shot(`${PREFIX}-4-final`);
await page.screenshot({ path: `${OUT}/${PREFIX}-4-final-full.png`, fullPage: true });
console.log('final status:', st);
await ctx.close(); await b.close();

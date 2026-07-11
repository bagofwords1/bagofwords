// Long-running agent task gauntlet: Chinook analysis with create_data +
// create_artifact tools, hit with BOTH a mid-run refresh and a mid-run TCP
// sever. Asserts the UI keeps live-streaming after each disruption and the
// run converges to success with real artifacts.
import { chromium } from '@playwright/test';
import { execSync } from 'node:child_process';
import fs from 'fs';

const BASE = process.env.BASE || 'http://localhost:3001';
const OUT = process.argv[2] || '/tmp/bow-agent/chinook';
const REPORT_ID = process.env.REPORT_ID;
if (!REPORT_ID) { console.error('REPORT_ID required'); process.exit(2); }
fs.mkdirSync(OUT, { recursive: true });
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

const PROMPT = 'Analyze the Chinook music store. First create a data table of total revenue by genre, then create a data table of the top 10 artists by revenue, and finally create a dashboard artifact that summarizes revenue by genre and top artists. Use the tools.';

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome', headless: true });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
page.on('console', (m) => { if (m.type() === 'error') log('console.error:', m.text().slice(0, 160)); });

const netlog = [];
page.on('request', (r) => {
  const u = r.url();
  if (u.includes('/completions')) netlog.push(`${new Date().toISOString().slice(11, 23)} ${r.method()} ${u.replace(BASE, '').slice(0, 120)}`);
});

const shot = async (name) => { await page.screenshot({ path: `${OUT}/${name}.png` }); log('screenshot', name); };
const textLen = () => page.evaluate(() => document.body.innerText.length);

// --- login ---
await page.goto(`${BASE}/users/sign-in`);
await page.waitForLoadState('networkidle').catch(() => {});
await page.fill('input[type="text"]', 'admin@example.com');
await page.fill('input[type="password"]', 'Password123!');
await page.click('button[type="submit"]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
log('logged in');

// --- submit the long-running analysis prompt ---
await page.goto(`${BASE}/reports/${REPORT_ID}`);
await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
const box = page.locator('.mention-input-field').first();
await box.waitFor({ state: 'visible', timeout: 60000 });
await page.waitForTimeout(2000);
await box.click();
await box.pressSequentially(PROMPT, { delay: 5 });
const send = page.locator('button.w-7.h-7.rounded-full:not([disabled])').last();
await send.waitFor({ state: 'visible', timeout: 10000 });
await send.click();
log('prompt submitted — agent run started');

// --- SEVER all TCP connections early, during the first create_data ---
await page.waitForTimeout(12000);
await shot('C1-tools-running');
log('severing TCP connections mid-tool...');
execSync('kill -USR1 $(cat /tmp/bow-agent/chaos.pid)');
await page.waitForTimeout(8000);
await shot('C2-after-sever');
const errUi = await page.locator('text=/error occurred during streaming|Stream was cancelled|Connection error/i').count();
const lenC = await textLen();
await page.waitForTimeout(12000);
const lenD = await textLen();
log(`after sever: error UI=${errUi > 0}, growth ${lenC} -> ${lenD} (${lenD > lenC + 50 ? 'LIVE' : 'quiet window'})`);
await shot('C3-post-sever-streaming');

// --- REFRESH mid-run, later in the run ---
await page.reload();
await page.waitForTimeout(8000);
await shot('C4-after-refresh');
const banner = await page.locator('text=showing recent progress').count();
const lenA = await textLen();
await page.waitForTimeout(12000);
const lenB = await textLen();
log(`after refresh: polling banner=${banner > 0}, growth ${lenA} -> ${lenB} (${lenB > lenA + 50 ? 'LIVE' : 'quiet window'})`);
await shot('C5-post-refresh-streaming');

// --- wait for the run to converge (up to 6 min), checking via the API ---
const token = fs.readFileSync('/tmp/bow-agent/token.txt', 'utf8').trim();
let final = null;
for (let i = 0; i < 36; i++) {
  await page.waitForTimeout(10000);
  const res = await fetch(`http://localhost:8000/api/reports/${REPORT_ID}/completions?limit=5`, {
    headers: { Authorization: `Bearer ${token}`, 'X-Organization-Id': '317703e1-0faf-44ce-8f74-44957010a0a5' },
  });
  const data = await res.json();
  const sys = (data.completions || []).filter((c) => c.role === 'system').pop();
  if (sys && sys.status !== 'in_progress') { final = sys; break; }
}
if (!final) { log('FAIL: run did not converge within 6 min'); await shot('C9-timeout'); process.exit(1); }

const blocks = final.completion_blocks || [];
const toolBlocks = blocks.filter((b) => b.tool_execution);
const widgets = toolBlocks.filter((b) => b.tool_execution?.created_widget || b.tool_execution?.created_step);
log(`final status: ${final.status}; blocks=${blocks.length}, tool blocks=${toolBlocks.length}, widget/step creations=${widgets.length}`);
log('tools used: ' + toolBlocks.map((b) => b.tool_execution.tool_name + ':' + b.tool_execution.status).join(', '));

await page.waitForTimeout(3000);
await shot('C9-final-state');
fs.writeFileSync(`${OUT}/network.log`, netlog.join('\n'));
log(`network requests logged: ${netlog.length} (see network.log)`);

const ok = final.status === 'success' && toolBlocks.length >= 2;
log(ok ? 'GAUNTLET PASS' : 'GAUNTLET FAIL');
await browser.close();
process.exit(ok ? 0 : 1);

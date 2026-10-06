// Drive the real AddConnectionModal at an Oracle "server" that accepts the
// login and never answers (python silent listener on ORACLE_PORT), and record
// what "Check connection" shows over time. Before the fix it spins forever;
// after it, it fails within ORACLE_CONNECT_TIMEOUT_S with a message naming
// the way out. See docs/feedback-loops/oracle-10g-check-connection-hang.md.
// The same flow against a real Oracle (ORACLE_HOST/PORT/SERVICE/USER/PASSWORD)
// is the regression leg: it must still connect and finish schema discovery.
//   python3 tools/agent/oracle_silent_listener.py 15210 &
//   cp tools/agent/oracle_check_connection_hang.mjs frontend/.oracle_hang.mjs   # resolves @playwright/test
//   cd frontend && CHROME=/opt/pw-browsers/chromium-1194/chrome-linux/chrome OUT=/tmp/o node .oracle_hang.mjs
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.env.OUT || '/tmp/oracle-hang';
const HOST = process.env.ORACLE_HOST || '127.0.0.1';
const PORT = process.env.ORACLE_PORT || '15210';
const SERVICE = process.env.ORACLE_SERVICE || 'DWH';
const USER = process.env.ORACLE_USER || 'REPOS2000';
const PASSWORD = process.env.ORACLE_PASSWORD || 'not-a-real-password';
const SDU = process.env.ORACLE_SDU || '';  // blank = leave the field empty (default)
const WAIT_S = Number(process.env.WAIT_S || 75);
const BASE = 'http://localhost:3000';
mkdirSync(OUT, { recursive: true });

const b = await chromium.launch({ executablePath: process.env.CHROME || undefined });
const ctx = await b.newContext({ viewport: { width: 1440, height: 950 } });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const shot = async (n) => { await page.screenshot({ path: `${OUT}/${n}.png` }); console.log('shot', n); };

await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'commit' });
await page.locator('input[type=email], input[name=email]').first().fill('admin@example.com');
await page.locator('input[type=password]').first().fill('Password123!');
await Promise.all([
  page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2000);
for (let i = 0; i < 2; i++) {
  await page.goto(`${BASE}/agents/new?mode=new_connection`, { waitUntil: 'commit' });
  await page.waitForTimeout(2500);
  try { await page.getByText(/skip onboarding/i).click({ timeout: 3000 }); await page.waitForTimeout(1500); } catch { break; }
}

await page.getByPlaceholder(/search/i).first().fill('oracle');
await page.waitForTimeout(800);
await page.getByText(/^Oracle Database$/).first().click();
await page.waitForTimeout(1200);

await page.locator('input[placeholder*="Sales DB"]').fill(`Oracle check ${Date.now()}`);
await page.locator('#host').fill(HOST);
await page.locator('#port').fill(PORT);
await page.locator('#service_name').fill(SERVICE);
await page.locator('#user').fill(USER);
await page.locator('#password').fill(PASSWORD);
if (SDU) await page.locator('#sdu').fill(SDU);
await page.locator('#sdu').scrollIntoViewIfNeeded();
await shot('01-form-filled');

const started = Date.now();
await page.getByRole('button', { name: /^Connect$/ }).click();
let result = 'STILL_CONNECTING';
let message = '';
while ((Date.now() - started) / 1000 < WAIT_S) {
  await page.waitForTimeout(2000);
  const t = await page.locator('body').innerText();
  const m = t.match(/Oracle did not finish the login[^\n]*/);
  if (m) { result = 'FAILED_WITH_MESSAGE'; message = m[0]; break; }
  if (await page.getByRole('button', { name: /^Done$/ }).count()) { result = 'CONNECTED_AND_INDEXED'; break; }
  // Any other outcome: the Connect button is clickable again and an error is shown.
  if (await page.getByRole('button', { name: /^Connect$/ }).isEnabled().catch(() => false)) {
    await page.waitForTimeout(500);
    const t2 = await page.locator('body').innerText();
    const m2 = t2.match(/Oracle did not finish the login[^\n]*/);
    result = m2 ? 'FAILED_WITH_MESSAGE' : 'OTHER_ERROR';
    message = m2 ? m2[0] : (await page.locator('.text-red-600, .text-red-500').allInnerTexts()).join(' | ');
    break;
  }
}
const elapsed = Math.round((Date.now() - started) / 1000);
await shot(`02-after-${elapsed}s`);
console.log(`RESULT: ${result} after ${elapsed}s`);
if (message) console.log(`MESSAGE: ${message}`);
await ctx.close(); await b.close();

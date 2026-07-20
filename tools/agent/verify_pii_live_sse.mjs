// Verify live PII redaction of the typed prompt via SSE (no page refresh).
//
//   cd frontend
//   node ../tools/agent/verify_pii_live_sse.mjs
//
// Requires the running stack, an org LLM configured, and PII protection enabled
// with an email rule (replacement "[REDACTED_EMAIL]"). Creates a report bound to
// the first data source via the API, opens it, types a message containing an
// email, submits, and polls the user bubble as the stream runs — proving the
// bubble flips to the redacted value LIVE (mid-stream, URL unchanged, no reload).
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
const api = process.env.BOW_API || 'http://localhost:8000';
const email = process.env.BOW_EMAIL || 'admin@example.com';
const password = process.env.BOW_PASSWORD || 'Password123!';
const RAW = 'yochze@gmail.com';
const MSG = `show me my email ${RAW}`;
const OUT = process.env.BOW_OUT || '/home/user/bagofwords/scratch/pii-live';
mkdirSync(OUT, { recursive: true });

async function apiLogin() {
  const body = new URLSearchParams({ username: email, password });
  const r = await fetch(`${api}/api/auth/jwt/login`, {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body,
  });
  return (await r.json()).access_token;
}
const token = await apiLogin();
const orgs = await (await fetch(`${api}/api/organizations`, { headers: { Authorization: `Bearer ${token}` } })).json();
const org = orgs[0].id;
const H = { Authorization: `Bearer ${token}`, 'X-Organization-Id': org, 'Content-Type': 'application/json' };
const dss = await (await fetch(`${api}/api/data_sources`, { headers: H })).json();
const ds = dss[0];
console.log('data source:', ds.name, ds.id);
const rep = await (await fetch(`${api}/api/reports`, {
  method: 'POST', headers: H,
  body: JSON.stringify({ title: 'PII Live Verify', files: [], data_sources: [ds.id] }),
})).json();
const reportId = rep.id;
console.log('report:', reportId);

const execPath = process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const browser = await chromium.launch({ executablePath: execPath });
const context = await browser.newContext({ viewport: { width: 1440, height: 950 } });
const page = await context.newPage();
page.on('console', m => { if (m.type() === 'error') console.log('  [browser error]', m.text().slice(0, 160)); });

await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.fill('#email', email);
await page.fill('#password', password);
await Promise.all([
  page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.click('button[type=submit]'),
]);
await page.waitForTimeout(1500);
try {
  if (page.url().includes('/onboarding')) {
    await page.getByText('Skip onboarding', { exact: false }).click({ timeout: 4000 });
    await page.waitForTimeout(1500);
  }
} catch {}

await page.goto(`${origin}/reports/${reportId}`, { waitUntil: 'commit' }).catch(() => {});
const input = page.locator('.mention-input-field').first();
await input.waitFor({ state: 'visible', timeout: 90000 });
await page.waitForTimeout(1500);

await input.click();
await page.keyboard.type(MSG, { delay: 10 });
await page.waitForTimeout(400);
await page.keyboard.press('Escape');   // dismiss mention autocomplete
await page.waitForTimeout(300);
await page.screenshot({ path: `${OUT}/00-typed.png` });
const urlBefore = page.url();
const clicked = await page.evaluate(() => {
  const btns = Array.from(document.querySelectorAll('button'));
  const b = btns.find(x => !x.disabled && x.className.includes('rounded-full') &&
    x.className.includes('w-7') && x.querySelector('.iconify, svg, span'));
  if (b) { b.click(); return true; }
  return false;
}).catch(() => false);
if (!clicked) await page.keyboard.press('Enter');
console.log('submit clicked via button:', clicked);

const samples = [];
let firstRedactedShot = false;
const start = Date.now();
while (Date.now() - start < 40000) {
  const txt = await page.evaluate(() => {
    document.querySelectorAll('.mention-input-field').forEach(e => e.setAttribute('data-input', '1'));
    return Array.from(document.querySelectorAll('body *')).filter(el =>
      !el.closest('[data-input="1"]') &&
      el.childElementCount === 0 && el.textContent &&
      el.textContent.includes('show me my email')).map(n => n.textContent.trim());
  }).catch(() => []);
  const joined = txt.join(' || ');
  const hasRaw = joined.includes(RAW);
  const hasRedacted = joined.includes('[REDACTED_EMAIL]');
  samples.push({ t: Date.now() - start, urlChanged: page.url() !== urlBefore, hasRaw, hasRedacted, n: txt.length, snip: joined.slice(0, 70) });
  if (hasRedacted && !firstRedactedShot) {
    firstRedactedShot = true;
    await page.screenshot({ path: `${OUT}/01-bubble-redacted-live.png` });
  }
  if (hasRedacted && (Date.now() - start > 5000)) break;
  await page.waitForTimeout(250);
}

await page.waitForTimeout(500);
await page.screenshot({ path: `${OUT}/02-final.png` });
const finalTxt = await page.evaluate(() => document.body.innerText).catch(() => '');

console.log('\n=== PII LIVE SSE VERIFICATION ===');
console.log('url before submit:', urlBefore);
console.log('url after        :', page.url(), '(unchanged =', page.url() === urlBefore, ')');
console.log('bubble flipped to [REDACTED_EMAIL] live:', firstRedactedShot);
console.log('final page body has [REDACTED_EMAIL]:', finalTxt.includes('[REDACTED_EMAIL]'));
console.log('final page body still shows raw email:', finalTxt.includes(RAW));
for (const s of samples.slice(0, 6)) console.log('  ', JSON.stringify(s));

await browser.close();
const pass = firstRedactedShot && finalTxt.includes('[REDACTED_EMAIL]') && !finalTxt.includes(RAW);
console.log('RESULT:', pass ? 'PASS' : 'FAIL');
process.exit(pass ? 0 : 1);

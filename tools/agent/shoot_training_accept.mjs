// Live UI evidence for the training-mode multi-instruction accept fix.
//
// Drives a real training session (Haiku), asks for several instructions in one
// go, then accepts each create_instruction card through the UI. Before the fix
// the 2nd+ accept errored ("Build is already published"); after the fix every
// accept succeeds and each card shows "Accepted".
//
//   cd frontend && node ../tools/agent/shoot_training_accept.mjs
import { chromium } from '/opt/node22/lib/node_modules/playwright/index.mjs';
import fs from 'node:fs';

const CHROME = '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';

const BASE = process.env.BASE || 'http://localhost:3000';
const API = process.env.API || 'http://localhost:8000';
const OUT = process.env.OUT || '/tmp/bow-agent/training-accept';
const RID = fs.readFileSync('/tmp/bow-agent/report_id.txt', 'utf8').trim();
const TOKEN = fs.readFileSync('/tmp/bow-agent/token.txt', 'utf8').trim();
const ORG = process.env.ORG || '76cd1eff-24e1-44f2-8c6e-cde157736ebf';
fs.mkdirSync(OUT, { recursive: true });
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

const PROMPT =
  'Please record three separate instructions now using the create_instruction tool, one tool call each, ' +
  'high confidence, no need to explore first:\n' +
  '1) In the invoices table the Total column is already in dollars, do not divide by 100.\n' +
  '2) When counting customers, treat each customer_id as unique; never dedupe by email.\n' +
  '3) The Music Store dataset is the Chinook sample database (artists, albums, tracks, invoices).\n' +
  'Create all three as separate global instructions.';

const shot = async (page, name) => { await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true }); log('shot', name); };

async function completionsDone() {
  const res = await fetch(`${API}/api/reports/${RID}/completions?limit=20`, {
    headers: { Authorization: `Bearer ${TOKEN}`, 'X-Organization-Id': ORG },
  });
  const data = await res.json();
  const comps = data.completions || data || [];
  const sys = comps.filter((c) => c.role === 'system');
  const last = sys[sys.length - 1];
  let creates = 0;
  for (const c of comps) {
    for (const b of (c.completion_blocks || [])) {
      const te = b.tool_execution;
      if (te && te.tool_name === 'create_instruction' && te.status === 'success') creates += 1;
    }
  }
  return { done: last && last.status !== 'in_progress', status: last && last.status, creates };
}

const browser = await chromium.launch({ headless: true, executablePath: CHROME });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 1200 } });
const page = await ctx.newPage();
page.on('console', (m) => { if (m.type() === 'error') log('console.error:', m.text().slice(0, 140)); });

// --- login ---
await page.goto(`${BASE}/users/sign-in`);
await page.waitForLoadState('networkidle').catch(() => {});
await page.fill('input[type="text"]', 'admin@example.com');
await page.fill('input[type="password"]', 'Password123!');
await page.click('button[type="submit"]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
log('logged in');

// --- open the training report + submit the prompt ---
await page.goto(`${BASE}/reports/${RID}`);
await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
const box = page.locator('.mention-input-field').first();
await box.waitFor({ state: 'visible', timeout: 60000 });
await page.waitForTimeout(1500);
await shot(page, '00-training-report-open');

let final = null;
if (process.env.SKIP_SUBMIT !== '1') {
  await box.click();
  await box.pressSequentially(PROMPT, { delay: 3 });
  const send = page.locator('button.w-7.h-7.rounded-full:not([disabled])').last();
  await send.waitFor({ state: 'visible', timeout: 10000 });
  await send.click();
  log('prompt submitted');
  for (let i = 0; i < 60; i++) {
    await page.waitForTimeout(5000);
    try {
      const s = await completionsDone();
      if (i % 3 === 0) log(`poll ${i}: status=${s.status} creates=${s.creates}`);
      if (s.done && s.creates >= 2) { final = s; break; }
      if (s.done && s.creates < 2) { final = s; log('converged but < 2 creates'); break; }
    } catch (e) { log('poll err', String(e).slice(0, 80)); }
  }
} else {
  final = await completionsDone();
  log('using existing completions (SKIP_SUBMIT)');
}
log('final:', JSON.stringify(final));

// Reload so all tool cards render settled, then screenshot the pending state.
await page.reload();
await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
await page.waitForTimeout(3000);

// Accept buttons live inside the create_instruction cards.
const acceptButtons = page.getByRole('button', { name: /^Accept$/ });
const nAccept = await acceptButtons.count();
log('accept buttons found:', nAccept);
await shot(page, '01-before-accept-all-pending');

// Click each Accept in turn, screenshotting after each so the flow is visible.
let clicked = 0;
for (let i = 0; i < 8; i++) {
  const btns = page.getByRole('button', { name: /^Accept$/ });
  const c = await btns.count();
  if (c === 0) break;
  await btns.first().click().catch((e) => log('click err', String(e).slice(0, 80)));
  clicked += 1;
  await page.waitForTimeout(2500);
  await shot(page, `02-after-accept-${clicked}`);
  log(`accepted ${clicked}; remaining accept buttons ~${(await page.getByRole('button', { name: /^Accept$/ }).count())}`);
}

// Final state: count "Accepted" markers and any failure toasts.
await page.waitForTimeout(1500);
const acceptedCount = await page.getByText(/^Accepted$/).count();
const failToast = await page.getByText(/Failed to accept/i).count();
await shot(page, '03-final-all-accepted');
log(`RESULT clicked=${clicked} acceptedMarkers=${acceptedCount} failToasts=${failToast}`);

fs.writeFileSync(`${OUT}/result.json`, JSON.stringify({ final, clicked, acceptedCount, failToast, nAccept }, null, 2));
await browser.close();
const ok = clicked >= 2 && failToast === 0 && acceptedCount >= clicked;
log(ok ? 'UI EVIDENCE PASS' : 'UI EVIDENCE INCONCLUSIVE');
process.exit(ok ? 0 : 1);

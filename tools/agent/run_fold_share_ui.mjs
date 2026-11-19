// Capture the run-activity fold on the shared-link view (/c/:token) of a
// report that already has a finished run (e.g. one made by run_fold_ui.mjs).
// Turns conversation sharing on through the API, then opens the link in a
// signed-out context: collapsed, expanded, and one step's details.
//
//   cd frontend && REPORT_ID=<id> node ../tools/agent/run_fold_share_ui.mjs <out-dir> [label]
//
// Env: BOW_ORIGIN (default http://localhost:3000), BOW_EMAIL, BOW_PASSWORD,
//      PLAIN=1 (screenshot only, for a ref without the fold).
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';

const { chromium } = createRequire(import.meta.url)('playwright');
const [OUT = 'run-fold-shots', LABEL = 'share'] = process.argv.slice(2);
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const REPORT_ID = process.env.REPORT_ID;
if (!REPORT_ID) { console.error('REPORT_ID is required'); process.exit(2); }
mkdirSync(OUT, { recursive: true });
const log = (...a) => console.log('[run-fold-share]', ...a);

const browser = await chromium.launch();
const ctxOpts = { viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 };

// Signed in: enable the share link.
const owner = await browser.newContext(ctxOpts);
const op = await owner.newPage();
op.setDefaultNavigationTimeout(240000);
await op.goto(`${BASE}/users/sign-in`);
await op.fill('#email', process.env.BOW_EMAIL || 'admin@example.com');
await op.fill('#password', process.env.BOW_PASSWORD || 'Password123!');
await op.click('button[type="submit"]');
await op.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
const token = await op.evaluate(async (id) => {
  const tk = decodeURIComponent((document.cookie.match(/(?:^|; )auth\.token=([^;]*)/) || [])[1] || '');
  const auth = tk.startsWith('Bearer') ? tk : `Bearer ${tk}`;
  const orgs = await (await fetch('/api/organizations', { headers: { Authorization: auth } })).json();
  const H = { Authorization: auth, 'X-Organization-Id': orgs[0].id };
  let r = await (await fetch(`/api/reports/${id}/conversation-share`, { method: 'POST', headers: H })).json();
  if (!r.enabled) r = await (await fetch(`/api/reports/${id}/conversation-share`, { method: 'POST', headers: H })).json();
  return r.token || r.share_token || r.conversation_share_token;
}, REPORT_ID);
await owner.close();
if (!token) { console.error('no share token returned'); process.exit(1); }

// Signed out: what a recipient of the link sees.
const guest = await browser.newContext(ctxOpts);
const page = await guest.newPage();
page.setDefaultNavigationTimeout(240000);
page.on('pageerror', (e) => log('pageerror', e.message));
await page.goto(`${BASE}/c/${token}`);
if (process.env.PLAIN) {
  await page.waitForTimeout(8000);
  await page.screenshot({ path: `${OUT}/${LABEL}-plain.png` });
  await browser.close();
  process.exit(0);
}
const toggle = page.locator('[data-testid="run-fold-toggle"]').first();
await toggle.waitFor({ timeout: 120000 });
await page.waitForTimeout(2500);
log('toggle (collapsed):', JSON.stringify(await toggle.innerText()), 'aria-expanded=', await toggle.getAttribute('aria-expanded'));
log('final messages:', await page.locator('[data-run-final]').count(), 'deliverables:', await page.locator('[data-testid="run-deliverables"] > *').count());
await page.screenshot({ path: `${OUT}/${LABEL}-collapsed.png` });
await toggle.focus();
await page.keyboard.press('Enter');
await page.waitForTimeout(800);
log('toggle (expanded):', JSON.stringify(await toggle.innerText()), 'aria-expanded=', await toggle.getAttribute('aria-expanded'));
const steps = page.locator('[data-testid="run-step-toggle"]');
log('steps:', JSON.stringify(await steps.allInnerTexts()));
await page.screenshot({ path: `${OUT}/${LABEL}-expanded.png` });
if (await steps.count()) {
  await steps.first().click();
  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${OUT}/${LABEL}-step-details.png` });
}
// Citations on a shared link: hover previews, and a click pins the preview
// (there is no panel to open the result in).
await toggle.focus();
await page.keyboard.press('Enter');
await page.waitForTimeout(500);
const chips = page.locator('[data-testid="citation-chip"]');
log('chips:', JSON.stringify(await chips.evaluateAll((els) => els.map((e) => [e.textContent, e.getAttribute('aria-label')]))));
log('sources:', JSON.stringify(await page.locator('[data-testid="run-source"]').allInnerTexts()));
log('raw markers on page:', (await page.evaluate(() => document.body.innerText)).includes('{{cite'));
for (let i = 0; i < await chips.count(); i++) {
  await chips.nth(i).hover();
  await page.waitForTimeout(1500);
  const pv = page.locator('[data-testid="citation-preview"]');
  log('hover chip', i, '->', JSON.stringify((await pv.innerText().catch(() => '')).replace(/\s+/g, ' ').slice(0, 90)));
  if (i === (await chips.count()) - 1) await page.screenshot({ path: `${OUT}/${LABEL}-hover.png` });
  await page.mouse.move(4, 4);
  await page.waitForTimeout(450);
}
if (await chips.count()) {
  await chips.first().click();
  await page.waitForTimeout(600);
  await page.mouse.move(4, 4);
  await page.waitForTimeout(600);
  log('click pins preview:', await page.locator('[data-testid="citation-preview"]').count(), 'url unchanged:', page.url().includes('/c/'));
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  log('Escape dismisses:', await page.locator('[data-testid="citation-preview"]').count() === 0);
}
await browser.close();
log('done');

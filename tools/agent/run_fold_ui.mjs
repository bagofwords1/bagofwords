// Drive one scripted run on /reports/:id and capture the run-activity fold:
// live (chronological), settled (collapsed), expanded, a step's details,
// citation chips with their hover previews, and a reopened report. Pair with run_fold_stub_llm.py (see its header).
//
//   cd frontend && node ../tools/agent/run_fold_ui.mjs <out-dir> [label]
//
// Env: BOW_ORIGIN (default http://localhost:3000), BOW_EMAIL, BOW_PASSWORD,
//      BOW_LOCALE (sets localStorage bow.locale, e.g. he), REPORT_ID (skip the
//      run and capture an existing report), DARK=1, VIDEO=1 (record the
//      session to <out-dir>/video/*.webm).
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';

const { chromium } = createRequire(import.meta.url)('playwright');
const [OUT = 'run-fold-shots', LABEL = 'after'] = process.argv.slice(2);
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const PROMPT = 'What is our revenue by country?';
mkdirSync(OUT, { recursive: true });
const log = (...a) => console.log('[run-fold]', ...a);
const shot = (page, name) => page.screenshot({ path: `${OUT}/${LABEL}-${name}.png` });

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1280, height: 900 },
  deviceScaleFactor: 2,
  colorScheme: process.env.DARK ? 'dark' : 'light',
  ...(process.env.VIDEO ? { recordVideo: { dir: `${OUT}/video`, size: { width: 1280, height: 900 } } } : {}),
});
if (process.env.BOW_LOCALE) {
  await context.addInitScript((l) => localStorage.setItem('bow.locale', l), process.env.BOW_LOCALE);
}
const page = await context.newPage();
page.setDefaultNavigationTimeout(240000);
page.on('pageerror', (e) => log('pageerror', e.message));

await page.goto(`${BASE}/users/sign-in`);
await page.fill('#email', process.env.BOW_EMAIL || 'admin@example.com');
await page.fill('#password', process.env.BOW_PASSWORD || 'Password123!');
await page.click('button[type="submit"]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
if (page.url().includes('/onboarding')) {
  const skip = page.getByRole('button', { name: 'Skip onboarding' });
  if (await skip.isVisible({ timeout: 10000 }).catch(() => false)) await skip.click();
}

let reportId = process.env.REPORT_ID;
if (!reportId) {
  reportId = await page.evaluate(async () => {
    const token = decodeURIComponent((document.cookie.match(/(?:^|; )auth\.token=([^;]*)/) || [])[1] || '');
    const auth = token.startsWith('Bearer') ? token : `Bearer ${token}`;
    const orgs = await (await fetch('/api/organizations', { headers: { Authorization: auth } })).json();
    const H = { Authorization: auth, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' };
    const ds = await (await fetch('/api/data_sources', { headers: H })).json();
    const r = await fetch('/api/reports', { method: 'POST', headers: H, body: JSON.stringify({ title: 'Revenue by country', data_sources: [ds[0].id] }) });
    return (await r.json()).id;
  });
  log('report', reportId);
  await page.goto(`${BASE}/reports/${reportId}`);
  const box = page.locator('.mention-input-field').first();
  await box.waitFor({ state: 'visible', timeout: 120000 });
  await page.waitForTimeout(1500);
  await box.click();
  await box.fill(PROMPT);
  const send = page.locator('button.w-7.h-7.rounded-full:not([disabled])').last();
  await send.waitFor({ state: 'visible', timeout: 15000 });
  await send.click();
  log('sent');

  // Live: the chain renders chronologically while the run works.
  const seen = new Set();
  const t0 = Date.now();
  while (Date.now() - t0 < 180000) {
    const text = await page.evaluate(() => document.body.innerText);
    const folded = await page.locator('[data-testid="run-fold-toggle"]').count();
    for (const [name, needle] of [['live-1-inspecting', 'checking the Invoice table'], ['live-2-building', 'Charting revenue by country']]) {
      if (!seen.has(name) && text.includes(needle) && !folded) {
        seen.add(name);
        await page.waitForTimeout(700);
        await shot(page, name);
        log('shot', name);
      }
    }
    if (await page.locator('[data-completion-footer]').count()) break;
    await page.waitForTimeout(150);
  }
  log('run finished in', Date.now() - t0, 'ms');
} else {
  await page.goto(`${BASE}/reports/${reportId}`);
}

await page.locator('[data-completion-footer]').first().waitFor({ timeout: 120000 });
await page.waitForTimeout(2500);
await shot(page, 'settled');

const toggle = page.locator('[data-testid="run-fold-toggle"]').first();
if (await toggle.count()) {
  log('toggle (collapsed):', JSON.stringify(await toggle.innerText()), 'aria-expanded=', await toggle.getAttribute('aria-expanded'));
  // Keyboard: focus + Enter opens the steps.
  await toggle.focus();
  await page.keyboard.press('Enter');
  await page.waitForTimeout(800);
  log('toggle (expanded):', JSON.stringify(await toggle.innerText()), 'aria-expanded=', await toggle.getAttribute('aria-expanded'));
  await shot(page, 'expanded');
  const steps = page.locator('[data-testid="run-step-toggle"]');
  log('steps:', JSON.stringify(await steps.allInnerTexts()));
  if (await steps.count()) {
    await steps.first().click();
    await page.waitForTimeout(1200);
    await shot(page, 'step-details');
    await steps.first().click();
  }
  await page.keyboard.press('Escape');
  await toggle.focus();
  await page.keyboard.press('Space');
  await page.waitForTimeout(500);

  // Citations: chips in the answer, the sources row under it, hover preview
  // per kind of result, and keyboard open into the side panel.
  // The latest answer: the page keeps itself scrolled to it.
  const chips = page.locator('[data-message-role="system"]').last().locator('[data-testid="citation-chip"]');
  log('chips:', JSON.stringify(await chips.evaluateAll((els) => els.map((e) => [e.textContent, e.getAttribute('aria-label')]))));
  log('sources:', JSON.stringify(await page.locator('[data-testid="run-source"]').allInnerTexts()));
  log('inline charts:', await page.locator('.run-inline').count(), 'appended cards:', await page.locator('[data-testid="run-deliverables"]').count());
  log('raw markers on page:', (await page.evaluate(() => document.body.innerText)).includes('{{cite'));
  const seenKinds = new Set();
  for (let i = 0; i < await chips.count(); i++) {
    await chips.nth(i).scrollIntoViewIfNeeded();
    await chips.nth(i).hover();
    await page.waitForTimeout(1600);
    const pv = page.locator('[data-testid="citation-preview"]');
    const kind = (await pv.locator('[data-testid="citation-preview-chart"], [data-testid="citation-preview-value"], [data-testid="citation-preview-table"]').first().getAttribute('data-testid').catch(() => null)) || 'none';
    log('hover chip', i, '->', kind, JSON.stringify((await pv.innerText().catch(() => '')).replace(/\s+/g, ' ').slice(0, 90)));
    if (!seenKinds.has(kind)) {
      seenKinds.add(kind);
      await shot(page, `hover-${kind.replace('citation-preview-', '')}`);
      // The preview's own tabs: Chart (charts only), Data, Code (with view_code).
      const tabs = pv.locator('[role="tab"]');
      log('  tabs:', JSON.stringify(await tabs.allInnerTexts()));
      for (const name of ['data', 'code']) {
        const tab = pv.locator(`[data-testid="citation-preview-tab-${name}"]`);
        if (!(await tab.count())) continue;
        // Move like a hand would: from the chip onto the preview, then press.
        const box = await tab.boundingBox();
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 6 });
        await page.mouse.down();
        await page.mouse.up();
        await page.waitForTimeout(500);
        log(`  ${name} tab ->`, JSON.stringify((await pv.innerText()).replace(/\s+/g, ' ').slice(0, 110)));
        if (kind === 'citation-preview-chart') await shot(page, `hover-chart-${name}`);
      }
    }
    await page.mouse.move(4, 4);
    await page.waitForTimeout(450);
  }
  if (await chips.count()) {
    await chips.last().focus();
    await page.waitForTimeout(400);
    log('focus shows preview:', await page.locator('[data-testid="citation-preview"]').count());
    await page.keyboard.press('Enter');
    await page.waitForTimeout(2500);
    log('Enter opens panel:', await page.locator('[data-testid="data-panel"]').count());
    await shot(page, 'cited-panel');
  }
  await page.waitForTimeout(500);
  log('toggle (re-collapsed): aria-expanded=', await toggle.getAttribute('aria-expanded'));

  // Reopen: a completed report comes back collapsed with the conclusion visible.
  await page.reload();
  await page.locator('[data-completion-footer]').first().waitFor({ timeout: 120000 });
  await page.waitForTimeout(2500);
  log('reopened: aria-expanded=', await page.locator('[data-testid="run-fold-toggle"]').first().getAttribute('aria-expanded'));
  await shot(page, 'reopened');
}

const dump = await page.evaluate(async (id) => {
  const token = decodeURIComponent((document.cookie.match(/(?:^|; )auth\.token=([^;]*)/) || [])[1] || '');
  const auth = token.startsWith('Bearer') ? token : `Bearer ${token}`;
  const orgs = await (await fetch('/api/organizations', { headers: { Authorization: auth } })).json();
  const H = { Authorization: auth, 'X-Organization-Id': orgs[0].id };
  return await (await fetch(`/api/reports/${id}/completions`, { headers: H })).json();
}, reportId);
writeFileSync(`${OUT}/${LABEL}-completions.json`, JSON.stringify(dump, null, 1));
writeFileSync(`${OUT}/${LABEL}-report-id.txt`, reportId);
log('done', reportId);
await context.close();
await browser.close();

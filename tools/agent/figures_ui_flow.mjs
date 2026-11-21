// Figures in answers ({{show:<visualization id>}}), driven like a user on the
// real stack with a real LLM: sign in, ask in the composer, wait for the run,
// then check and capture the figures.
//
//   cd frontend && node ../tools/agent/figures_ui_flow.mjs <out-dir>
//
// Env: BOW_ORIGIN (default http://localhost:3000), BOW_EMAIL, BOW_PASSWORD,
//      VIDEO=1 (record the first run), ONLY=<prompt index>.
// Prints one JSON line per prompt: figures found (kind, rows), raw markers
// leaked, single-number figures, console errors.
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';

const { chromium, devices } = createRequire(import.meta.url)('playwright');
const OUT = process.argv[2] || 'figures-shots';
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
mkdirSync(OUT, { recursive: true });
const log = (...a) => console.log('[figures]', ...a);

const PROMPTS = [
  { name: 'revenue', text: 'Where does our revenue come from? Chart revenue by billing country, and show me our top 10 customers by total spend.' },
  { name: 'trend', text: 'How has monthly revenue trended over the years? Show it as a line chart.' },
  { name: 'single', text: 'What is our total revenue across all invoices?' },
  // A right-to-left answer: the figure mirrors with it.
  { name: 'hebrew', text: 'מאיפה מגיעות ההכנסות שלנו? הצג תרשים של ההכנסות לפי מדינה.', locale: 'he' },
];

const browser = await chromium.launch();

async function signIn(context) {
  const page = await context.newPage();
  await page.goto(`${BASE}/users/sign-in`);
  await page.fill('#email', process.env.BOW_EMAIL || 'admin@example.com');
  await page.fill('#password', process.env.BOW_PASSWORD || 'Password123!');
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
  return page;
}

async function ask(page, text) {
  // A new report from the home composer, as a user would start one.
  await page.goto(`${BASE}/`);
  const box = page.locator('.mention-input-field').first();
  await box.waitFor({ state: 'visible', timeout: 120000 });
  await page.waitForTimeout(1200);
  await box.click();
  await box.fill(text);
  await page.keyboard.press('Enter');
  await page.waitForURL(/\/reports\//, { timeout: 60000 });
  await page.locator('[data-testid="stop-button"]').waitFor({ timeout: 60000 }).catch(() => {});
}

async function waitDone(page, onLive) {
  const t0 = Date.now();
  while (Date.now() - t0 < 300000) {
    if (onLive) await onLive();
    const live = await page.locator('[data-testid="stop-button"]').count();
    if (!live && await page.locator('[data-completion-footer]').count()) break;
    await page.waitForTimeout(250);
  }
  await page.waitForTimeout(2500);
  return Date.now() - t0;
}

async function audit(page) {
  return page.evaluate(() => {
    const last = [...document.querySelectorAll('[data-message-role="system"]')].pop();
    const figs = [...(last?.querySelectorAll('[data-testid="chat-figure"]') || [])].map((f) => ({
      kind: f.getAttribute('data-kind'),
      state: f.getAttribute('data-state'),
      caption: f.querySelector('figcaption .pf-cap-text')?.textContent?.replace(/\s+/g, ' ').trim(),
      highlight: f.getAttribute('data-highlight') || '',
      drawn: !!f.querySelector('svg path, table tbody tr'),
    }));
    const text = last?.innerText || '';
    return {
      figures: figs,
      rawMarkers: /\{\{\s*(show|cite)\s*:/.test(text),
      chips: last?.querySelectorAll('a.citation-chip').length || 0,
    };
  });
}

// CUSTOM_PROMPT=<text> runs only that question (named 'custom').
if (process.env.CUSTOM_PROMPT) PROMPTS.splice(0, PROMPTS.length, { name: 'custom', text: process.env.CUSTOM_PROMPT });
const results = [];
const only = process.env.ONLY ? Number(process.env.ONLY) : null;
for (const [i, p] of PROMPTS.entries()) {
  if (only !== null && only !== i) continue;
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2,
    ...(process.env.VIDEO && i === 0 ? { recordVideo: { dir: `${OUT}/video`, size: { width: 1280, height: 900 } } } : {}),
  });
  if (p.locale) await context.addInitScript((l) => localStorage.setItem('bow.locale', l), p.locale);
  const page = await signIn(context);
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => { if (m.type() === 'error' && !/favicon|DevTools|websocket|ws:/i.test(m.text())) errors.push(m.text().slice(0, 200)); });

  await ask(page, p.text);
  log(p.name, 'asked', page.url());
  // Live: capture the first moment a figure is on screen (entrance/loading).
  let liveShot = false;
  const ms = await waitDone(page, async () => {
    if (liveShot) return;
    if (await page.locator('[data-testid="chat-figure"]').count()) {
      liveShot = true;
      // The renderer may re-create the block while text streams after it.
      await page.locator('[data-testid="chat-figure"]').first().scrollIntoViewIfNeeded({ timeout: 3000 }).catch(() => {});
      await page.screenshot({ path: `${OUT}/${p.name}-live.png` }).catch(() => {});
    }
  });
  const a = await audit(page);
  const row = { prompt: p.name, ms, ...a, errors };
  results.push(row);
  log(JSON.stringify(row));

  // Settled answer, top to bottom.
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(400);
  const answer = page.locator('[data-message-role="system"]').last();
  await answer.screenshot({ path: `${OUT}/${p.name}-answer.png` }).catch(() => {});
  const figs = page.locator('[data-testid="chat-figure"]');
  for (let k = 0; k < await figs.count(); k++) {
    await figs.nth(k).scrollIntoViewIfNeeded();
    await page.waitForTimeout(500);
    await figs.nth(k).screenshot({ path: `${OUT}/${p.name}-figure-${k + 1}.png` });
  }
  writeFileSync(`${OUT}/${p.name}-report-url.txt`, page.url());

  if (p.name === 'revenue' && await figs.count()) {
    const fig = figs.first();
    await fig.scrollIntoViewIfNeeded();
    // Hover: the text controls appear; a bar shows its tooltip.
    await fig.hover();
    const body = fig.locator('.pf-body');
    const bb = await body.boundingBox();
    if (bb) { await page.mouse.move(bb.x + bb.width * 0.3, bb.y + 12); await page.waitForTimeout(600); }
    await fig.screenshot({ path: `${OUT}/${p.name}-hover.png` });
    // Chart -> table -> chart.
    await fig.locator('[data-testid="chat-figure-as-table"]').click();
    await page.waitForTimeout(500);
    await fig.screenshot({ path: `${OUT}/${p.name}-as-table.png` });
    await fig.locator('[data-testid="chat-figure-as-table"]').click();
    await page.waitForTimeout(400);
    // The ••• menu.
    const actions = {};
    const menu = async () => { await fig.hover(); await fig.locator('[data-testid="chat-figure-menu"]').click(); await page.waitForTimeout(300); };
    await menu();
    await page.screenshot({ path: `${OUT}/${p.name}-menu.png` });
    actions.items = (await page.locator('[role="menuitem"]').allInnerTexts()).map((x) => x.trim());
    await page.keyboard.press('Escape');
    for (const [label, ext] of [['Download CSV', 'csv'], ['Download Excel', 'xlsx'], ['Download image', 'png']]) {
      await menu();
      const [dl] = await Promise.all([page.waitForEvent('download', { timeout: 20000 }).catch(() => null), page.getByRole('menuitem', { name: label }).click()]);
      if (dl) {
        const path = `${OUT}/${p.name}-download.${ext}`;
        await dl.saveAs(path);
        actions[ext] = (await import('node:fs')).statSync(path).size;
      } else actions[ext] = 0;
      await page.waitForTimeout(400);
    }
    await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: BASE });
    await menu();
    await page.getByRole('menuitem', { name: 'Copy data' }).click();
    await page.waitForTimeout(500);
    actions.copiedLines = (await page.evaluate(() => navigator.clipboard.readText()).catch(() => '')).split('\n').length;
    await menu();
    await page.getByRole('menuitem', { name: 'Save as query' }).click();
    await page.waitForTimeout(1200);
    actions.saveDialog = await page.locator('[role="dialog"]').count() > 0;
    await page.screenshot({ path: `${OUT}/${p.name}-save-dialog.png` });
    await page.keyboard.press('Escape');
    await page.waitForTimeout(600);
    await menu();
    await page.getByRole('menuitem', { name: 'Add to dashboard' }).click();
    await page.waitForTimeout(3000);
    await page.screenshot({ path: `${OUT}/${p.name}-added-to-dashboard.png` });
    await page.mouse.move(5, 5); await page.waitForTimeout(300);
    await menu();
    actions.afterAdd = (await page.locator('[role="menuitem"]').allInnerTexts()).map((x) => x.trim()).filter((x) => /dashboard/i.test(x));
    await page.keyboard.press('Escape');
    // Open in panel.
    await fig.hover();
    await fig.locator('[data-testid="chat-figure-open"]').click();
    await page.waitForTimeout(2500);
    actions.panelOpened = await page.locator('[data-testid="data-panel"]').count() > 0;
    row.actions = actions;
    log('actions:', JSON.stringify(actions));
    // Reload: same figures, same numbers, no second entrance.
    await page.reload();
    await page.locator('[data-completion-footer]').first().waitFor({ timeout: 120000 });
    await page.waitForTimeout(3000);
    row.afterReload = (await audit(page)).figures.map((f) => `${f.kind}|${f.caption?.slice(0, 12)}`);
    log('after reload:', JSON.stringify(row.afterReload));
  }
  await context.close();
}

// Same report in dark, Hebrew and on a phone.
const first = !process.env.CUSTOM_PROMPT && results.find((r) => r.prompt === 'revenue');
if (first) {
  const url = (await import('node:fs')).readFileSync(`${OUT}/revenue-report-url.txt`, 'utf8');
  for (const [name, opts, init] of [
    ['dark', { viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2, colorScheme: 'dark' }, (c) => c.addInitScript(() => localStorage.setItem('nuxt-color-mode', 'dark'))],
    ['he', { viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 }, (c) => c.addInitScript(() => localStorage.setItem('bow.locale', 'he'))],
    ['mobile', { ...devices['iPhone 14'] }, null],
  ]) {
    const context = await browser.newContext(opts);
    if (init) await init(context);
    const page = await signIn(context);
    await page.goto(url);
    await page.locator('[data-completion-footer]').first().waitFor({ timeout: 120000 });
    await page.waitForTimeout(3500);
    const fig = page.locator('[data-testid="chat-figure"]').first();
    if (await fig.count()) {
      await fig.scrollIntoViewIfNeeded();
      await page.evaluate(() => window.scrollBy(0, -140));
      await page.waitForTimeout(500);
    }
    await page.screenshot({ path: `${OUT}/variant-${name}.png` });
    await context.close();
  }
}
writeFileSync(`${OUT}/results.json`, JSON.stringify(results, null, 1));
await browser.close();

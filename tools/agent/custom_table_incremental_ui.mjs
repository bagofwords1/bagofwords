// Drive the custom-table modal through an incremental table's life in a real
// browser, asserting what the admin sees and capturing screenshots.
//
// Prereqs: the stack (tools/agent/boot_stack.sh), a seeded org, and
// tools/agent/custom_table_incremental_setup.py (which prints the data source
// ids this needs).
//
//   cd frontend
//   node ../tools/agent/custom_table_incremental_ui.mjs <data_source_id> <out_dir> [--scene before|after] [--locale he]
//
// `--scene before` captures only what the modal showed before this feature
// (the Settings tab), for a before/after pair; the default drives everything.
// Env: BOW_ORIGIN (default http://localhost:3000), BOW_EMAIL, BOW_PASSWORD,
// PW_CHROMIUM (default /opt/pw-browsers/chromium), INCR_LOOP_TABLE.
import { mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require('@playwright/test')); } catch { ({ chromium } = require('playwright')); }

const [dsId, outDir, ...rest] = process.argv.slice(2);
if (!dsId || !outDir) {
  console.error('usage: node custom_table_incremental_ui.mjs <data_source_id> <out_dir> [--scene before|after] [--locale he]');
  process.exit(2);
}
const flag = (name, fallback) => { const i = rest.indexOf(name); return i >= 0 ? rest[i + 1] : fallback; };
const scene = flag('--scene', 'after');
const locale = flag('--locale', 'en');
const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
const email = process.env.BOW_EMAIL || 'admin@example.com';
const password = process.env.BOW_PASSWORD || 'Password123!';
const tableName = process.env.INCR_LOOP_TABLE || 'orders_cache';
mkdirSync(outDir, { recursive: true });

const checks = [];
const check = (name, ok, detail = '') => {
  checks.push({ name, ok: !!ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
};

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium' });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
const page = await context.newPage();
const shot = async (name, target = page) => {
  await target.screenshot({ path: `${outDir}/${name}.png` });
  console.log(`shot  ${outDir}/${name}.png`);
};
const modal = page.locator('[data-testid="custom-query-modal"]');

try {
  await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.fill('#email', email);
  await page.fill('#password', password);
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
    page.click('button[type=submit]'),
  ]);
  await page.waitForTimeout(1500);
  if (page.url().includes('/onboarding')) {
    await page.getByText('Skip onboarding', { exact: false }).click({ timeout: 4000 }).catch(() => {});
    await page.waitForTimeout(1500);
  }
  if (locale !== 'en') {
    await page.evaluate((l) => localStorage.setItem('bow.locale', l), locale);
  }

  await page.goto(`${origin}/agents/${dsId}/tables`, { waitUntil: 'networkidle' }).catch(() => {});
  const row = page.locator(`[data-testid="cq-row-${tableName}"]`);
  await row.waitFor({ timeout: 30000 });
  await shot(`${scene}-01-tables`);

  // Open the cached table for editing (the pencil on its row).
  await row.hover();
  await row.locator('button:has(.i-heroicons-pencil-square), button[aria-label*="' + tableName + '"]').first().click();
  await modal.waitFor({ timeout: 15000 });
  await page.click('[data-testid="cq-tab-settings"]');
  await page.waitForTimeout(400);
  await shot(`${scene}-02-settings`, modal);

  if (scene === 'before') {
    check('the modal opens on the existing table', await modal.isVisible());
  } else {
    // --- Settings: what each refresh fetches ---------------------------------
    check('incremental is the selected mode', await page.isChecked('[data-testid="cq-mode-incremental"]'));
    check('cursor column is shown', (await page.inputValue('[data-testid="cq-cursor-column"]')) === 'updated_at');
    check('merge by key is selected', await page.isChecked('[data-testid="cq-incr-merge"]'));
    check('the key column is selected', await page.isChecked('[data-testid="cq-key-id"]'));
    check('lookback reads as one day',
      (await page.inputValue('[data-testid="cq-lookback"]')) === '1'
      && (await page.inputValue('[data-testid="cq-lookback-unit"]')) === 'days');
    const checkpoint = await page.textContent('[data-testid="cq-checkpoint"]').catch(() => '');
    check('the checkpoint is shown', /Loaded up to:\s*20\d\d-/.test(checkpoint || ''), (checkpoint || '').trim().slice(0, 60));
    check('the row-drop guard is on at 50%',
      await page.isChecked('[data-testid="cq-guard-enabled"]') && (await page.inputValue('[data-testid="cq-guard-pct"]')) === '50');

    // --- Runs -----------------------------------------------------------------
    await page.click('[data-testid="cq-tab-runs"]');
    await page.locator('[data-testid="cq-run-success"]').first().waitFor({ timeout: 15000 });
    await page.waitForTimeout(600);
    const rows = 'tr[data-testid^="cq-run-"]';
    const before = await page.locator(rows).count();
    // The first load is the oldest run: the one that created the table.
    await page.locator(rows).filter({ hasText: 'Created' }).first().click();
    await page.locator('[data-testid="cq-run-chunks"]').waitFor({ timeout: 10000 }).catch(() => {});
    const detail = await page.textContent('[data-testid="cq-run-detail"]');
    check('the first load is in the history', before >= 1 && /Read from source/.test(detail));
    check('the first load shows its chunks', await page.locator('[data-testid="cq-run-chunks"]').count() === 1);
    check('the run shows the query it sent', /SELECT/i.test(await page.textContent('[data-testid="cq-run-query"]')));
    await shot(`${scene}-03-runs-first-load`, modal);

    // --- Refresh now: an incremental run appears --------------------------------
    await page.click('[data-testid="cq-refresh-now"]');
    await page.waitForFunction(([sel, n]) => document.querySelectorAll(sel).length > n, [rows, before], { timeout: 120000 });
    await page.waitForTimeout(800);
    const top = page.locator(rows).first();
    const topText = await top.textContent();
    check('a manual refresh adds an incremental run', /Manual/.test(topText) && /New rows/.test(topText) && /OK/.test(topText), topText.replace(/\s+/g, ' ').trim());
    await top.click();
    await page.waitForTimeout(800);
    const incDetail = await page.textContent('[data-testid="cq-run-detail"]');
    check('the incremental run shows where it read from', /Read from:\s*20\d\d-/.test(incDetail));
    const sent = await page.textContent('[data-testid="cq-run-query"]');
    check('the query sent carries the window on the cursor column', /updated_at.*>=/s.test(sent), sent.replace(/\s+/g, ' ').slice(0, 160));
    await shot(`${scene}-04-runs-incremental`, modal);

    // --- Full refresh -----------------------------------------------------------
    check('an incremental table offers a full refresh', await page.isVisible('[data-testid="cq-full-refresh"]'));

    // --- A new table: the settings an admin fills in ---------------------------
    await modal.locator('button:has-text("Cancel")').click();
    await modal.waitFor({ state: 'hidden', timeout: 10000 });
    await page.click('[data-testid="add-custom-query"]');
    await modal.waitFor({ timeout: 15000 });
    await page.fill('[data-testid="cq-name"]', 'events_cache');
    const editor = modal.locator('.monaco-editor').first();
    await editor.click();
    await page.keyboard.type(process.env.INCR_LOOP_EVENTS_SQL || 'SELECT * FROM shop.events');
    await page.click('[data-testid="cq-run-preview"]');
    await page.locator('[data-testid="cq-preview-count"]').waitFor({ timeout: 60000 });
    await page.click('[data-testid="cq-tab-settings"]');
    check('a new table starts as a full refresh', await page.isChecked('[data-testid="cq-mode-full"]'));
    await page.check('[data-testid="cq-mode-incremental"]');
    await page.waitForTimeout(300);
    const err1 = await page.textContent('[data-testid="cq-incr-error"]').catch(() => '');
    check('saving is blocked until a cursor is chosen',
      /cursor column/i.test(err1 || '') && await page.isDisabled('[data-testid="cq-save"]'), (err1 || '').trim());
    await page.selectOption('[data-testid="cq-cursor-column"]', 'happened_at');
    const err2 = await page.textContent('[data-testid="cq-incr-error"]').catch(() => '');
    check('merge asks for a key', /key column/i.test(err2 || ''), (err2 || '').trim());
    await page.check('[data-testid="cq-incr-append"]');
    await page.waitForTimeout(300);
    check('append needs no key and can be saved',
      (await page.locator('[data-testid="cq-incr-error"]').count()) === 0 && await page.isEnabled('[data-testid="cq-save"]'));
    await page.click('[data-testid="cq-incr-advanced-toggle"]');
    await page.waitForTimeout(300);
    await shot(`${scene}-05-new-table-settings`, modal);
  }
} catch (e) {
  check('the flow completed', false, String(e).slice(0, 400));
  await shot(`${scene}-99-failure`).catch(() => {});
} finally {
  await browser.close();
}

const failed = checks.filter((c) => !c.ok);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
process.exit(failed.length ? 1 : 0);

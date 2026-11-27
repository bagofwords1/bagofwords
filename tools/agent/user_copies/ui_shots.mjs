// UI evidence for per-user copies of a custom table.
//
// Drives the agent's Tables page and the custom-table dialog for a table on a
// connection where each person signs in with their own credentials, and (for
// comparison) a table on a shared-credential connection.
//
//   node tools/agent/user_copies/ui_shots.mjs \
//     --per-user <data_source_id> --table sales_cached \
//     [--shared <data_source_id> --shared-table album_sales] \
//     [--delete-copy <email>] [--locale he] [--out media/pr/<slug>]
//
// Env: BOW_ORIGIN (http://localhost:3000), BOW_EMAIL / BOW_PASSWORD (the local
// dev admin by default). `--delete-copy` really deletes that person's copy
// (it is rebuilt the next time they ask); without it nothing is changed.
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';

// CommonJS resolution on purpose: it honours NODE_PATH, which is where a cloud
// sandbox keeps the Playwright build that matches its pre-installed Chromium.
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const args = process.argv.slice(2);
const arg = (name, fallback = '') => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
const email = process.env.BOW_EMAIL || 'admin@example.com';
const password = process.env.BOW_PASSWORD || 'Password123!';
const perUserDs = arg('per-user');
const table = arg('table', 'sales_cached');
const sharedDs = arg('shared');
const sharedTable = arg('shared-table');
const deleteCopy = arg('delete-copy');
const locale = arg('locale', 'en');
const outDir = arg('out', 'media/pr/custom-table-user-copies');
const prefix = locale === 'en' ? '' : `${locale}-`;
if (!perUserDs) {
  console.error('usage: ui_shots.mjs --per-user <data_source_id> [--table name] [--shared <data_source_id> --shared-table name]');
  process.exit(2);
}
mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
const page = await context.newPage();
const problems = [];
page.on('console', (m) => {
  const text = m.text();
  if (m.type() === 'error' || text.includes('[intlify]') || text.includes('[Vue warn]')) problems.push(text.slice(0, 300));
});
page.on('response', (r) => { if (r.status() >= 400) problems.push(`${r.status()} ${r.request().method()} ${r.url().replace(origin, '')}`); });
page.on('pageerror', (e) => problems.push(`pageerror: ${String(e).slice(0, 300)}`));

const modal = page.locator('[data-testid="custom-query-modal"]');
const shot = async (name, target = page) => {
  const path = `${outDir}/${prefix}${name}.png`;
  await target.screenshot({ path });
  console.log(`shot  ${path}`);
};
const openTables = async (dsId, name) => {
  await page.goto(`${origin}/agents/${dsId}/tables`, { waitUntil: 'networkidle' }).catch(() => {});
  const row = page.locator(`[data-testid="cq-row-${name}"]`);
  await row.waitFor({ timeout: 30000 });
  return row;
};
const openEdit = async (row, name) => {
  await row.hover();
  // The pencil's label is localized but always carries the table name.
  await row.locator(`button[aria-label*="${name}"]`).click();
  await modal.waitFor({ timeout: 15000 });
  await page.waitForTimeout(600);
};
const closeModal = async () => {
  // The header's close button: the first button in the dialog.
  await modal.locator('button').first().click();
  await modal.waitFor({ state: 'hidden', timeout: 10000 }).catch(() => {});
};
const tabKeys = async () => modal.locator('[data-testid^="cq-tab-"]').evaluateAll(
  (els) => els.map((e) => e.getAttribute('data-testid').replace('cq-tab-', '')));

try {
  await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.fill('#email', email);
  await page.fill('#password', password);
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
    page.click('button[type=submit]'),
  ]);
  await page.waitForTimeout(1500);
  if (locale !== 'en') await page.evaluate((l) => localStorage.setItem('bow.locale', l), locale);

  // ---- per-user table --------------------------------------------------
  let row = await openTables(perUserDs, table);
  console.log('row badge:', await row.locator(`[data-testid="cq-per-user-${table}"]`).innerText().catch(() => '(none)'));
  await shot('01-tables-row-per-user');

  await openEdit(row, table);
  console.log('per-user tabs:', (await tabKeys()).join(','));
  console.log('notice:', await modal.locator('[data-testid="cq-per-user-notice"]').innerText().catch(() => '(none)'));
  await shot('02-per-user-query', modal);

  await modal.locator('[data-testid="cq-tab-settings"]').click();
  await page.waitForTimeout(300);
  console.log('status:', (await modal.locator('[data-testid="cq-copies-status"]').innerText().catch(() => '(none)')).replace(/\n/g, ' / '));
  console.log('incremental options shown:', await modal.locator('[data-testid="cq-refresh-mode"]').count());
  await shot('03-per-user-settings', modal);

  await modal.locator('[data-testid="cq-tab-copies"]').click();
  await modal.locator('[data-testid^="cq-copy-row-"], [data-testid="cq-copies-empty"]').first().waitFor({ timeout: 15000 });
  const before = await modal.locator('tbody tr').count();
  console.log('copies listed:', before);
  await shot('04-per-user-copies', modal);

  if (before) {
    const target = deleteCopy
      ? modal.locator(`[data-testid="cq-copy-delete-${deleteCopy}"]`)
      : modal.locator('[data-testid^="cq-copy-delete-"]').last();
    await target.click();
    await modal.locator('[data-testid="cq-copies-confirm"]').waitFor();
    console.log('confirm one:', await modal.locator('[data-testid="cq-copies-confirm"] p').innerText());
    await shot('05-per-user-delete-copy-confirm', modal);
    if (deleteCopy) {
      await modal.locator('[data-testid="cq-copies-confirm-delete"]').click();
      await modal.locator('[data-testid="cq-copies-confirm"]').waitFor({ state: 'hidden', timeout: 15000 });
      await page.waitForTimeout(800);
      console.log('copies after delete:', await modal.locator('tbody tr').count());
      await shot('06-per-user-copy-deleted');
    } else {
      await modal.locator('[data-testid="cq-copies-confirm-cancel"]').click();
    }
    await modal.locator('[data-testid="cq-copies-delete-all"]').click();
    console.log('confirm all:', await modal.locator('[data-testid="cq-copies-confirm"] p').innerText());
    await shot('07-per-user-delete-all-confirm', modal);
    await modal.locator('[data-testid="cq-copies-confirm-cancel"]').click();
  }
  console.log('footer:', (await modal.locator('[data-testid="cq-refresh-now"]').innerText()).trim(),
    '| full refresh buttons:', await modal.locator('[data-testid="cq-full-refresh"]').count());
  await closeModal();

  // ---- creating a table on the per-user connection ---------------------
  await page.locator('[data-testid="add-custom-query"]').click();
  await modal.waitFor({ timeout: 15000 });
  await page.waitForTimeout(600);
  console.log('new per-user tabs:', (await tabKeys()).join(','), '| save:', (await modal.locator('[data-testid="cq-save"]').innerText()).trim());
  await shot('08-per-user-new', modal);
  await closeModal();

  // ---- shared table: unchanged -----------------------------------------
  if (sharedDs && sharedTable) {
    row = await openTables(sharedDs, sharedTable);
    console.log('shared row badge count:', await row.locator(`[data-testid="cq-per-user-${sharedTable}"]`).count());
    await openEdit(row, sharedTable);
    console.log('shared tabs:', (await tabKeys()).join(','), '| notice count:', await modal.locator('[data-testid="cq-per-user-notice"]').count());
    await shot('09-shared-query', modal);
    await modal.locator('[data-testid="cq-tab-settings"]').click();
    await page.waitForTimeout(300);
    await shot('10-shared-settings', modal);
    console.log('shared footer:', (await modal.locator('[data-testid="cq-refresh-now"]').innerText()).trim());
    await closeModal();
    await page.locator('[data-testid="add-custom-query"]').click();
    await modal.waitFor({ timeout: 15000 });
    await page.waitForTimeout(600);
    console.log('new shared save:', (await modal.locator('[data-testid="cq-save"]').innerText()).trim());
    await closeModal();
  }

  // ---- organization settings -------------------------------------------
  await page.goto(`${origin}/settings/ai_settings`, { waitUntil: 'networkidle' }).catch(() => {});
  // Sections start collapsed; searching opens the ones that match.
  if (locale === 'en') {
    await page.locator('input[type="text"], input[type="search"]').first().fill('per-user');
    await page.waitForTimeout(600);
  }
  console.log('ai settings rows for per-user copies:',
    await page.locator('[data-testid^="ai-setting-custom_table_user_cop"]').count());
  await shot('11-ai-settings');
} finally {
  console.log(problems.length ? `console problems (${problems.length}):\n  ${[...new Set(problems)].join('\n  ')}` : 'console problems: none');
  await browser.close();
}

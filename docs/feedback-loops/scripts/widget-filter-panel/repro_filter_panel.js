/**
 * Canonical reproduction for the widget filter panel (components/dashboard/VisualizationFilter.vue).
 *
 * Widget under test: a 122-row table rendered in the chat (ToolWidgetPreview).
 * Each numbered step prints panel state + real AG Grid row count.
 */
const { chromium } = require('playwright');
const fs = require('fs');

const REPORT = process.env.REPORT_URL || 'http://localhost:3000/reports/8fb854f9-82c7-4167-87a6-9c7a1cc6fed9';
const PANEL = 'div.w-\\[380px\\]';
const log = [];
const say = (...a) => { console.log(...a); log.push(a.join(' ')); };

const panelOpen = p => p.locator(PANEL).count().then(c => c > 0);
async function panelText(page) {
  if (!(await panelOpen(page))) return '<closed>';
  return (await page.locator(PANEL).first().innerText()).replace(/\n+/g, ' | ');
}
async function rowCount(page) {
  const t = await page.evaluate(() => document.body.innerText);
  const m = t.match(/1 to (\d+) of (\d+)/);
  if (m) return `${m[2]} rows (paginated, showing ${m[1]})`;
  return (await page.locator('.ag-center-cols-container .ag-row').count()) + ' rows';
}
async function chipCount(page) {
  const t = await page.locator('.ag-root-wrapper').count();
  const chip = await page.locator('span:text-matches("^\\\\d+$")').allInnerTexts().catch(() => []);
  return chip.join(',');
}
async function clickFunnel(page) {
  for (const b of await page.locator('button').all()) {
    const h = await b.innerHTML().catch(() => '');
    if (h.includes('funnel')) { await b.click(); await page.waitForTimeout(1200); return true; }
  }
  say('!! funnel button not found');
  return false;
}
async function ensureOpen(page) { if (!(await panelOpen(page))) await clickFunnel(page); }
async function xRemovers(page) {
  const out = [];
  for (const b of await page.locator(PANEL).locator('button').all()) {
    const h = await b.innerHTML().catch(() => '');
    if (h.includes('x-mark')) out.push(b);
  }
  return out;
}

(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
  const ctx = await browser.newContext({ storageState: 'state.json', viewport: { width: 1500, height: 950 } });
  const page = await ctx.newPage();
  await page.goto(REPORT, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(14000);

  say('[0] grid unfiltered:', await rowCount(page));

  // 1. open panel, add a filter: Project contains P10
  await clickFunnel(page);
  say('[1] panel opened:', await panelText(page));
  await page.getByRole('button', { name: 'Add filter' }).click();
  await page.waitForTimeout(700);
  say('[2] after "Add filter" (note the row count preview with an empty value):', await panelText(page));

  await page.locator(PANEL).locator('button').filter({ hasText: /Logistic Company/ }).first().click();
  await page.waitForTimeout(600);
  await page.getByRole('option', { name: /^Project$/i }).first().click();
  await page.waitForTimeout(600);
  await page.locator(PANEL).locator('button').filter({ hasText: /^equals$/ }).first().click();
  await page.waitForTimeout(600);
  await page.getByRole('option', { name: /^contains$/i }).first().click();
  await page.waitForTimeout(600);
  await page.locator(PANEL).locator('input[type="text"]').first().fill('P10');
  await page.waitForTimeout(700);
  say('[3] condition filled (Project contains P10):', await panelText(page));
  await page.screenshot({ path: 'shots/13_1_filled.png' });

  // 2. Apply
  await page.getByRole('button', { name: 'Apply' }).click();
  await page.waitForTimeout(1800);
  say('[4] BUG B - popover still open after Apply?', await panelOpen(page));
  say('[4] grid after Apply:', await rowCount(page));
  await page.screenshot({ path: 'shots/13_2_applied.png' });

  // 3. remove the (only) condition with its X
  await ensureOpen(page);
  const rs = await xRemovers(page);
  say('[5] X buttons in panel:', rs.length);
  await rs[0].click();
  await page.waitForTimeout(1000);
  say('[6] BUG A - panel after clicking X:', await panelText(page));
  say('[6] BUG A - Apply button still present?', (await page.getByRole('button', { name: 'Apply' }).count()) > 0);
  say('[6] BUG A - grid still filtered:', await rowCount(page));
  await page.screenshot({ path: 'shots/13_3_after_x.png' });

  // 4. close the panel like a user would; grid should be unfiltered but is not
  await page.keyboard.press('Escape');
  await page.waitForTimeout(1200);
  say('[7] grid after closing the panel:', await rowCount(page));
  await page.screenshot({ path: 'shots/13_4_closed.png', fullPage: true });

  // 5. reopen: panel says "no filters" while the grid is still filtered
  await ensureOpen(page);
  say('[8] panel on reopen (diverged from the grid):', await panelText(page));
  await page.screenshot({ path: 'shots/13_5_reopen.png' });

  // 6. only Clear recovers
  const clear = page.getByRole('button', { name: 'Clear' });
  say('[9] Clear present:', await clear.count());
  if (await clear.count()) {
    await clear.click();
    await page.waitForTimeout(1500);
    say('[9] grid after Clear:', await rowCount(page));
  }
  await page.screenshot({ path: 'shots/13_6_after_clear.png' });

  fs.writeFileSync('13_log.txt', log.join('\n'));
  await browser.close();
})();

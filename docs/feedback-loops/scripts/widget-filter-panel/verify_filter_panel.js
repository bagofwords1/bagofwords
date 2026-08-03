/**
 * Verification of the filter-panel fixes. Same driving as the repro, but asserts.
 */
const { chromium } = require('playwright');
const fs = require('fs');

const REPORT = process.env.REPORT_URL || 'http://localhost:3000/reports/8fb854f9-82c7-4167-87a6-9c7a1cc6fed9';
const PANEL = 'div.w-\\[380px\\]';
const log = [];
const say = (...a) => { console.log(...a); log.push(a.join(' ')); };
let failures = 0;
function check(name, cond, detail) {
  const s = cond ? 'PASS' : 'FAIL';
  if (!cond) failures++;
  say(`[${s}] ${name}${detail ? ' — ' + detail : ''}`);
}

const panelOpen = p => p.locator(PANEL).count().then(c => c > 0);
async function panelText(page) {
  if (!(await panelOpen(page))) return '<closed>';
  return (await page.locator(PANEL).first().innerText()).replace(/\n+/g, ' | ');
}
async function rows(page) {
  const t = await page.evaluate(() => document.body.innerText);
  const m = t.match(/1 to (\d+) of (\d+)/);
  if (m) return Number(m[2]);
  return page.locator('.ag-center-cols-container .ag-row').count();
}
async function clickFunnel(page) {
  for (const b of await page.locator('button').all()) {
    const h = await b.innerHTML().catch(() => '');
    if (h.includes('funnel')) { await b.click(); await page.waitForTimeout(1200); return true; }
  }
  say('!! funnel not found');
  return false;
}
async function xRemovers(page) {
  const out = [];
  for (const b of await page.locator(PANEL).locator('button').all()) {
    const h = await b.innerHTML().catch(() => '');
    if (h.includes('x-mark')) out.push(b);
  }
  return out;
}
async function addCondition(page, column, operator, value) {
  await page.locator(PANEL).locator('button').filter({ hasText: /Logistic Company/ }).last().click();
  await page.waitForTimeout(600);
  await page.getByRole('option', { name: new RegExp('^' + column + '$', 'i') }).first().click();
  await page.waitForTimeout(600);
  if (operator) {
    await page.locator(PANEL).locator('button').filter({ hasText: /^equals$/ }).last().click();
    await page.waitForTimeout(600);
    await page.getByRole('option', { name: new RegExp('^' + operator + '$', 'i') }).first().click();
    await page.waitForTimeout(600);
  }
  if (value !== undefined) {
    await page.locator(PANEL).locator('input[type="text"]').last().fill(value);
    await page.waitForTimeout(600);
  }
}

(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
  const ctx = await browser.newContext({ storageState: 'state.json', viewport: { width: 1500, height: 950 } });
  const page = await ctx.newPage();
  page.on('pageerror', e => say('[pageerror]', String(e).slice(0, 200)));
  await page.goto(REPORT, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(30000);

  const total = await rows(page);
  say('grid unfiltered:', total);

  // ---- 1. add + apply ----
  await clickFunnel(page);
  await page.getByRole('button', { name: 'Add filter' }).click();
  await page.waitForTimeout(700);
  const previewEmpty = await panelText(page);
  check('BUG C: a freshly added empty condition does not zero the preview',
    /122 of 122 rows/.test(previewEmpty), previewEmpty);

  await addCondition(page, 'Project', 'contains', 'P10');
  say('panel:', await panelText(page));
  await page.getByRole('button', { name: 'Apply' }).click();
  await page.waitForTimeout(1800);
  check('BUG B: popover closes on Apply', !(await panelOpen(page)));
  check('filter applied to grid', (await rows(page)) === 10, `${await rows(page)} rows`);
  await page.screenshot({ path: 'shots/14_1_applied.png' });

  // ---- 2. reopen shows the applied condition ----
  await clickFunnel(page);
  const reopened = await panelText(page);
  check('reopen shows the applied condition', /Project/.test(reopened) && /contains/.test(reopened), reopened);

  // ---- 3. "+ AND" must not blank the table ----
  await page.getByRole('button', { name: 'AND', exact: true }).first().click();
  await page.waitForTimeout(900);
  const afterAnd = await panelText(page);
  check('BUG C: "+ AND" keeps the preview at the current match count',
    /10 of 122 rows/.test(afterAnd), afterAnd);
  await page.getByRole('button', { name: 'Apply' }).click();
  await page.waitForTimeout(1800);
  check('applying with a half-written 2nd condition keeps the rows',
    (await rows(page)) === 10, `${await rows(page)} rows`);
  await page.screenshot({ path: 'shots/14_2_after_and.png' });

  // ---- 4. remove conditions, last one included ----
  if (!(await panelOpen(page))) await clickFunnel(page);
  let rs = await xRemovers(page);
  say('X buttons:', rs.length);
  await rs[rs.length - 1].click();           // drop the empty 2nd condition
  await page.waitForTimeout(800);
  rs = await xRemovers(page);
  check('one condition left after removing the empty one', rs.length === 1, `${rs.length} left`);
  await rs[0].click();                        // drop the LAST condition
  await page.waitForTimeout(900);
  const afterRemoveLast = await panelText(page);
  check('BUG A: Apply is still reachable after removing the last condition',
    (await page.getByRole('button', { name: 'Apply' }).count()) > 0, afterRemoveLast);
  await page.screenshot({ path: 'shots/14_3_after_remove_last.png' });

  await page.getByRole('button', { name: 'Apply' }).click();
  await page.waitForTimeout(1800);
  check('BUG A: removal is committed — grid back to all rows',
    (await rows(page)) === total, `${await rows(page)} rows`);
  check('popover closed after committing the removal', !(await panelOpen(page)));
  await page.screenshot({ path: 'shots/14_4_removed.png' });

  // ---- 5. panel and grid agree afterwards ----
  await clickFunnel(page);
  const finalPanel = await panelText(page);
  check('panel agrees with the grid (no stale "Clear")',
    /No filters applied/.test(finalPanel) && !/Clear/.test(finalPanel), finalPanel);

  // ---- 6. Clear still works ----
  await page.getByRole('button', { name: 'Add filter' }).click();
  await page.waitForTimeout(600);
  await addCondition(page, 'Project', 'contains', 'P10');
  await page.getByRole('button', { name: 'Apply' }).click();
  await page.waitForTimeout(1600);
  check('re-applied after clearing', (await rows(page)) === 10, `${await rows(page)} rows`);
  await clickFunnel(page);
  await page.getByRole('button', { name: 'Clear' }).click();
  await page.waitForTimeout(1600);
  check('Clear still resets the grid', (await rows(page)) === total, `${await rows(page)} rows`);
  await page.screenshot({ path: 'shots/14_5_clear.png' });

  say('');
  say(failures === 0 ? 'ALL CHECKS PASSED' : `${failures} CHECK(S) FAILED`);
  fs.writeFileSync('14_log.txt', log.join('\n'));
  await browser.close();
  process.exit(failures === 0 ? 0 : 1);
})();

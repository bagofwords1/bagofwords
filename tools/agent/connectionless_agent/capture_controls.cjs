// Before/after shots of the three controls this change touches (wizard, manage-connections, delete warning).
//   cd frontend && PHASE=before|after OUT=/tmp/connectionless-agent node ../tools/agent/connectionless_agent/capture_controls.cjs
const { chromium } = require('playwright');
const SP = (process.env.OUT || '/tmp/connectionless-agent'), PHASE = process.env.PHASE || 'before';
const out = (n) => `${SP}/shots/${PHASE}_${n}.png`;
let page;
(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, storageState: SP + '/state.json' });
  page = await ctx.newPage();
  page.on('pageerror', e => console.log('PAGEERROR', e.message.slice(0, 300)));
  page.on('console', m => { if (m.type() === 'error') console.log('CONSOLE', m.text().slice(0, 300)); });
  const go = async (p) => { await page.goto('http://localhost:3000' + p, { waitUntil: 'load' }); await page.waitForTimeout(2500); };

  // 1) New agent wizard, nothing selected.
  await go('/agents');
  await page.getByText('Music Store', { exact: true }).first().waitFor({ timeout: 60000 });
  await page.getByRole('button', { name: 'New agent' }).first().click();
  const wiz = page.getByTestId('new-agent-wizard');
  await wiz.waitFor({ timeout: 10000 });
  await page.waitForTimeout(1500);
  await wiz.locator('input').first().fill('HR Policies');
  await page.waitForTimeout(500);
  await wiz.screenshot({ path: out('1_wizard_no_connection') });
  console.log('save button visible:', await wiz.getByRole('button', { name: 'Save & Continue' }).isVisible().catch(() => false));
  await page.keyboard.press('Escape');
  await page.waitForTimeout(800);

  // 2) Agent with a single connection → Manage connections.
  await go('/agents');
  await page.getByText('sqlite_source_1', { exact: true }).first().click();
  await page.waitForTimeout(2500);
  await page.getByRole('button', { name: /actions/i }).first().click().catch(async () => {
    await page.locator('.landing-icon-button').first().click();
  });
  await page.waitForTimeout(800);
  await page.getByText('Manage connections', { exact: true }).first().click();
  await page.waitForTimeout(2000);
  const dlg = page.locator('[id^=headlessui-dialog-panel]').last();
  await dlg.screenshot({ path: out('2_manage_connections') });
  console.log('unlink buttons:', await dlg.locator('[class*="link-slash"]').count());

  // 3) Edit → Delete connection → confirmation warning.
  await dlg.locator('[class*="pencil"]').first().click();
  await page.waitForTimeout(2000);
  const del = page.getByText('Delete Connection', { exact: true }).last();
  await del.scrollIntoViewIfNeeded().catch(() => {});
  await del.click();
  await page.waitForTimeout(1000);
  const warn = page.locator('text=This will impact').last();
  const box = warn.locator('xpath=ancestor::div[contains(@class,"space-y-3")][1]');
  await box.screenshot({ path: out('3_delete_connection_warning') }).catch(async () => page.screenshot({ path: out('3_delete_connection_warning') }));
  console.log('done', PHASE);
  await browser.close();
})().catch(async e => { console.error('ERR', e.message.split('\n')[0]); try { await page.screenshot({ path: SP + '/shots/' + PHASE + '_error.png' }); } catch {} process.exit(1); });

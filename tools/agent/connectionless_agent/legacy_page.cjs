// Legacy /agents/new page: create an agent with no connection and land on the context step.
//   cd frontend && OUT=/tmp/connectionless-agent node ../tools/agent/connectionless_agent/legacy_page.cjs
const { chromium } = require('playwright');
const SP = (process.env.OUT || '/tmp/connectionless-agent');
(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, storageState: SP + '/state.json' });
  const page = await ctx.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push(e.message.slice(0, 200)));
  await page.goto('http://localhost:3000/agents/new', { waitUntil: 'load' });
  await page.getByText('Create Data Agent').first().waitFor({ timeout: 60000 });
  await page.waitForTimeout(2500);
  console.log('add-connection modal auto-opened:', await page.getByText('Add Connection').first().isVisible().catch(() => false));
  await page.locator('input').first().fill('Glossary ' + Date.now());
  // Deselect the auto-selected single connection.
  await page.locator('button:has-text("SQLite Chinook")').first().click();
  await page.waitForTimeout(600);
  await page.getByRole('option', { name: /SQLite Chinook/ }).first().click();
  await page.keyboard.press('Escape');
  await page.waitForTimeout(600);
  console.log('hint visible:', await page.getByText('Leave empty to build the agent').isVisible());
  await page.getByRole('button', { name: 'Save & Continue' }).click();
  await page.waitForURL(/\/agents\/new\/[^/]+\/context/, { timeout: 30000 });
  await page.waitForTimeout(3000);
  console.log('landed on', page.url().replace(/[0-9a-f-]{36}/, '<id>'));
  console.log('drafting spinner visible:', await page.getByText('Generating overview instruction').isVisible().catch(() => false));
  await page.screenshot({ path: SP + '/shots/legacy_context.png' });
  console.log('page errors:', JSON.stringify(errs));
  await browser.close();
})().catch(e => { console.error('ERR', e.message.split('\n')[0]); process.exit(1); });

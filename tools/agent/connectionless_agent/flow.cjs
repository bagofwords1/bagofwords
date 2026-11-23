// End-to-end UI flow: create an agent with no connection, open it, chat with it, then delete
// another agent's only connection and check that agent is kept. Records a video in $OUT/video.
//   cd frontend && OUT=/tmp/connectionless-agent node ../tools/agent/connectionless_agent/flow.cjs
const { chromium } = require('playwright');
const SP = (process.env.OUT || '/tmp/connectionless-agent');
const shot = (n) => `${SP}/shots/flow_${n}.png`;
let page;
(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, storageState: SP + '/state.json',
    recordVideo: { dir: SP + '/video', size: { width: 1440, height: 900 } } });
  page = await ctx.newPage();
  page.on('pageerror', e => console.log('PAGEERROR', e.message.slice(0, 300)));
  const go = async (p) => { await page.goto('http://localhost:3000' + p, { waitUntil: 'load' }); await page.waitForTimeout(2000); };

  // A) Create a connectionless agent through the wizard.
  await go('/agents');
  await page.getByText('Music Store', { exact: true }).first().waitFor({ timeout: 60000 });
  await page.getByRole('button', { name: 'New agent' }).first().click();
  const wiz = page.getByTestId('new-agent-wizard');
  await wiz.waitFor();
  await page.waitForTimeout(1200);
  await wiz.locator('input').first().pressSequentially('HR Policies', { delay: 40 });
  await page.waitForTimeout(600);
  await wiz.getByRole('button', { name: 'Save & Continue' }).click();
  await wiz.getByText('Add custom AI rules and instructions').waitFor({ timeout: 20000 });
  await page.waitForTimeout(1500);
  const editor = wiz.locator('[contenteditable=true]').first();
  await editor.click();
  await page.keyboard.type('Full-time employees get 25 vacation days a year. Unused days carry over until March 31.', { delay: 10 });
  await page.waitForTimeout(800);
  await wiz.screenshot({ path: shot('A_context_step') });
  await wiz.getByRole('button', { name: 'Finish' }).click();
  await page.waitForTimeout(3000);

  // B) The agent in the tree and its landing page.
  await go('/agents');
  await page.getByText('HR Policies', { exact: true }).first().waitFor({ timeout: 60000 });
  await page.getByText('HR Policies', { exact: true }).first().click();
  await page.getByTestId('agent-no-connections').waitFor({ timeout: 20000 });
  await page.getByRole('button', { name: /Add a description/ }).first().click();
  await page.keyboard.type('Answers HR policy questions: leave, benefits and travel.', { delay: 10 });
  await page.keyboard.press('Enter');
  await page.waitForTimeout(2000);
  await page.screenshot({ path: shot('B_agent_landing') });

  // C) Chat with it.
  await page.getByTestId('agent-new-report').click();
  await page.waitForURL(/\/reports\//, { timeout: 30000 });
  await page.waitForTimeout(3000);
  const box = page.locator('[contenteditable=true], textarea').first();
  await box.click();
  await page.keyboard.type('How many vacation days do I get?', { delay: 15 });
  await page.keyboard.press('Enter');
  await page.getByText('Agents I was given for this question').first().waitFor({ timeout: 90000 });
  await page.waitForTimeout(2500);
  await page.screenshot({ path: shot('C_chat_answer') });

  // D) Delete the only connection of sqlite_source_1 from its agent page.
  await go('/agents');
  await page.getByText('sqlite_source_1', { exact: true }).first().waitFor({ timeout: 60000 });
  await page.getByText('sqlite_source_1', { exact: true }).first().click();
  await page.waitForTimeout(2000);
  await page.locator('.landing-icon-button').first().click();
  await page.waitForTimeout(600);
  await page.getByText('Manage connections', { exact: true }).first().click();
  await page.waitForTimeout(1500);
  const dlg = page.locator('[id^=headlessui-dialog-panel]').last();
  await dlg.locator('[class*="pencil"]').first().click();
  await page.waitForTimeout(1500);
  await page.getByText('Delete Connection', { exact: true }).last().click();
  await page.getByText('This will impact').last().waitFor({ timeout: 10000 });
  await page.waitForTimeout(1200);
  await page.getByRole('button', { name: 'Delete', exact: true }).last().click();
  await page.waitForTimeout(3000);
  await go('/agents');
  await page.getByText('sqlite_source_1', { exact: true }).first().waitFor({ timeout: 60000 });
  await page.getByText('sqlite_source_1', { exact: true }).first().click();
  await page.getByTestId('agent-no-connections').waitFor({ timeout: 20000 });
  await page.waitForTimeout(2000);
  await page.screenshot({ path: shot('D_agent_kept_after_connection_delete') });
  console.log('flow done');
  await ctx.close();
  await browser.close();
})().catch(async e => { console.error('ERR', e.message.split('\n')[0]); try { await page.screenshot({ path: SP + '/shots/flow_error.png' }); } catch {} process.exit(1); });

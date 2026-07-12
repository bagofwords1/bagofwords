// Open the seeded report, run a prompt that drives the file tools, screenshot
// the live agent completion.
import { createRequire } from 'module';
const require = createRequire('/home/user/bagofwords/frontend/');
const { chromium } = require('playwright');
import fs from 'fs';

const OUT = '/home/user/bagofwords/.agents/sandbox-feedback-loop/shots';
const BASE = 'http://localhost:3000';
const SEED = JSON.parse(fs.readFileSync('/home/user/bagofwords/.agents/sandbox-feedback-loop/runtime/seed.json'));
const shot = async (page, n) => { await page.screenshot({ path: `${OUT}/${n}.png`, fullPage: true }); console.log('shot:', n); };

const run = async () => {
  const browser = await chromium.launch({ headless: true, executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(25000);

  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);
  await page.locator('#email').fill('admin@example.com');
  await page.locator('#password').fill('Password123!');
  await page.locator('button[type="submit"]').first().click();
  try { await page.waitForURL((u) => !u.pathname.includes('/users/sign-in'), { timeout: 30000 }); } catch (e) {}
  await page.waitForTimeout(2000);

  await page.goto(SEED.report_url, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  await shot(page, '08-report-open');

  const prompt = `Use the S3 connection (connection_id ${SEED.connections.find(c => c[0].startsWith('S3'))[1]}): `
    + `list the files, then read docs/team.json and tell me its keys. `
    + `Then try to read output.parquet and report whether you were allowed to.`;

  // Find the chat input (textarea or contenteditable).
  let input = page.locator('textarea').last();
  if (!(await input.count())) input = page.locator('[contenteditable="true"]').last();
  await input.click();
  await input.fill(prompt);
  await page.waitForTimeout(400);
  await shot(page, '09-report-prompt');
  await page.keyboard.press('Enter');

  // Let the agent stream tool calls + answer.
  await page.waitForTimeout(45000);
  await shot(page, '10-report-completion');

  await browser.close();
  console.log('done');
};
run().catch((e) => { console.error('ERR', e); process.exit(1); });

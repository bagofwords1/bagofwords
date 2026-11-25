// Drive the "agent needs your sign-in" flow as a real member, through the UI
// with a real LLM, and capture screenshots + a video at each stage.
//
//   cd frontend && node ../tools/agent/signin_required_flow.mjs <outDir> [--before]
//
// Setup (see docs/feedback-loops/agent-signin-required.md): a per-user
// ("user_required") Postgres agent "Sales" the member has NOT signed in to, a
// system_only agent "Inventory", and the member member@example.com.
//
// Stages (after the fix):
//   01-question.png     the member's question typed on the home page
//   02-signin-card.png  the reply names the Sales agent + a Connect card
//   03-credentials.png  Connect opens the sign-in modal (member's own DB login)
//   04-connected.png    card flips to Connected + "Ask again"
//   05-answer.png       the same question re-runs and is answered from Sales
// With --before (fix stashed) it stops after the first reply: 02-before.png.
//
// Env: BOW_ORIGIN (default http://localhost:3000), MEMBER_EMAIL/MEMBER_PASSWORD,
// SALES_DB_USER/SALES_DB_PASSWORD (the member's personal database login).
import { mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';

// Resolve Playwright from frontend/ (the project pin) or a global install.
const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require(require.resolve('@playwright/test', { paths: [process.cwd()] }))); }
catch { ({ chromium } = require('playwright')); }

const OUT = process.argv[2] || '../media/agent-signin-required';
const BEFORE = process.argv.includes('--before');
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const EMAIL = process.env.MEMBER_EMAIL || 'member@example.com';
const PASSWORD = process.env.MEMBER_PASSWORD || 'Password123!';
const DB_USER = process.env.SALES_DB_USER || 'alice';
const DB_PASSWORD = process.env.SALES_DB_PASSWORD || '';
const QUESTION = 'What were total sales by region last quarter?';
mkdirSync(OUT, { recursive: true });

// frontend/ pins its own Playwright; sandboxes pre-install one Chromium.
const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium' });
const ctx = await browser.newContext({
  viewport: { width: 1280, height: 860 },
  deviceScaleFactor: 2,
  recordVideo: { dir: `${OUT}/video`, size: { width: 1280, height: 860 } },
});
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const shot = async (name) => { await page.waitForTimeout(600); await page.screenshot({ path: `${OUT}/${name}` }); console.log('captured', name); };
const fail = async (why) => { await page.screenshot({ path: `${OUT}/FAIL.png`, fullPage: true }); console.error('FAIL:', why); await ctx.close(); await browser.close(); process.exit(1); };

// Sign in as the member.
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.fill('#email', EMAIL);
await page.fill('#password', PASSWORD);
await Promise.all([
  page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.click('button[type=submit]'),
]);
await page.waitForTimeout(2000);
await page.goto(`${BASE}/`, { waitUntil: 'networkidle' }).catch(() => {});
await page.waitForTimeout(2500);

// Ask, in Auto mode (no agent pinned).
const box = page.locator('[contenteditable=true]').first();
await box.click();
await page.keyboard.type(QUESTION, { delay: 10 });
await shot('01-question.png');
await page.keyboard.press('Enter');
await page.waitForURL(/\/reports\//, { timeout: 30000 }).catch(() => {});

// Wait for the turn to finish (the composer's stop button goes away and an
// assistant reply is on screen).
async function waitForTurn(predicate, timeoutMs = 180000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    await page.waitForTimeout(2500);
    if (await predicate()) return true;
  }
  return false;
}

if (BEFORE) {
  const done = await waitForTurn(async () => (await page.getByText(/feedback|copy/i).count()) > 0 || (await page.locator('.markdown-wrapper').count()) > 0, 180000);
  await page.waitForTimeout(8000);
  await page.screenshot({ path: `${OUT}/02-before.png` });
  console.log('captured 02-before.png', done ? '' : '(timed out waiting)');
  await ctx.close(); await browser.close();
  process.exit(0);
}

const card = page.locator('[data-testid=request-agent-signin]');
const connect = card.locator('[data-testid=signin-connect]');
if (!(await waitForTurn(async () => (await connect.count()) > 0))) await fail('no Connect card in the reply');
await page.waitForTimeout(2500);
await card.scrollIntoViewIfNeeded();
await shot('02-signin-card.png');

// Connect → the per-user credentials modal (Postgres user/password).
await connect.first().click();
await page.locator('input[type=password]').first().waitFor({ timeout: 15000 }).catch(() => fail('credentials modal did not open'));
const textInputs = page.locator('input[type=text]:visible');
await textInputs.last().fill(DB_USER);
await page.locator('input[type=password]').first().fill(DB_PASSWORD);
await shot('03-credentials.png');
await page.getByRole('button', { name: /^Save$/ }).click();

const connected = card.locator('[data-testid=signin-connected]');
if (!(await waitForTurn(async () => (await connected.count()) > 0, 30000))) await fail('card did not flip to Connected');
await card.scrollIntoViewIfNeeded();
await shot('04-connected.png');

// Ask again → the same question runs with Sales now usable.
await card.locator('[data-testid=signin-ask-again]').click();
// The re-run's data result folds behind its source line (the app's normal
// run fold); wait for that line, then open it to see the queried rows.
// Wait for the run to END (its final message), not just for the result to
// stream in: the fold only settles once the answer is written.
const finalMsg = page.getByText(/total sales by region for last quarter/i);
if (!(await waitForTurn(async () => (await finalMsg.count()) > 0, 240000))) await fail('re-run produced no answer');
await page.waitForTimeout(3000);
const source = page.getByText('Sales by region — last quarter').last();
await page.waitForTimeout(3000);
await page.screenshot({ path: `${OUT}/05-answer.png` });
console.log('captured 05-answer.png');
await source.click();
await page.waitForTimeout(2500);
await page.screenshot({ path: `${OUT}/06-result.png` });
console.log('captured 06-result.png');
// Chart labels are drawn on a canvas: read the rows from the Data tab.
await page.getByRole('tab', { name: 'Data' }).or(page.getByText('Data', { exact: true })).last().click();
const rowsShown = await waitForTurn(async () => {
  const txt = await page.locator('body').innerText();
  // Region names exist only in the member's Sales database.
  return /North America/.test(txt) && /Europe/.test(txt) && /APAC/.test(txt) && /LATAM/.test(txt);
}, 30000);
if (!rowsShown) await fail('the Sales result does not show the regions from the database');
await page.waitForTimeout(1500);
await page.screenshot({ path: `${OUT}/07-rows.png` });
console.log('PASS: re-run answered from the Sales database');

await ctx.close();
await browser.close();

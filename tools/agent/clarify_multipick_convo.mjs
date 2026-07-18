// Drive a real BOW conversation (Haiku) that triggers a multi-select clarify,
// pick several options, submit, and capture screenshots at each stage.
//
//   cd frontend && node ../tools/agent/clarify_multipick_convo.mjs [outDir]
//
// Stages captured:
//   01-typed.png        prompt typed into the composer
//   02-clarify-form.png clarify form rendered with the multi-select hint
//   03-multi-picked.png several options selected (check badges)
//   04-submitted.png    form locked + answers sent as the next user turn
//   05-rehydrated.png   after full page reload — persisted response restored
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.argv[2] || '../media/clarify-multipick';
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const PROMPT =
  'I want a dashboard from the demo data, but before you build anything, ' +
  'ask me which metrics to include — several may apply, so let me pick multiple.';
mkdirSync(OUT, { recursive: true });

const b = await chromium.launch({
  executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
});
const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);

// login
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.getByPlaceholder(/email/i).fill('admin@example.com');
await page.getByPlaceholder(/password/i).fill('Password123!');
await Promise.all([
  page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2500);

await page.goto(`${BASE}/`, { waitUntil: 'networkidle' }).catch(() => {});
await page.waitForTimeout(3000);

// Type the prompt and send.
const box = page.locator('[contenteditable=true]').first();
await box.click();
await page.keyboard.type(PROMPT, { delay: 5 });
await page.waitForTimeout(500);
await page.screenshot({ path: `${OUT}/01-typed.png` });
const sendBtn = page.locator('button.rounded-full').last();
await sendBtn.click({ timeout: 8000 }).catch(async () => { await box.click(); await page.keyboard.press('Enter'); });
await page.waitForTimeout(2000);
console.log('url after submit:', page.url());

// Wait for the clarify form. The multiHint <p> only renders for a
// multi_select question — its presence proves the flag flowed LLM -> tool ->
// SSE -> component.
const hint = page.getByText('Select all that apply', { exact: true }).first();
let found = false;
for (let i = 0; i < 40; i++) {
  await page.waitForTimeout(3000);
  if (await hint.count()) { found = true; break; }
}
if (!found) {
  await page.screenshot({ path: `${OUT}/02-clarify-form-MISSING.png`, fullPage: true });
  console.error('FAIL: multi-select clarify form did not appear within 120s');
  await ctx.close(); await b.close(); process.exit(1);
}
await page.waitForTimeout(1500);
await page.screenshot({ path: `${OUT}/02-clarify-form.png`, fullPage: true });

// The question block that owns the hint; its option rows are the buttons with
// the letter badge.
const qBlock = page.locator('div.space-y-1\\.5').filter({ has: page.getByText('Select all that apply', { exact: true }) }).first();
const options = qBlock.locator('button');
const optCount = await options.count();
console.log('multi-select option rows:', optCount);
const labels = [];
// Pick every non-"Other" option up to 3 picks.
let picked = 0;
for (let i = 0; i < optCount && picked < 3; i++) {
  const text = (await options.nth(i).innerText()).trim();
  if (/other/i.test(text.split('\n').pop())) continue;
  await options.nth(i).click();
  labels.push(text.replace(/\n/g, ' '));
  picked++;
  await page.waitForTimeout(400);
}
console.log('picked options:', JSON.stringify(labels));
if (picked < 2) {
  console.error('FAIL: fewer than 2 selectable options — cannot demonstrate multi-pick');
  await page.screenshot({ path: `${OUT}/03-multi-picked-FAIL.png`, fullPage: true });
  await ctx.close(); await b.close(); process.exit(1);
}
await page.screenshot({ path: `${OUT}/03-multi-picked.png`, fullPage: true });

// Answer any remaining questions in the form (single-pick chips: first
// option; free-form inputs: a canned answer) so Submit enables.
const form = page.locator('div.space-y-4.ms-4').first();
const otherBlocks = form.locator('div.space-y-1\\.5');
const nBlocks = await otherBlocks.count();
for (let i = 0; i < nBlocks; i++) {
  const blk = otherBlocks.nth(i);
  if (await blk.getByText('Select all that apply', { exact: true }).count()) continue;
  const btns = blk.locator('button');
  if (await btns.count()) {
    const first = btns.first();
    const t = (await first.innerText()).trim();
    if (!/other/i.test(t.split('\n').pop())) await first.click();
    else if (await btns.count() > 1) await btns.nth(1).click();
  } else {
    const input = blk.locator('input[type=text]');
    if (await input.count()) await input.fill('Monthly, last 12 months');
  }
  await page.waitForTimeout(300);
}

// Submit the clarify form (its submit button, not the composer).
const submitBtn = form.getByRole('button', { name: 'Submit' });
await submitBtn.click({ timeout: 8000 });
await page.waitForTimeout(2500);
await page.screenshot({ path: `${OUT}/04-submitted.png`, fullPage: true });
console.log('submitted; url:', page.url());

// Reload — the form must rehydrate LOCKED with the same picks (persisted via
// the clarify_response endpoint, not sessionStorage: clear it first).
await page.evaluate(() => sessionStorage.clear());
await page.reload({ waitUntil: 'networkidle' }).catch(() => {});
await page.waitForTimeout(5000);
const rehydrated = await page.getByText('Select all that apply', { exact: true }).count();
console.log('rehydrated form present after reload + sessionStorage.clear():', rehydrated > 0);
await page.screenshot({ path: `${OUT}/05-rehydrated.png`, fullPage: true });

await ctx.close(); await b.close();
console.log('done');

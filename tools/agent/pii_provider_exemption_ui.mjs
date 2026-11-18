// UI leg of the per-provider PII exemption loop
// (docs/feedback-loops/pii-provider-exemption.md).
//
//   node tools/agent/pii_provider_exemption_ui.mjs shot <out.png>
//       sign in, screenshot Settings → PII Protection (BOW_LOCALE=he for RTL)
//   node tools/agent/pii_provider_exemption_ui.mjs flow <out-dir>
//       exempt "In-VPC Llama" from the PII page, then send one chat message
//       containing an email through each provider and report what the mock
//       LLM (pii_capture_llm.py) received
//
// Needs the stack, pii_capture_llm.py and pii_provider_exemption_setup.py.
// Env: BOW_ORIGIN (http://localhost:3000), BOW_EMAIL / BOW_PASSWORD,
// MOCK_URL (http://127.0.0.1:9199), PW_CHROMIUM (/opt/pw-browsers/chromium).
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';

const requireFromFrontend = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = requireFromFrontend('@playwright/test');

const [mode, out] = process.argv.slice(2);
if (!['shot', 'flow'].includes(mode) || !out) {
  console.error('usage: node pii_provider_exemption_ui.mjs shot <out.png> | flow <out-dir>');
  process.exit(2);
}
const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
const email = process.env.BOW_EMAIL || 'admin@example.com';
const password = process.env.BOW_PASSWORD || 'Password123!';
const mockUrl = process.env.MOCK_URL || 'http://127.0.0.1:9199';
const RAW = 'jane.roe@contoso.com';

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium' });
const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 });
const page = await context.newPage();

async function signIn() {
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
}

async function openPii() {
  await page.goto(`${origin}/settings/pii`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.getByTestId('pii-provider-exemptions').or(page.getByText('Built-in rules')).first().waitFor({ timeout: 30000 });
  await page.waitForTimeout(800);
}

async function captures() {
  return (await fetch(`${mockUrl}/captures`)).json();
}

await signIn();

if (mode === 'shot') {
  mkdirSync(dirname(out), { recursive: true });
  // BOW_LOCALE=he etc.: the per-user locale override the i18n plugin reads.
  if (process.env.BOW_LOCALE) {
    await page.evaluate((l) => localStorage.setItem('bow.locale', l), process.env.BOW_LOCALE);
  }
  await openPii();
  await page.screenshot({ path: out, fullPage: true });
  console.log(`captured ${out}`);
  await browser.close();
  process.exit(0);
}

// ---- flow ----------------------------------------------------------------
mkdirSync(out, { recursive: true });
const result = {};

// 1. Exempt the trusted provider from the PII page (autosaves).
await openPii();
const section = page.getByTestId('pii-provider-exemptions');
await section.scrollIntoViewIfNeeded();
await page.screenshot({ path: join(out, 'after-settings-before-toggle.png'), fullPage: true });
const row = section.getByTestId('pii-provider-row').filter({ hasText: 'In-VPC Llama' });
await row.getByRole('switch').click();
await page.getByText('Saved', { exact: false }).first().waitFor({ timeout: 10000 }).catch(() => {});
await page.waitForTimeout(1200);
await section.screenshot({ path: join(out, 'after-section.png') });
await page.screenshot({ path: join(out, 'after.png'), fullPage: true });

// The exemption survives a reload (it was saved, not just toggled locally).
await openPii();
result.persisted = await page.getByTestId('pii-provider-exemptions')
  .getByTestId('pii-provider-row').filter({ hasText: 'In-VPC Llama' })
  .getByRole('switch').getAttribute('aria-checked');

// 2. Send the same message through each provider and read what each received.
await fetch(`${mockUrl}/captures`, { method: 'DELETE' });
for (const modelName of ['gpt-public', 'llama-3.3-70b']) {
  await page.goto(`${origin}/`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.waitForTimeout(1500);
  // Pick the model in the composer's model picker.
  await page.getByTestId('model-picker-button').first().click();
  await page.getByTestId(`model-option-${modelName}`).click({ timeout: 8000 });
  await page.keyboard.press('Escape').catch(() => {});
  await page.waitForTimeout(500);
  const box = page.locator('textarea, [contenteditable="true"]').first();
  await box.click();
  await box.type(`Please email ${RAW} the Q3 summary`);
  await page.keyboard.press('Enter');
  await page.waitForURL(/\/reports\//, { timeout: 30000 }).catch(() => {});
  await page.getByText('Noted.', { exact: false }).first().waitFor({ timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(2500);
  await page.screenshot({ path: join(out, `chat-${modelName}.png`) });
}

const seen = await captures();
const byProvider = {};
for (const c of seen) {
  byProvider[c.provider] ||= { calls: 0, raw: 0 };
  byProvider[c.provider].calls += 1;
  if (c.text.includes(RAW)) byProvider[c.provider].raw += 1;
}
result.byProvider = byProvider;
console.log(JSON.stringify(result, null, 2));
await browser.close();

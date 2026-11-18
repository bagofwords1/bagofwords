// UI leg of the PII policy loop
// (docs/feedback-loops/pii-provider-exemption.md).
//
//   node tools/agent/pii_provider_exemption_ui.mjs shot <out.png> [path]
//       sign in, screenshot a page (default Settings → PII Protection);
//       BOW_LOCALE=he for RTL
//   node tools/agent/pii_provider_exemption_ui.mjs flow <out-dir>
//       assign "In-VPC Llama" to Exempt from the PII page, then send the same
//       chat message through Default / Exempt / Strict providers and report
//       what the mock LLM (pii_capture_llm.py) received
//   node tools/agent/pii_provider_exemption_ui.mjs live <out-dir>
//       ask a real data question on Claude Haiku 5.5 (Default policy); the
//       backend's BOW_LLM_TRACE_FILE shows what reached Anthropic
//
// Needs the stack, pii_capture_llm.py and pii_provider_exemption_setup.py.
// Env: BOW_ORIGIN (http://localhost:3000), BOW_EMAIL / BOW_PASSWORD,
// MOCK_URL (http://127.0.0.1:9199), PW_CHROMIUM (/opt/pw-browsers/chromium).
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';

const requireFromFrontend = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = requireFromFrontend('@playwright/test');

const [mode, out, pagePath] = process.argv.slice(2);
if (!['shot', 'flow', 'live'].includes(mode) || !out) {
  console.error('usage: node pii_provider_exemption_ui.mjs shot <out.png> [path] | flow <out-dir> | live <out-dir>');
  process.exit(2);
}
const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
const email = process.env.BOW_EMAIL || 'admin@example.com';
const password = process.env.BOW_PASSWORD || 'Password123!';
const mockUrl = process.env.MOCK_URL || 'http://127.0.0.1:9199';
const RAW = 'jane.roe@contoso.com';
const INSTRUCTION_EMAIL = 'billing-escalations@musicstore.example';

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
  if (process.env.BOW_LOCALE) {
    await page.evaluate((l) => localStorage.setItem('bow.locale', l), process.env.BOW_LOCALE);
  }
}

async function openPage(path = '/settings/pii') {
  await page.goto(`${origin}${path}`, { waitUntil: 'load' }).catch(() => {});
  if (path === '/settings/pii') {
    await page.getByTestId('pii-provider-policies').waitFor({ timeout: 30000 });
  }
  // Reports stream their history in after load.
  await page.waitForTimeout(path.startsWith('/reports/') ? 6000 : 1200);
}

async function waitSaved() {
  await page.waitForResponse((r) => r.url().includes('/api/organization/settings') && r.request().method() === 'PUT', { timeout: 15000 });
  await page.waitForTimeout(800);
}

const providerSelect = (name) =>
  page.getByTestId('pii-provider-row').filter({ hasText: name }).getByTestId('pii-provider-policy');

async function ask(modelId, text, shot) {
  await page.goto(`${origin}/`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.waitForTimeout(1500);
  await page.getByTestId('model-picker-button').first().click();
  await page.getByTestId(`model-option-${modelId}`).click({ timeout: 8000 });
  await page.keyboard.press('Escape').catch(() => {});
  await page.waitForTimeout(400);
  const box = page.locator('textarea, [contenteditable="true"]').first();
  await box.click();
  await box.type(text);
  await page.keyboard.press('Enter');
  await page.waitForURL(/\/reports\//, { timeout: 30000 }).catch(() => {});
  return page;
}

async function captures() {
  return (await fetch(`${mockUrl}/captures`)).json();
}

await signIn();

if (mode === 'shot') {
  mkdirSync(dirname(out), { recursive: true });
  await openPage(pagePath || '/settings/pii');
  await page.screenshot({ path: out, fullPage: true });
  console.log(`captured ${out}`);
  await browser.close();
  process.exit(0);
}

mkdirSync(out, { recursive: true });
const result = {};

if (mode === 'flow') {
  // 1. Assign the in-VPC model to Exempt from the PII page (autosaves).
  await openPage();
  const saved = waitSaved();
  await providerSelect('In-VPC Llama').selectOption('exempt');
  await saved;
  await page.getByTestId('pii-provider-policies').screenshot({ path: join(out, 'after-providers.png') });
  await page.screenshot({ path: join(out, 'after.png'), fullPage: true });

  // 2. The Strict policy's editor.
  await page.getByTestId('pii-policy-strict').click();
  await page.waitForTimeout(600);
  await page.screenshot({ path: join(out, 'after-strict-policy.png'), fullPage: true });

  // 3. The assignment survives a reload (it was saved, not just selected).
  await openPage();
  result.persisted = await providerSelect('In-VPC Llama').inputValue();

  // 4. The LLM page shows each provider's policy.
  await openPage('/settings/models');
  await page.screenshot({ path: join(out, 'llm-badges.png') });

  // 5. The same message through Default, Exempt and Strict providers.
  await fetch(`${mockUrl}/captures`, { method: 'DELETE' });
  const msg = `Please email ${RAW} the Q3 summary`;
  for (const modelId of ['gpt-public', 'llama-3.3-70b', 'gpt-strict']) {
    await ask(modelId, msg);
    if (modelId === 'gpt-strict') {
      await page.getByText('PII protection blocked', { exact: false }).first().waitFor({ timeout: 60000 }).catch(() => {});
    } else {
      await page.getByText('Noted.', { exact: false }).first().waitFor({ timeout: 60000 }).catch(() => {});
    }
    await page.waitForTimeout(2500);
    await page.screenshot({ path: join(out, `chat-${modelId}.png`) });
    if (modelId === 'gpt-strict') {
      result.blockedMessage = (await page.getByText('PII protection blocked', { exact: false }).first().textContent().catch(() => null))?.trim() || null;
      // After a reload the stored error is shown, still localized.
      await page.reload({ waitUntil: 'networkidle' }).catch(() => {});
      await page.waitForTimeout(2500);
      result.blockedAfterReload = await page.getByText('PII protection blocked', { exact: false }).count();
    }
  }

  const seen = await captures();
  const byProvider = {};
  for (const c of seen) {
    byProvider[c.provider] ||= { calls: 0, raw_prompt_email: 0, instruction_email: 0 };
    byProvider[c.provider].calls += 1;
    if (c.text.includes(RAW)) byProvider[c.provider].raw_prompt_email += 1;
    if (c.text.includes(INSTRUCTION_EMAIL)) byProvider[c.provider].instruction_email += 1;
  }
  result.byProvider = byProvider;
}

if (mode === 'live') {
  // A real data question on Claude Haiku 5.5. The customer email arrives in
  // query results (data → redacted); the refund contact is in an org
  // instruction (context → sent as written).
  await ask('claude-haiku-5-5', 'What is the email address of the customer Luís Gonçalves? And who should I contact about a refund?');
  await page.waitForFunction(() => !document.querySelector('[data-testid="stop-button"]'), null, { timeout: 180000 }).catch(() => {});
  await page.waitForTimeout(25000);
  await page.screenshot({ path: join(out, 'live-haiku.png'), fullPage: false });
  result.url = page.url();
}

console.log(JSON.stringify(result, null, 2));
await browser.close();

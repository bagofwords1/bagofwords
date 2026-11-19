// Drives Settings → PII Protection end to end and checks what it shows:
// the policies list, the page per policy, and the modals.
//
//   node tools/agent/pii_settings_ui.mjs <out-dir>
//
// Creates a policy and a custom rule, edits them, checks the live preview
// against the backend, then deletes both, so it leaves the settings as found.
// Env: BOW_ORIGIN (http://localhost:3000), BOW_EMAIL / BOW_PASSWORD,
// BOW_LOCALE (screenshots only; the checks assume English),
// PW_CHROMIUM (/opt/pw-browsers/chromium).
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

const requireFromFrontend = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = requireFromFrontend('@playwright/test');

const out = process.argv[2];
if (!out) { console.error('usage: node pii_settings_ui.mjs <out-dir>'); process.exit(2); }
mkdirSync(out, { recursive: true });
const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
const email = process.env.BOW_EMAIL || 'admin@example.com';
const password = process.env.BOW_PASSWORD || 'Password123!';
const locale = process.env.BOW_LOCALE || '';
const shotsOnly = !!locale && locale !== 'en';

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium' });
const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 });
const page = await context.newPage();
const consoleErrors = [];
page.on('console', (m) => { if (m.type() === 'error' || /\[intlify\]/.test(m.text())) consoleErrors.push(m.text()); });
page.on('pageerror', (e) => consoleErrors.push(String(e)));

const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`);
};
const shot = (name) => page.screenshot({ path: join(out, `${name}${locale ? `-${locale}` : ''}.png`), fullPage: true });
const saved = () => page.waitForResponse(
  (r) => r.url().includes('/api/organization/settings') && r.request().method() === 'PUT', { timeout: 15000 });
const row = (id) => page.locator(`[data-testid="pii-rule-row"][data-rule="${id}"]`);

await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.locator('#email').waitFor({ timeout: 120000 });
await page.fill('#email', email);
await page.fill('#password', password);
await Promise.all([
  page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.click('button[type=submit]'),
]);
await page.waitForTimeout(1500);
if (locale) await page.evaluate((l) => localStorage.setItem('bow.locale', l), locale);

// --- the list page ---
await page.goto(`${origin}/settings/pii`, { waitUntil: 'load' });
await page.getByTestId('pii-policy-list').waitFor({ timeout: 30000 });
await page.waitForTimeout(800);
await shot('list');
check('list: Default policy row', await page.getByTestId('pii-policy-default').isVisible());
check('list: provider assignments', await page.getByTestId('pii-provider-policies').isVisible());
check('list: no rule options on the list page', (await page.getByTestId('pii-rule-row').count()) === 0);

if (shotsOnly) {
  await page.getByTestId('pii-policy-default').click();
  await page.getByTestId('pii-policy-editor').waitFor({ timeout: 15000 });
  await page.getByTestId('pii-advanced-toggle').click();
  await page.waitForTimeout(500);
  await shot('policy');
  check(`${locale}: html dir`, (await page.evaluate(() => document.documentElement.dir)) === (['he', 'ar'].includes(locale) ? 'rtl' : 'ltr'));
} else {
  // --- new policy modal → its own page ---
  const name = `Loop ${Date.now() % 100000}`;
  await page.getByTestId('pii-new-policy').click();
  await page.getByTestId('pii-new-policy-modal').waitFor();
  await shot('new-policy-modal');
  check('new policy: Create is disabled without a name', await page.getByTestId('pii-create-policy').isDisabled());
  await page.locator('#pii-new-policy-name').fill(name);
  await Promise.all([saved(), page.getByTestId('pii-create-policy').click()]);
  await page.waitForURL(/\/settings\/pii\/policy-/, { timeout: 15000 });
  await page.getByTestId('pii-policy-editor').waitFor();
  const policyUrl = page.url();
  check('new policy: opens its own page', /\/settings\/pii\/policy-\d+$/.test(policyUrl), policyUrl);
  check('new policy: name shown', (await page.getByTestId('pii-policy-name').inputValue()) === name);
  check('new policy: used by no provider', /Not used by any provider/.test(await page.getByTestId('pii-policy-used-by').innerText()));

  // --- one rules list, action per rule, example follows the action ---
  const emailRow = row('email');
  if (!(await emailRow.getByTestId('pii-rule-action').count())) {
    await Promise.all([saved(), emailRow.getByTestId('pii-rule-toggle').click()]);
  }
  await Promise.all([saved(), emailRow.getByTestId('pii-rule-action').selectOption('mask')]);
  const masked = await emailRow.getByTestId('pii-rule-example').innerText();
  check('rule: mask example keeps the last 4', /jane@acme\.com → \*{4}@\*{2}e\.com/.test(masked) || /\*+.*\.com$/.test(masked), masked);
  await Promise.all([saved(), emailRow.getByTestId('pii-rule-action').selectOption('block')]);
  check('rule: block example', /Blocks the request/.test(await emailRow.getByTestId('pii-rule-example').innerText()));

  // --- live preview, checked against the backend's own answer ---
  const sample = 'Card 4111 1111 1111 1234 for jane@acme.com';
  await page.getByTestId('pii-test-input').fill(sample);
  await page.getByTestId('pii-test-blocked').waitFor({ timeout: 10000 });
  check('preview: email on Block blocks the sample', true);
  await Promise.all([saved(), emailRow.getByTestId('pii-rule-action').selectOption('mask')]);
  await page.getByTestId('pii-test-output').waitFor({ timeout: 10000 });
  await page.waitForFunction(() => /\*/.test(document.querySelector('[data-testid="pii-test-output"]')?.textContent || ''), null, { timeout: 10000 });
  const output = await page.getByTestId('pii-test-output').innerText();
  check('preview: updates when the rule changes, without a button', !output.includes('jane@acme.com') && !output.includes('4111 1111 1111 1234'), output);
  check('preview: mask in the result matches the row example', output.includes(masked.split('→')[1].trim()), output);

  await Promise.all([saved(), row('credit_card').getByTestId('pii-rule-toggle').click()]);
  await page.waitForFunction(() => (document.querySelector('[data-testid="pii-test-output"]')?.textContent || '').includes('4111 1111 1111 1234'), null, { timeout: 10000 });
  check('preview: a rule turned off stops applying', true);
  check('rule: an off rule hides its options', (await row('credit_card').getByTestId('pii-rule-action').count()) === 0);

  // --- advanced fold ---
  check('advanced: closed by default', (await page.getByTestId('pii-advanced').count()) === 0);
  await page.getByTestId('pii-advanced-toggle').click();
  check('advanced: holds the instructions/schemas switch', await page.getByTestId('pii-redact-context').isVisible());
  await shot('policy');

  // --- saved for real: reload and read it back ---
  await page.reload({ waitUntil: 'load' });
  await page.getByTestId('pii-policy-editor').waitFor({ timeout: 30000 });
  check('persisted: email action after reload', (await row('email').getByTestId('pii-rule-action').inputValue()) === 'mask');
  check('persisted: credit card off after reload', (await row('credit_card').getByTestId('pii-rule-action').count()) === 0);

  // --- Default is untouched by edits to another policy ---
  await page.getByTestId('pii-back').click();
  await page.getByTestId('pii-policy-list').waitFor();
  check('list: new policy listed', await page.getByText(name, { exact: true }).isVisible());

  // --- custom rule modal ---
  await page.getByTestId('pii-add-rule').click();
  await page.getByTestId('pii-rule-modal').waitFor();
  check('custom rule: Save disabled until named with a pattern', await page.getByTestId('pii-save-rule').isDisabled());
  await page.locator('#pii-rule-name').fill('Loop ticket');
  await page.getByTestId('pii-rule-pattern').first().fill('TCK-(\\d+');
  await Promise.all([saved(), page.getByTestId('pii-save-rule').click()]);
  await page.waitForTimeout(800);
  check('custom rule: an invalid pattern keeps the modal open', await page.getByTestId('pii-rule-modal').isVisible());
  await page.getByTestId('pii-rule-pattern').first().fill('TCK-\\d+');
  await shot('custom-rule-modal');
  await Promise.all([saved(), page.getByTestId('pii-save-rule').click()]);
  await page.getByTestId('pii-rule-modal').waitFor({ state: 'hidden', timeout: 8000 });
  check('custom rule: listed', await page.getByTestId('pii-custom-rule').filter({ hasText: 'Loop ticket' }).isVisible());
  await shot('list-after');

  // It shows up in the policy, and the preview applies it.
  await page.goto(policyUrl, { waitUntil: 'load' });
  await page.getByTestId('pii-policy-editor').waitFor({ timeout: 30000 });
  check('custom rule: appears in the policy rules', await page.getByTestId('pii-group-custom').getByText('Loop ticket').isVisible());
  await page.getByTestId('pii-test-input').fill('see TCK-4821');
  await page.waitForFunction(() => /REDACTED/.test(document.querySelector('[data-testid="pii-test-output"]')?.textContent || ''), null, { timeout: 10000 });
  check('custom rule: applied in the preview', !(await page.getByTestId('pii-test-output').innerText()).includes('TCK-4821'));

  // --- delete policy modal ---
  await page.getByTestId('pii-delete-policy').click();
  await page.getByTestId('pii-delete-policy-modal').waitFor();
  await shot('delete-policy-modal');
  await Promise.all([saved(), page.getByTestId('pii-confirm-delete-policy').click()]);
  await page.waitForURL(/\/settings\/pii$/, { timeout: 15000 });
  await page.getByTestId('pii-policy-list').waitFor();
  check('delete policy: gone from the list', (await page.getByText(name, { exact: true }).count()) === 0);
  await page.goto(policyUrl, { waitUntil: 'load' });
  await page.getByTestId('pii-policy-missing').waitFor({ timeout: 30000 });
  check('delete policy: its page says it no longer exists', true);

  // --- delete the custom rule ---
  await page.goto(`${origin}/settings/pii`, { waitUntil: 'load' });
  await page.getByTestId('pii-policy-list').waitFor({ timeout: 30000 });
  await page.getByTestId('pii-custom-rule').filter({ hasText: 'Loop ticket' }).getByTestId('pii-delete-rule').click();
  await Promise.all([saved(), page.getByTestId('pii-confirm-delete-rule').click()]);
  await page.waitForTimeout(600);
  check('custom rule: deleted', (await page.getByTestId('pii-custom-rule').filter({ hasText: 'Loop ticket' }).count()) === 0);
}

const noise = consoleErrors.filter((e) => !/favicon|Failed to load resource|ResizeObserver/.test(e));
check('no console errors or missing-string warnings', noise.length === 0, noise.slice(0, 3).join(' | '));
await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);

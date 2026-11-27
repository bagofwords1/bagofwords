// Screenshots of the Data retention settings (Settings → AI settings), for
// the retention feedback loop: the section open, en and he.
//
//   cd frontend
//   node ../tools/agent/retention_ui.mjs <out-dir> <prefix> [--section retention|workspace] [--set 30]
//
// --set N types N into the report and dashboard retention fields (like an
// admin would: type, then leave the field), waits for the save and the
// preview line, and shoots again.
// Env: BOW_EMAIL, BOW_PASSWORD, BOW_ORIGIN (default http://localhost:3000)
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';

// Resolve playwright from where the script is run (frontend/), not from tools/agent/.
const { chromium } = createRequire(`${process.cwd()}/`)('playwright');

const [outDir, prefix, ...rest] = process.argv.slice(2);
const opt = (k, d) => (rest.includes(k) ? rest[rest.indexOf(k) + 1] : d);
const section = opt('--section', 'retention');
const setDays = opt('--set', null);
const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
const email = process.env.BOW_EMAIL || 'admin@example.com';
const password = process.env.BOW_PASSWORD || 'Password123!';
mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch().catch(() => chromium.launch({ executablePath: '/opt/pw-browsers/chromium' }));
for (const locale of ['en', 'he']) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 });
  await context.addInitScript(([loc, sec]) => {
    try {
      localStorage.setItem('bow.locale', loc);
      localStorage.setItem('bow.aiSettings.openSections', JSON.stringify([sec]));
    } catch {}
  }, [locale, section]);
  const page = await context.newPage();
  // Hide the Nuxt devtools badge from the shots.
  page.on('load', () => page.addStyleTag({ content: '#nuxt-devtools-container{display:none!important}' }).catch(() => {}));
  await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.fill('#email', email);
  await page.fill('#password', password);
  await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}), page.click('button[type=submit]')]);
  await page.waitForTimeout(1500);
  if (page.url().includes('/onboarding')) {
    try { await page.getByText('Skip onboarding', { exact: false }).click({ timeout: 4000 }); await page.waitForTimeout(1500); } catch {}
  }
  await page.goto(`${origin}/settings/ai_settings`, { waitUntil: 'load', timeout: 120000 });
  const box = page.locator(`[data-testid="ai-section-${section}"]`);
  await box.waitFor({ timeout: 90000 });
  if (!(await box.locator('[aria-expanded="true"]').count())) await box.locator('button').first().click();
  await page.waitForTimeout(800);
  await box.scrollIntoViewIfNeeded();
  await box.screenshot({ path: `${outDir}/${prefix}-${locale}.png` });
  if (setDays && locale === 'en') {
    for (const key of ['report_retention_days', 'artifact_retention_days']) {
      const input = page.locator(`[data-testid="retention-input-${key}"] input, input[data-testid="retention-input-${key}"]`).first();
      await input.fill(String(setDays));
      await input.press('Enter');
      await page.waitForResponse(r => r.url().includes('/api/organization/settings') && r.request().method() === 'PUT');
      await page.waitForResponse(r => r.url().includes('/api/organization/retention/preview'));
    }
    await page.waitForTimeout(7000); // let the save toasts clear
    await box.screenshot({ path: `${outDir}/${prefix}-set-${locale}.png` });
    console.log('preview line:', await page.locator('[data-testid="retention-preview"]').innerText().catch(() => '(none)'));
  }
  console.log('shot', locale, section);
  await context.close();
}
await browser.close();

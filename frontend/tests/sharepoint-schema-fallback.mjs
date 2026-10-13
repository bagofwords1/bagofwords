// Seed with tools/agent/seed_org.py. Only reads connection-field metadata.
// BOW_QA_SEED_FILE=/path/to/seed.json BOW_QA_BASE_URL=http://localhost:3011 node tests/sharepoint-schema-fallback.mjs
import { chromium, expect } from '@playwright/test';
import fs from 'node:fs';
const baseURL = process.env.BOW_QA_BASE_URL || 'http://localhost:3000';
const seed = JSON.parse(fs.readFileSync(process.env.BOW_QA_SEED_FILE));
const before = process.env.EXPECT_MISSING_LABELS === '1';
const browser = await chromium.launch();
try {
  for (const locale of ['en', 'he']) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const login = await context.request.post(`${baseURL}/api/auth/jwt/login`, {
      form: { username: seed.admin.email, password: seed.admin.password },
    });
    expect(login.ok()).toBeTruthy();
    const { access_token } = await login.json();
    await context.addCookies([{ name: 'auth.token', value: access_token, url: baseURL }]);
    await context.addInitScript(({ id, locale }) => {
      localStorage.setItem('bow.selectedOrganizationId', id);
      localStorage.setItem('bow.locale', locale);
    }, { id: seed.organization.id, locale });
    const page = await context.newPage();
    await page.route('**/data_sources/sharepoint_onprem/fields?*', async route => {
      const response = await route.fetch();
      const json = await response.json();
      // A backend schema may precede its translation catalog's deployment.
      const field = json.config.properties.api_mode;
      field['ui:titleKey'] = 'data.unreleasedApi.title';
      field['ui:descriptionKey'] = 'data.unreleasedApi.description';
      field['ui:enumLabelKeys']['2010'] = 'data.unreleasedApi.legacy';
      await route.fulfill({ response, json });
    });
    await page.goto(`${baseURL}/agents/new`);
    await page.getByRole('button', { name: 'Create new connection' }).click();
    await page.getByRole('button', { name: /SharePoint Server \(on-prem\)/ }).click();
    await page.locator('#api_mode').selectOption('2010');
    await expect(page.locator('#api_mode option:checked')).toHaveText(before ? 'data.unreleasedApi.legacy' : 'SharePoint 2010');
    await expect(page.getByText(before ? 'data.unreleasedApi.title' : 'SharePoint API', { exact: true })).toBeVisible();
    if (!before) await expect(page.getByText('data.unreleasedApi.description', { exact: true })).toHaveCount(0);
    await expect(page.locator('html')).toHaveAttribute('dir', locale === 'he' ? 'rtl' : 'ltr');
    if (process.env.BOW_QA_EVIDENCE_DIR) {
      await page.screenshot({ path: `${process.env.BOW_QA_EVIDENCE_DIR}/fallback-${before ? 'before' : 'after'}-${locale}.png` });
    }
    await context.close();
  }
  console.log(`Schema translation fallback: ${before ? 'reproduced missing labels' : 'passed'} (en, he)`);
} finally {
  await browser.close();
}

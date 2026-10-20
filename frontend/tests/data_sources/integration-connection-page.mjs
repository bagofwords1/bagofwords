// Live check for /integrations/<id>/connection against a seeded stack
// (tools/agent/seed_org.py --demo --invite viewer@example.com).
// usage: BASE=http://localhost:3000 DS_ID=<demo data source id> node tests/data_sources/integration-connection-page.mjs
// Regression: the page injected its data from a removed `integrations` layout
// and sat on "Loading..." forever for every role.
import { chromium, expect } from '@playwright/test';
const BASE = process.env.BASE || 'http://localhost:3000';
const DS = process.env.DS_ID;
if (!DS) throw Error('DS_ID is required');
const MISSING = '00000000-0000-0000-0000-000000000000';

async function asUser(browser, email, fn) {
  const page = await (await browser.newContext()).newPage();
  const calls = [];
  page.on('response', (r) => { const u = new URL(r.url()); if (u.pathname.startsWith('/api/')) calls.push(`${r.status()} ${u.pathname}`); });
  await page.goto(`${BASE}/users/sign-in`);
  await page.fill('#email', email);
  await page.fill('#password', 'Password123!');
  await page.click('button[type=submit]');
  await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
  try { await fn(page, calls); } finally { await page.context().close(); }
}

const browser = await chromium.launch();
try {
  await asUser(browser, 'admin@example.com', async (page, calls) => {
    await page.goto(`${BASE}/integrations/${DS}/connection`);
    await expect(page.getByTestId('connection-loading')).toHaveCount(0, { timeout: 30000 });
    await expect(page.getByTestId('connection-edit')).toBeVisible();
    await page.getByTestId('connection-edit').click();
    await expect(page.getByText('Edit Connection')).toBeVisible();
    await expect.poll(() => calls.some((c) => /^200 \/api\/connections\/[^/]+$/.test(c))).toBe(true);
    await page.goto(`${BASE}/integrations/${MISSING}/connection`);
    await expect(page.getByTestId('connection-error')).toBeVisible({ timeout: 30000 });
  });
  await asUser(browser, 'viewer@example.com', async (page, calls) => {
    await page.goto(`${BASE}/integrations/${DS}/connection`);
    await expect(page.getByTestId('connection-loading')).toHaveCount(0, { timeout: 30000 });
    await expect(page.getByText('sqlite', { exact: true })).toBeVisible();
    await expect(page.getByTestId('connection-edit')).toHaveCount(0);
    if (calls.some((c) => c.includes('/api/connections/'))) throw Error('viewer requested manage-only /connections/{id}');
    await page.goto(`${BASE}/integrations/${MISSING}/connection`);
    await expect(page.getByTestId('connection-error')).toBeVisible({ timeout: 30000 });
  });
  console.log('PASS: page loads for manager (edit + /connections/{id}) and viewer (no edit, no config fetch); 403/404 show an error, not a spinner');
} finally {
  await browser.close();
}

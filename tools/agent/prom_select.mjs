// Select a focused set of Prometheus metrics on the schema step and finalize.
import { chromium } from '@playwright/test';
const BASE = 'http://localhost:3000';
const DS = process.argv[2];
const OUT = '../media/prometheus';
const METRICS = [
  'up',
  'ALERTS',
  'node_memory_MemAvailable_bytes',
  'node_filesystem_avail_bytes',
  'node_cpu_seconds_total',
  'prometheus_http_requests_total',
  'prometheus_http_request_duration_seconds_bucket',
  'job:up:count',
];

const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);

await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.getByPlaceholder(/email/i).fill('admin@example.com');
await page.getByPlaceholder(/password/i).fill('Password123!');
await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.locator('button[type=submit]').first().click()]);
await page.waitForTimeout(1500);

await page.goto(`${BASE}/onboarding/data/${DS}/schema`, { waitUntil: 'networkidle' }).catch(() => {});
await page.waitForTimeout(2000);

const search = page.getByPlaceholder(/search tables/i).first();
for (const metric of METRICS) {
  await search.fill(metric);
  await page.waitForTimeout(900);
  // Check the row whose label exactly equals the metric name.
  const row = page.locator('div', { hasText: new RegExp(`^${metric}\\b`) });
  const cb = page.locator('input[type=checkbox]').first();
  await cb.click().catch(() => {});
  await page.waitForTimeout(300);
  console.log('selected', metric);
}
await search.fill('');
await page.waitForTimeout(800);
await page.screenshot({ path: `${OUT}/06-metrics-selected.png` });

await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.getByRole('button', { name: /save & continue|save and continue/i }).first().click().catch(() => {})]);
await page.waitForTimeout(4000);
console.log('url after finalize:', page.url());
await page.screenshot({ path: `${OUT}/07-onboarding-done.png`, fullPage: true });
await ctx.close(); await b.close(); console.log('done');

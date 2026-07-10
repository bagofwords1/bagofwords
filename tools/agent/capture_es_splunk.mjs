// Capture the Elasticsearch + Splunk connectors end-to-end in the running app.
// Usage: node capture_es_splunk.mjs
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = '../media/es-splunk';
const BASE = 'http://localhost:3000';
mkdirSync(OUT, { recursive: true });

const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
const ctx = await b.newContext({ viewport: { width: 1512, height: 1000 } });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const shot = async (n) => { await page.screenshot({ path: `${OUT}/${n}.png` }); console.log('shot', n); };

// ---- login ----
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.getByPlaceholder(/email/i).fill('admin@example.com');
await page.getByPlaceholder(/password/i).fill('Password123!');
await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.locator('button[type=submit]').first().click()]);
await page.waitForTimeout(2500);

// ---- catalog: the connector picker (both connectors, icons, enterprise badge) ----
await page.goto(`${BASE}/agents/new`, { waitUntil: 'networkidle' }).catch(() => {});
await page.waitForTimeout(2500);
await shot('01-catalog');
// search "elastic"
try {
  const s = page.getByPlaceholder(/search/i).first();
  await s.fill('elastic'); await page.waitForTimeout(1200); await shot('02-catalog-elastic');
  await s.fill('splunk'); await page.waitForTimeout(1200); await shot('03-catalog-splunk');
  await s.fill(''); await page.waitForTimeout(500);
} catch (e) { console.log('search box note', e.message); }

// ---- the data source list (created connections) ----
await page.goto(`${BASE}/agents`, { waitUntil: 'networkidle' }).catch(() => {});
await page.waitForTimeout(2500);
await shot('04-datasources-list');

// ---- open each data source to show its tables (schema) ----
for (const [name, tag] of [[/elasticsearch/i, 'elastic'], [/splunk/i, 'splunk']]) {
  await page.goto(`${BASE}/agents`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.waitForTimeout(1500);
  try {
    await page.getByText(name).first().click({ timeout: 5000 });
    await page.waitForTimeout(3000);
    await shot(`05-${tag}-overview`);
    // try to reach the schema/tables tab
    for (const re of [/schema/i, /tables/i, /data/i]) {
      const tab = page.getByRole('link', { name: re }).first().or(page.getByText(re).first());
      try { await tab.click({ timeout: 2500 }); await page.waitForTimeout(2500); break; } catch {}
    }
    await shot(`06-${tag}-schema`);
    await page.screenshot({ path: `${OUT}/06-${tag}-schema-full.png`, fullPage: true });
  } catch (e) { console.log(`open ${tag} note`, e.message); }
}

await ctx.close(); await b.close(); console.log('CATALOG DONE');

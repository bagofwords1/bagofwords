// Part 3: revisit the SAME wizard tables step after sign-in — tables now appear.
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = require('@playwright/test');

const BASE = 'http://localhost:3000';
const API = 'http://localhost:8000';
const MEDIA = '/tmp/bow-agent/obo-repro-media';
const DS_ID = process.env.DS_ID;

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const shot = (n) => page.screenshot({ path: `${MEDIA}/${n}.png` }).then(() => console.log(`   [shot] ${n}`));

const login = await fetch(`${API}/api/auth/jwt/login`, {
  method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  body: new URLSearchParams({ username: 'admin@example.com', password: 'Password123!' }),
});
const apiToken = (await login.json()).access_token;
const orgs = await (await fetch(`${API}/api/organizations`, { headers: { Authorization: `Bearer ${apiToken}` } })).json();
const H = { Authorization: `Bearer ${apiToken}`, 'X-Organization-Id': orgs[0].id };

await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
await page.fill('#email', 'admin@example.com');
await page.fill('#password', 'Password123!');
await page.click('button[type="submit"]');
await page.waitForTimeout(4000);

console.log('>> same wizard tables step, after sign-in');
await page.goto(`${BASE}/agents/new/${DS_ID}/schema`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(5000);
await shot('18-tables-step-after-signin');

// select all + save, then agent page shows the tables
const selectAll = page.getByRole('button', { name: /select all/i }).first();
if (await selectAll.count()) { await selectAll.click(); await page.waitForTimeout(1000); }
await shot('19-tables-selected');
await page.getByRole('button', { name: /save & continue/i }).first().click();
await page.waitForTimeout(5000);

await page.goto(`${BASE}/agents/${DS_ID}`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(6000);
// expand Tables tree group
const tablesRow = page.getByText(/^Tables$/).first();
if (await tablesRow.count()) { await tablesRow.click().catch(() => {}); await page.waitForTimeout(2500); }
await shot('20-agent-page-tables-visible');

const pag = await (await fetch(`${API}/api/data_sources/${DS_ID}/full_schema?page=1&page_size=50`, { headers: H })).json();
console.log('   full_schema for admin now: total_tables=', pag.total_tables, 'rows=', (pag.tables || []).length);
console.log('   names:', (pag.tables || []).map((t) => t.name).join(' | '));
await browser.close();
console.log('DONE');

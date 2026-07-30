// End-to-end UI verification of the SAP BusinessObjects and SAP BW (XMLA)
// connectors, driven through the real app against the mock SAP servers
// (tools/agent/mock_sap_servers.py on :6405 and :8410).
//
// Proves, with screenshots into OUT:
//   1. Both connectors appear in the Add-Connection catalog with the SAP icon.
//   2. Their schema-generated connect forms render the right fields.
//   3. Test Connection succeeds against the mock servers (logon + discovery).
//
// Usage: node tools/agent/sap_connectors_e2e.mjs
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.env.OUT || '../media/sap-connectors';
const BASE = 'http://localhost:3000';
const EMAIL = process.env.BOW_ADMIN_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_ADMIN_PASSWORD || 'Password123!';
mkdirSync(OUT, { recursive: true });

const CHROME = '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const b = await chromium.launch({ executablePath: CHROME });
const ctx = await b.newContext({ viewport: { width: 1512, height: 1000 } });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const shot = async (n) => { await page.screenshot({ path: `${OUT}/${n}.png`, fullPage: false }); console.log('shot', n); };

const results = {};

// ---- login ----
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'commit' });
await page.getByPlaceholder(/email/i).fill(EMAIL);
await page.getByPlaceholder(/password/i).fill(PASSWORD);
await Promise.all([
  page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2000);
try { await page.getByText(/skip onboarding/i).click({ timeout: 4000 }); await page.waitForTimeout(1200); } catch {}

async function openCatalogAndSearch() {
  await page.goto(`${BASE}/agents/new?mode=new_connection`, { waitUntil: 'commit' });
  await page.waitForTimeout(2500);
  try { await page.getByText(/skip onboarding/i).click({ timeout: 3000 }); await page.waitForTimeout(1200);
        await page.goto(`${BASE}/agents/new?mode=new_connection`, { waitUntil: 'commit' }); await page.waitForTimeout(2000); } catch {}
  const search = page.getByPlaceholder(/search/i).first();
  await search.fill('sap');
  await page.waitForTimeout(1000);
}

// Return the icon <img> src used by the tile whose label matches `label`.
async function tileIconSrc(label) {
  return await page.evaluate((lbl) => {
    const els = Array.from(document.querySelectorAll('*'))
      .filter((e) => e.children.length === 0 && e.textContent.trim() === lbl);
    for (const el of els) {
      let node = el;
      for (let i = 0; i < 6 && node; i++) {
        const img = node.querySelector && node.querySelector('img');
        if (img) return img.getAttribute('src');
        node = node.parentElement;
      }
    }
    return null;
  }, label);
}

// ---- catalog: tiles + SAP icon ----
await openCatalogAndSearch();
await shot('01-catalog-sap-search');

for (const label of ['SAP BusinessObjects', 'SAP BW (XMLA)']) {
  const visible = await page.getByText(label, { exact: true }).first().isVisible().catch(() => false);
  const iconSrc = await tileIconSrc(label);
  results[label] = { tileVisible: visible, iconSrc };
  console.log(`TILE ${label}: visible=${visible} icon=${iconSrc}`);
}

// ---- BusinessObjects connect form + Test Connection ----
async function driveConnector({ label, name, host, fields, expect }) {
  await openCatalogAndSearch();
  await page.getByText(label, { exact: true }).first().click();
  await page.waitForTimeout(1500);
  await shot(`${name}-form-empty`);

  await page.locator('input[placeholder*="Sales DB"]').first().fill(`${label} (mock)`);
  await page.locator('#host').fill(host);
  for (const [id, val] of Object.entries(fields)) {
    const loc = page.locator(`#${id}`);
    if (await loc.count()) await loc.first().fill(val);
  }
  await shot(`${name}-form-filled`);

  await page.getByRole('button', { name: /test/i }).first().click();
  await page.waitForTimeout(6000);
  await shot(`${name}-test-result`);
  const body = await page.locator('body').innerText();
  const m = body.match(new RegExp(expect, 'i'));
  results[label].testMessage = m ? m[0] : '(not found)';
  results[label].testPassed = !!m;
  console.log(`TEST ${label}: ${results[label].testMessage}`);
}

await driveConnector({
  label: 'SAP BusinessObjects',
  name: 'bo',
  host: 'http://localhost:6405',
  fields: { username: 'Administrator', password: 'x', auth_type: 'secEnterprise' },
  expect: 'Connected successfully[^\\n]*',
});

await driveConnector({
  label: 'SAP BW (XMLA)',
  name: 'bw',
  host: 'http://localhost:8410',
  fields: { username: 'BWUSER', password: 'x' },
  expect: 'Connected successfully[^\\n]*',
});

console.log('\n==== SUMMARY ====');
console.log(JSON.stringify(results, null, 2));

// Non-zero exit if any core assertion failed, so the loop fails loudly.
const ok = ['SAP BusinessObjects', 'SAP BW (XMLA)'].every((l) =>
  results[l].tileVisible && (results[l].iconSrc || '').includes('sap_datasphere') && results[l].testPassed
);
await b.close();
console.log(ok ? 'E2E PASS' : 'E2E FAIL');
process.exit(ok ? 0 : 1);

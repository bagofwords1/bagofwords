// Verify the assistant avatar's LLM badge shows on a LIVE (just-streamed) message
// — not only after a full page reload — when Auto / the model router is selected.
//
// Fresh report, one message, no reload. Asserts the last assistant message's
// avatar carries a /llm_providers_icons/*.png badge.
//
//   cd frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
//     node ../tools/agent/verify_live_model_badge.mjs
import { chromium } from '@playwright/test';

const BASE = 'http://localhost:3000';
const EMAIL = 'admin@example.com';
const PASSWORD = 'Password123!';
const DS_ID = process.env.DS_ID || 'ff8a6539-4dfd-4fff-a373-7aaa490681c0';
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
const p = await ctx.newPage();

await p.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
await p.fill('#email', EMAIL); await p.fill('#password', PASSWORD);
await p.click('button[type="submit"]');
await p.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });

const token = await p.evaluate(async (c) => (await (await fetch('/api/auth/jwt/login', {
  method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  body: `username=${encodeURIComponent(c.e)}&password=${encodeURIComponent(c.p)}`,
})).json()).access_token, { e: EMAIL, p: PASSWORD });
const orgId = await p.evaluate(async (t) => (await (await fetch('/api/organizations', { headers: { Authorization: `Bearer ${t}` } })).json())[0].id, token);
// Ensure Auto router is on so the placeholder is created with model_id=null.
await p.evaluate(async ({ t, org }) => fetch('/api/organization/settings', {
  method: 'PUT', headers: { Authorization: `Bearer ${t}`, 'X-Organization-Id': org, 'Content-Type': 'application/json' },
  body: JSON.stringify({ config: { model_routing: { value: true } } }),
}), { t: token, org: orgId });
const report = await p.evaluate(async ({ t, org, ds }) => (await (await fetch('/api/reports', {
  method: 'POST', headers: { Authorization: `Bearer ${t}`, 'X-Organization-Id': org, 'Content-Type': 'application/json' },
  body: JSON.stringify({ title: 'live-badge', files: [], data_sources: [ds] }),
})).json()), { t: token, org: orgId, ds: DS_ID });
log('fresh report', report.id);

await p.goto(`${BASE}/reports/${report.id}`, { waitUntil: 'domcontentloaded' });
const input = p.locator('div.mention-input-field[contenteditable="true"]').first();
await input.waitFor({ state: 'visible', timeout: 60000 });
await p.waitForTimeout(2500);
await input.click(); await p.keyboard.type('hi'); await p.waitForTimeout(300); await input.press('Enter');
log('submitted "hi" (Auto)');

// Wait for the run to finish — no reload.
await p.locator('.thinking-shimmer').first().waitFor({ state: 'visible', timeout: 60000 }).catch(() => {});
await p.locator('.thinking-shimmer').first().waitFor({ state: 'detached', timeout: 240000 }).catch(() => {});
await p.waitForTimeout(1500);

const badges = await p.locator('img[src*="llm_providers_icons"]').all();
log('LLM badges on the live message (no reload):', badges.length);
for (const ic of badges) log('  src=', await ic.getAttribute('src'));
await p.screenshot({ path: `${process.env.EVIDENCE_DIR || '../media/pr/promptboxv2-home-error'}/live-badge.png` });

if (badges.length >= 1) log('PASS: assistant LLM badge shows on the live message without a reload');
else { log('FAIL: no LLM badge on the live message'); process.exitCode = 3; }
await b.close();

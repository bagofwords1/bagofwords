// One framing the full flow misses: the user's prompt and the launched
// sub-agent cards in one frame, with the symptom widget folded.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';
const OUT = process.env.EVIDENCE_DIR || '../media/pr/sub-agents';
mkdirSync(OUT, { recursive: true });
const BASE = process.env.BOW_FRONTEND_URL || 'http://localhost:3000';
const EMAIL = process.env.BOW_ADMIN_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_ADMIN_PASSWORD || 'Password123!';
const PROMPT = 'Paid orders dropped about 15% since mid-September. Why? Test the likely causes separately and tell me which one it is, with evidence, and which ones you ruled out.';
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium' });
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
await page.fill('#email', EMAIL); await page.fill('#password', PASSWORD); await page.click('button[type="submit"]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
const token = await page.evaluate(async (c) => (await (await fetch('/api/auth/jwt/login', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: `username=${encodeURIComponent(c.email)}&password=${encodeURIComponent(c.password)}` })).json()).access_token, { email: EMAIL, password: PASSWORD });
const orgId = await page.evaluate(async (t) => (await (await fetch('/api/organizations', { headers: { Authorization: `Bearer ${t}` } })).json())[0].id, token);
const api = (path, init = {}) => page.evaluate(async ({ t, org, path, init }) => { const r = await fetch(path, { ...init, headers: { Authorization: `Bearer ${t}`, 'X-Organization-Id': org, 'Content-Type': 'application/json' } }); return r.json(); }, { t: token, org: orgId, path, init });
const ds = (await api('/api/data_sources')).find((d) => d.name === 'Checkout Ops');
const report = await api('/api/reports', { method: 'POST', body: JSON.stringify({ title: 'Why did paid orders drop?', files: [], data_sources: [ds.id] }) });
log('report', report.id);
await page.goto(`${BASE}/reports/${report.id}`, { waitUntil: 'domcontentloaded' });
const input = page.locator('div.mention-input-field[contenteditable="true"]').first();
await input.waitFor({ state: 'visible', timeout: 120000 }); await page.waitForTimeout(2000);
await input.fill(PROMPT); await input.press('Enter');
await page.locator('[data-testid^="delegate-card-"]').first().waitFor({ state: 'visible', timeout: 300000 });
// The planner streams its delegate calls one by one; the wave line appears
// once the turn ended with the whole wave launched. Wait for it (or settle).
await page.locator('text=/Wave 1/').first().waitFor({ state: 'visible', timeout: 90000 }).catch(() => log('wave line not seen in 90s; shooting anyway'));
await page.waitForTimeout(2000);
// Fold the symptom widget (its header toggles collapse) so the cards sit
// right under the prompt; the Created Data tool row is collapsed by default.
try { await page.locator('.widget-header').first().click({ timeout: 5000 }); await page.waitForTimeout(800); } catch (e) { log('widget collapse skipped:', e.message.split('\n')[0]); }
await page.locator('text=' + PROMPT.slice(0, 40)).first().scrollIntoViewIfNeeded();
await page.waitForTimeout(800);
const cards = await page.locator('[data-testid^="delegate-card-"]').count();
await page.screenshot({ path: `${OUT}/13-prompt-and-sub-agent-launch.png` });
log(`📸 ${OUT}/13-prompt-and-sub-agent-launch.png — prompt + ${cards} sub-agent cards launched`);
await browser.close();

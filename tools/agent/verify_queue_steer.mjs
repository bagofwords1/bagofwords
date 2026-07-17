// End-to-end UI verification of PromptBoxV2 queue + steer.
// Drives the real stack (backend :8000, frontend :3000, real Anthropic model)
// and captures one screenshot per stage into media/pr/promptboxv2-queue-steering/.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.env.EVIDENCE_DIR || "../media/pr/promptboxv2-queue-steering";
mkdirSync(OUT, { recursive: true });

const BASE = 'http://localhost:3000';
const API = 'http://localhost:8000';
const EMAIL = 'admin@example.com';
const PASSWORD = 'Password123!';
const DS_ID = 'cae34cce-35fe-4afd-9a62-d8721e877400';

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

async function shot(page, name, caption) {
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT}/${name}.png` });
  log(`📸 ${name} — ${caption}`);
}

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
page.on('pageerror', (e) => log('PAGE ERROR:', e.message));

// --- Sign in through the real UI ---
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
await page.fill('#email', EMAIL);
await page.fill('#password', PASSWORD);
await page.click('button[type="submit"]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
log('signed in');

// --- Create a fresh report via API (stable), attached to the sqlite source ---
const token = await page.evaluate(async (creds) => {
  const r = await fetch('/api/auth/jwt/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: `username=${encodeURIComponent(creds.email)}&password=${encodeURIComponent(creds.password)}`,
  });
  return (await r.json()).access_token;
}, { email: EMAIL, password: PASSWORD });

const orgId = await page.evaluate(async (t) => {
  const r = await fetch('/api/organizations', { headers: { Authorization: `Bearer ${t}` } });
  return (await r.json())[0].id;
}, token);

const report = await page.evaluate(async ({ t, org, ds }) => {
  const r = await fetch('/api/reports', {
    method: 'POST',
    headers: { Authorization: `Bearer ${t}`, 'X-Organization-Id': org, 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: 'Queue & Steer demo', files: [], data_sources: [ds] }),
  });
  return await r.json();
}, { t: token, org: orgId, ds: DS_ID });
log('report created:', report.id);

await page.goto(`${BASE}/reports/${report.id}`, { waitUntil: 'domcontentloaded' });
const promptInput = page.locator('div.mention-input-field[contenteditable="true"]').first();
await promptInput.waitFor({ state: 'visible', timeout: 120000 });
await page.waitForTimeout(2500); // data sources hydrate

// ============ STAGE 1: submit a prompt, completion starts running ============
await promptInput.fill('Compare total and average order amounts per region in orders_1, and explain what drives the differences.');
await promptInput.press('Enter');
// Thinking indicator = run started
await page.locator('.thinking-shimmer').waitFor({ state: 'visible', timeout: 60000 });
await page.waitForTimeout(2500);
// Type the next prompt so the queue/steer buttons show alongside Stop
await promptInput.fill('Now show the top 5 customers by total order amount.');
await page.locator('[data-testid="queue-button"]').waitFor({ state: 'visible', timeout: 15000 });
await page.locator('[data-testid="steer-button"]').waitFor({ state: 'visible', timeout: 15000 });
await shot(page, '1-running-with-queue-steer-buttons', 'completion running; input stays live with Queue (list icon), Steer (bolt) and Stop buttons');

// ============ STAGE 2: queue the second prompt ============
await page.locator('[data-testid="queue-button"]').click();
await page.locator('[data-testid="queued-prompt-chip"]').waitFor({ state: 'visible', timeout: 30000 });
await shot(page, '2-prompt-queued', 'second prompt sits in the queue chip while the first completion streams');

// ============ STAGE 3: steer the running completion ============
// Park the cursor first: the queued chip's tooltip (still open from the
// queue click) can overlap and intercept the steer button.
await page.mouse.move(10, 10);
await page.waitForTimeout(400);
await promptInput.fill('Also mention which region has the fewest orders. Keep the final answer short.');
await page.locator('[data-testid="steer-button"]').click();
await page.locator('[data-testid="steering-badge"]').first().waitFor({ state: 'visible', timeout: 30000 });
await shot(page, '3-steered-into-running-completion', 'steer message injected into the live run (amber badge + bordered bubble), queue chip still pending');

// ============ STAGE 4: first run finishes -> queued prompt auto-starts ============
// The queued chip disappears when the dispatcher claims it and the row becomes a
// normal user turn; a second system completion appears.
await page.locator('[data-testid="queued-prompt-chip"]').waitFor({ state: 'detached', timeout: 300000 });
log('queued chip cleared — dispatcher started the queued prompt');
await page.waitForTimeout(3000);
await page.locator('.thinking-shimmer').waitFor({ state: 'visible', timeout: 60000 });
await shot(page, '4-queued-prompt-auto-started', 'first run finished; the queued prompt was auto-dispatched and is now running');

// ============ STAGE 5: everything done ============
await page.locator('.thinking-shimmer').waitFor({ state: 'detached', timeout: 300000 });
await page.waitForTimeout(2000);
await page.keyboard.press('End');
await shot(page, '5-final-both-completed', 'both turns complete: steered answer + queued prompt answered, queue empty');

// Sanity dump of final timeline state
const finalState = await page.evaluate(async ({ t, org, rid }) => {
  const r = await fetch(`/api/reports/${rid}/completions?limit=30`, {
    headers: { Authorization: `Bearer ${t}`, 'X-Organization-Id': org },
  });
  const d = await r.json();
  return d.completions.map((c) => ({ role: c.role, status: c.status, type: c.message_type, text: (c.prompt?.content || '').slice(0, 50) }));
}, { t: token, org: orgId, rid: report.id });
console.log('FINAL TIMELINE:', JSON.stringify(finalState, null, 1));

const bad = finalState.filter((c) => !['success'].includes(c.status));
if (bad.length) { console.log('UNEXPECTED STATUSES:', bad); process.exitCode = 1; }
else console.log('E2E PASS: all completions succeeded, queue drained, steering recorded');

await browser.close();

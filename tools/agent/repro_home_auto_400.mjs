// Repro: starting a chat from the HOME PromptBoxV2 with the Auto model selected
// makes the very first completion fail with "HTTP error! status: 400"; a retry
// from inside the report then works.
//
// Root cause: createReport() forwards the raw selectedModel sentinel ('auto')
// as ?model_id=auto instead of the payload-mapped value (auto -> null), so the
// report page submits prompt.model_id='auto' and the backend rejects the
// unknown model id with 400.
//
//   tools/agent/boot_stack.sh --dev
//   cd backend && uv run python ../tools/agent/seed_org.py --demo
//   cd frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
//     node ../tools/agent/repro_home_auto_400.mjs
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.env.EVIDENCE_DIR || '../media/pr/promptboxv2-home-error';
mkdirSync(OUT, { recursive: true });

const BASE = 'http://localhost:3000';
const EMAIL = 'admin@example.com';
const PASSWORD = 'Password123!';
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

// Record the status of every completions POST — this is the smoking gun.
const completionPosts = [];
page.on('response', async (res) => {
  const u = res.url();
  const m = res.request().method();
  if (m === 'POST' && u.includes('/completions')) {
    completionPosts.push({ status: res.status(), url: u });
    log(`POST completions -> ${res.status()}`);
  }
  if (m === 'POST' && /\/reports$/.test(new URL(u).pathname)) {
    log(`POST /reports -> ${res.status()}`);
  }
});

// --- Sign in ---
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
await page.fill('#email', EMAIL);
await page.fill('#password', PASSWORD);
await page.click('button[type="submit"]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
log('signed in');

// --- Enable the org's Auto model router (needs the EE license) ---
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
const routeResp = await page.evaluate(async ({ t, org }) => {
  const r = await fetch('/api/organization/settings', {
    method: 'PUT',
    headers: { Authorization: `Bearer ${t}`, 'X-Organization-Id': org, 'Content-Type': 'application/json' },
    body: JSON.stringify({ config: { model_routing: { value: true } } }),
  });
  return { status: r.status, body: (await r.text()).slice(0, 200) };
}, { t: token, org: orgId });
log('enable model_routing ->', routeResp.status, routeResp.body);

// --- Home page ---
await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
const promptInput = page.locator('div.mention-input-field[contenteditable="true"]').first();
await promptInput.waitFor({ state: 'visible', timeout: 120000 });
await page.waitForTimeout(3000); // models + agents load; Auto becomes default
await shot(page, '1-home-before-submit', 'home prompt box (Auto model expected when routing is on)');

// --- Submit "hi" from home ---
await promptInput.click();
await page.keyboard.type('hi');
await page.waitForTimeout(400);
await shot(page, '1b-home-typed', 'typed "hi" into the home prompt box');
await promptInput.press('Enter');
log('pressed Enter to submit "hi" from home');

// --- Wait for navigation into the new report ---
await page.waitForURL((u) => /\/reports\/[^/]+/.test(u.pathname), { timeout: 60000 });
log('navigated to', page.url());

// --- Look for the 400 error surfaced in the chat ---
const errorLocator = page.locator('text=/HTTP error! status: 400/i');
let sawError = false;
try {
  await errorLocator.first().waitFor({ state: 'visible', timeout: 45000 });
  sawError = true;
} catch { /* fall through — maybe the greeting streamed instead */ }
await page.waitForTimeout(2000);
await shot(page, '2-report-first-message', sawError ? 'first message errored (HTTP 400)' : 'first message result');

const errorText = sawError ? (await errorLocator.first().innerText()) : '';
log('error visible on first message:', sawError, JSON.stringify(errorText));

const reportId = page.url().match(/\/reports\/([^/?]+)/)?.[1];
const had400 = completionPosts.some((c) => c.status === 400 && /\/completions$/.test(new URL(c.url).pathname));

if (had400 || sawError) {
  console.log('COMPLETION POSTS:', JSON.stringify(completionPosts, null, 1));
  console.log('REPRO CONFIRMED: first completion from HOME (Auto model) returned HTTP 400');
  process.exitCode = 2;
} else {
  // FIX PATH: the first message should stream a real answer with no 400.
  log('no 400 — waiting for the first completion to finish streaming');
  try {
    await page.locator('.thinking-shimmer').first().waitFor({ state: 'visible', timeout: 30000 });
  } catch { /* may have finished already */ }
  await page.locator('.thinking-shimmer').first().waitFor({ state: 'detached', timeout: 240000 }).catch(() => {});
  await page.waitForTimeout(1500);
  await shot(page, '3-first-message-success', 'first message from home now streams a real answer (no 400)');

  const timeline = await page.evaluate(async ({ t, org, rid }) => {
    const r = await fetch(`/api/reports/${rid}/completions?limit=20`, {
      headers: { Authorization: `Bearer ${t}`, 'X-Organization-Id': org },
    });
    const d = await r.json();
    return (d.completions || []).map((c) => ({ role: c.role, status: c.status }));
  }, { t: token, org: orgId, rid: reportId });
  console.log('FINAL TIMELINE:', JSON.stringify(timeline));
  console.log('COMPLETION POSTS:', JSON.stringify(completionPosts));

  const systemOk = timeline.some((c) => c.role === 'system' && c.status === 'success');
  const anyError = timeline.some((c) => c.status === 'error');
  if (systemOk && !anyError && !had400) {
    console.log('FIX VERIFIED: first completion from HOME (Auto) succeeded, no HTTP 400');
  } else {
    console.log('UNEXPECTED: fix did not fully verify', { systemOk, anyError, had400 });
    process.exitCode = 3;
  }
}

await browser.close();

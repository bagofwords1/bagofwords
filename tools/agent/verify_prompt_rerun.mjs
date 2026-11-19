// UI evidence for the report-chat Rerun action on a user prompt.
//
//   source tools/agent/devbox_env.sh   # BOW_EMAIL / BOW_PASSWORD
//   node tools/agent/verify_prompt_rerun.mjs seed            -> prints a report id
//   node tools/agent/verify_prompt_rerun.mjs shot <id> <out.png> [he]
//   node tools/agent/verify_prompt_rerun.mjs flow <id> <outdir>   (after the change)
//
// Drives the real stack: backend :8000, frontend :3000, the configured LLM.
import { createRequire } from 'node:module';
import { mkdirSync, readFileSync } from 'node:fs';
import { dirname } from 'node:path';
const requireFromFrontend = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = requireFromFrontend('@playwright/test');

const BASE = 'http://localhost:3000';
const API = 'http://localhost:8000';
const EMAIL = process.env.BOW_EMAIL;
const PASSWORD = process.env.BOW_PASSWORD;
const [cmd, ...args] = process.argv.slice(2);
const log = (...a) => console.error(new Date().toISOString().slice(11, 19), ...a);
const PNG = readFileSync(new URL('./fixtures/rerun_chart.png', import.meta.url));

async function login() {
  const r = await fetch(`${API}/api/auth/jwt/login`, {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: `username=${encodeURIComponent(EMAIL)}&password=${encodeURIComponent(PASSWORD)}`,
  });
  const token = (await r.json()).access_token;
  const orgs = await (await fetch(`${API}/api/organizations`, { headers: { Authorization: `Bearer ${token}` } })).json();
  return { token, org: orgs[0].id };
}
const H = ({ token, org }, json = true) => ({ Authorization: `Bearer ${token}`, 'X-Organization-Id': org, ...(json ? { 'Content-Type': 'application/json' } : {}) });

async function waitIdle(auth, reportId, timeoutMs = 180000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    const r = await (await fetch(`${API}/api/reports/${reportId}/completions?limit=50`, { headers: H(auth) })).json();
    const comps = r.completions || [];
    if (comps.length && !comps.some((c) => ['in_progress', 'queued'].includes(c.status))) return comps;
    await new Promise((res) => setTimeout(res, 2000));
  }
  throw new Error('report did not go idle');
}

async function seed() {
  const auth = await login();
  const dss = await (await fetch(`${API}/api/data_sources`, { headers: H(auth) })).json();
  const report = await (await fetch(`${API}/api/reports`, {
    method: 'POST', headers: H(auth),
    body: JSON.stringify({ title: 'Rerun demo', files: [], data_sources: dss.slice(0, 1).map((d) => d.id) }),
  })).json();
  const fd = new FormData();
  fd.append('file', new Blob([PNG], { type: 'image/png' }), 'chart.png');
  fd.append('report_id', report.id);
  const up = await fetch(`${API}/api/files`, { method: 'POST', headers: H(auth, false), body: fd });
  if (!up.ok) throw new Error(`upload failed ${up.status}`);
  const r = await fetch(`${API}/api/reports/${report.id}/completions?background=true`, {
    method: 'POST', headers: H(auth),
    body: JSON.stringify({ prompt: { content: 'What does the attached chart show? Answer in one sentence.', mentions: [], mode: 'chat' } }),
  });
  if (!r.ok) throw new Error(`completion failed ${r.status} ${await r.text()}`);
  await waitIdle(auth, report.id);
  console.log(report.id);
}

async function openReport(reportId, locale) {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  page.on('pageerror', (e) => log('PAGE ERROR:', e.message));
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded', timeout: 180000 });
  if (locale) await page.evaluate((l) => localStorage.setItem('bow.locale', l), locale);
  await page.locator('#email').waitFor({ timeout: 120000 });
  await page.fill('#email', EMAIL);
  await page.fill('#password', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
  await page.goto(`${BASE}/reports/${reportId}`, { waitUntil: 'domcontentloaded', timeout: 180000 });
  await page.locator('.user-bubble').first().waitFor({ state: 'visible', timeout: 120000 });
  await page.waitForTimeout(6000);
  return { browser, page };
}

async function hoverFirstUserMessage(page) {
  const bubble = page.locator('.user-bubble').first();
  await bubble.evaluate((el) => el.scrollIntoView({ block: 'center' }));
  await page.waitForTimeout(300);
  await bubble.hover();
  await page.waitForTimeout(400);
}

async function shot(reportId, out, locale) {
  mkdirSync(dirname(out), { recursive: true });
  const { browser, page } = await openReport(reportId, locale);
  await hoverFirstUserMessage(page);
  const rerun = page.locator('[data-testid="user-message-rerun"]').first();
  if (await rerun.count()) { await rerun.hover(); await page.waitForTimeout(600); }
  await page.screenshot({ path: out });
  log('saved', out);
  await browser.close();
}

async function flow(reportId, outDir) {
  mkdirSync(outDir, { recursive: true });
  const auth = await login();
  const { browser, page } = await openReport(reportId);
  const before = await page.locator('.user-bubble').count();
  await hoverFirstUserMessage(page);
  await page.locator('[data-testid="user-message-rerun"]').first().click();
  // New turn appears at the bottom and starts running.
  await page.waitForFunction((n) => document.querySelectorAll('.user-bubble').length > n, before, { timeout: 30000 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${outDir}/after-rerun-running.png` });
  log('saved running');
  // Rerun again while the run is active: goes to the queue, no second run.
  await hoverFirstUserMessage(page);
  await page.locator('[data-testid="user-message-rerun"]').first().click();
  await page.locator('[data-testid="queued-prompt-chip"]').first().waitFor({ state: 'visible', timeout: 30000 }).catch(() => log('no queue chip seen'));
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${outDir}/after-rerun-queued.png` });
  log('saved queued');
  const comps = await waitIdle(auth, reportId, 300000);
  await page.waitForTimeout(2500);
  const users = comps.filter((c) => c.role === 'user');
  const systems = comps.filter((c) => c.role === 'system');
  const summary = users.map((u) => ({ id: u.id, content: u.prompt?.content, mode: u.prompt?.mode, model_id: u.prompt?.model_id ?? null, files: (u.files || []).map((f) => f.filename) }));
  console.log(JSON.stringify({ users: summary, systems: systems.map((s) => s.status) }, null, 2));
  const last = page.locator('.user-bubble').last();
  await last.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${outDir}/after-rerun-done.png` });
  await browser.close();
}

if (cmd === 'seed') await seed();
else if (cmd === 'shot') await shot(args[0], args[1], args[2]);
else if (cmd === 'flow') await flow(args[0], args[1]);
else { console.error('usage: seed | shot <id> <out> [locale] | flow <id> <outdir>'); process.exit(2); }

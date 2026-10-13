// End-to-end UI verification of sub-agents (docs/design/sub-agents.md) as a human
// would see it: sign in, ask the planted RCA question on the Checkout Ops demo,
// watch the delegate cards and the background-tasks indicator, ask a mid-wave
// question, see the fan-in strip and the synthesis, check the Summary tab,
// reload, and check the org settings page. One screenshot per stage into
// media/pr/sub-agents/ (override with EVIDENCE_DIR).
//
// Requires: tools/agent/boot_stack.sh, seed_org.py, setup_haiku_llm.py and
// seed_rca_incident.py --install already run. Run from frontend/:
//   PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node ../tools/agent/verify_sub_agents.mjs
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.env.EVIDENCE_DIR || '../media/pr/sub-agents';
mkdirSync(OUT, { recursive: true });

const BASE = process.env.BOW_FRONTEND_URL || 'http://localhost:3000';
const EMAIL = process.env.BOW_ADMIN_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_ADMIN_PASSWORD || 'Password123!';
const DS_NAME = process.env.BOW_RCA_DS_NAME || 'Checkout Ops';
const PROMPT = process.env.BOW_RCA_PROMPT || (
  'Paid orders dropped about 15% since mid-September. Why? Test the likely causes ' +
  'separately and tell me which one it is, with evidence, and which ones you ruled out.'
);

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
let shotIndex = 0;
async function shot(page, name, caption) {
  await page.waitForTimeout(500);
  shotIndex += 1;
  const file = `${OUT}/${String(shotIndex).padStart(2, '0')}-${name}.png`;
  await page.screenshot({ path: file });
  log(`📸 ${file} — ${caption}`);
}

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium' });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
page.on('pageerror', (e) => log('PAGE ERROR:', e.message));
const consoleErrors = [];
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });

// --- Sign in through the real UI ---
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
await page.fill('#email', EMAIL);
await page.fill('#password', PASSWORD);
await page.click('button[type="submit"]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
log('signed in');

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
const api = async (path, init = {}) => page.evaluate(async ({ t, org, path, init }) => {
  const r = await fetch(path, { ...init, headers: { Authorization: `Bearer ${t}`, 'X-Organization-Id': org, 'Content-Type': 'application/json', ...(init.headers || {}) } });
  const text = await r.text();
  try { return { status: r.status, body: JSON.parse(text) }; } catch { return { status: r.status, body: text }; }
}, { t: token, org: orgId, path, init });

// --- Settings page: the Sub-agents section exists and is on ---
await page.goto(`${BASE}/settings/ai_settings`, { waitUntil: 'domcontentloaded' });
const section = page.locator('[data-testid="ai-section-sub_agents"]');
await section.waitFor({ state: 'visible', timeout: 60000 });
await section.locator('button').first().click();
await page.locator('[data-testid="ai-setting-enable_sub_agents"]').waitFor({ state: 'visible', timeout: 20000 });
await section.scrollIntoViewIfNeeded();
await shot(page, 'settings-sub-agents-section', 'org settings: the Sub-agents section with its nine settings');

// Make sure the posture is proactive and children may create queries for evidence.
const settingsBefore = await api('/api/organization/settings');
const cfg = settingsBefore.body?.config || {};
const patch = {};
for (const [k, v] of Object.entries({ enable_sub_agents: true, sub_agent_posture: 'proactive', sub_agent_allow_create_data: true, sub_agent_concurrency: 3, sub_agent_max_steps: 40 })) {
  patch[k] = { ...(cfg[k] || {}), value: v };
}
const upd = await api('/api/organization/settings', { method: 'PUT', body: JSON.stringify({ config: patch }) });
log('settings updated:', upd.status);

// --- Report on the Checkout Ops source ---
const dsList = await api('/api/data_sources');
const ds = (dsList.body || []).find((d) => d.name === DS_NAME);
if (!ds) { log('data source not found:', DS_NAME, JSON.stringify(dsList.body).slice(0, 300)); process.exit(2); }
const report = (await api('/api/reports', { method: 'POST', body: JSON.stringify({ title: 'Sub-agents RCA demo', files: [], data_sources: [ds.id] }) })).body;
log('report created:', report.id);

await page.goto(`${BASE}/reports/${report.id}`, { waitUntil: 'domcontentloaded' });
const promptInput = page.locator('div.mention-input-field[contenteditable="true"]').first();
await promptInput.waitFor({ state: 'visible', timeout: 120000 });
await page.waitForTimeout(2500);

// ============ STAGE 1: the RCA ask fans out ============
await promptInput.fill(PROMPT);
await promptInput.press('Enter');
await page.locator('[data-testid^="delegate-card-"]').first().waitFor({ state: 'visible', timeout: 300000 });
log('first delegate card visible');
await page.waitForTimeout(4000);
const cardsEarly = await page.locator('[data-testid^="delegate-card-"]').count();
log('delegate cards:', cardsEarly);
await shot(page, 'wave-launched-cards-running', `${cardsEarly} delegate cards + wave strip; the parent's turn ended with one line`);

// ============ STAGE 2: indicator + popover while children run ============
const indicator = page.locator('[data-testid="background-tasks-indicator"]');
await indicator.waitFor({ state: 'visible', timeout: 120000 });
await shot(page, 'background-tasks-indicator', 'status row shows N background tasks while the parent is idle');
await indicator.hover();
await page.locator('[data-testid="background-tasks-popover"]').waitFor({ state: 'visible', timeout: 15000 });
await page.waitForTimeout(1200);
await shot(page, 'background-tasks-popover', 'popover: per sub-agent title, live duration, steps, stop; Stop all on the wave');
await page.mouse.move(10, 10);

// Expand the first card for the live step rows.
const firstCard = page.locator('[data-testid^="delegate-card-"]').first();
await firstCard.locator('div').first().click();
await page.waitForTimeout(3500);
await shot(page, 'delegate-card-expanded-live', 'a delegate card expanded: goal + live tool calls of the child');

// ============ STAGE 3: a mid-wave question is a normal turn ============
await promptInput.fill('How is the investigation going so far?');
await promptInput.press('Enter');
await page.locator('.thinking-shimmer').waitFor({ state: 'visible', timeout: 60000 }).catch(() => {});
await page.waitForTimeout(2500);
await shot(page, 'mid-wave-question-working-plus-tasks', 'Working counter AND background tasks side by side');
await page.locator('.thinking-shimmer').waitFor({ state: 'detached', timeout: 300000 }).catch(() => {});
await page.waitForTimeout(1500);
await shot(page, 'mid-wave-answer', 'the parent answered from <sub_agents> (read_sub_agent card if it read one)');

// ============ STAGE 4: fan-in ============
const strip = page.locator('text=/sub-agents finished|sub-agents — deadline/').first();
await strip.waitFor({ state: 'visible', timeout: 1500000 });
log('fan-in strip visible');
await page.locator('.thinking-shimmer').waitFor({ state: 'detached', timeout: 600000 }).catch(() => {});
await page.waitForTimeout(2500);
await strip.scrollIntoViewIfNeeded();
await page.waitForTimeout(800);
await shot(page, 'fan-in-strip', 'the fan-in event strip with outcome counts, where the wave reported back');
// The synthesis reply is the last assistant message; bring its end into view.
const lastReply = page.locator('[data-testid="chat-timeline"] .markdown, .prose, [class*="markdown"]').last();
if (await lastReply.count()) { await lastReply.scrollIntoViewIfNeeded(); await page.mouse.wheel(0, 4000); }
await page.waitForTimeout(800);
await shot(page, 'fan-in-synthesis', 'the synthesis reply: cause, evidence and ruled-out decoys from the records');
// Finished cards with outcome badges
await page.locator('[data-testid^="delegate-card-"]').first().scrollIntoViewIfNeeded();
await page.waitForTimeout(800);
await shot(page, 'delegate-cards-finished', 'delegate cards with outcome badges, confidence, steps and tokens');

// ============ STAGE 5: Summary tab ============
// The Summary tab lives in the right panel header; the panel is collapsed on
// a chat-only report, so open it with the Sidebar toggle first.
let summaryTab = page.locator('button:has-text("Summary"), [role="tab"]:has-text("Summary")').first();
if (!(await summaryTab.count())) {
  const sidebarToggle = page.locator('button:has-text("Sidebar")').first();
  if (await sidebarToggle.count()) { await sidebarToggle.click(); await page.waitForTimeout(800); }
  summaryTab = page.locator('button:has-text("Summary"), [role="tab"]:has-text("Summary")').first();
}
if (await summaryTab.count()) {
  await summaryTab.click();
  const section = page.locator('[data-testid="summary-sub-agents"]');
  await section.waitFor({ state: 'visible', timeout: 30000 });
  await section.scrollIntoViewIfNeeded();
  await page.waitForTimeout(600);
  await shot(page, 'summary-tab-sub-agents', 'Summary tab: Sub-agents section with outcome badges, steps, tokens');
} else {
  log('Summary tab control not found; skipping');
}

// ============ STAGE 6: reload hydrates everything from rows ============
await page.goto(`${BASE}/reports/${report.id}`, { waitUntil: 'domcontentloaded' });
await page.locator('[data-testid^="delegate-card-"]').first().waitFor({ state: 'visible', timeout: 120000 });
await page.waitForTimeout(2500);
await shot(page, 'after-reload', 'same cards and strip after a reload, hydrated from the sub_agents rows');

// ============ Final state dump ============
const subAgents = await api(`/api/reports/${report.id}/sub-agents`);
const comps = await api(`/api/reports/${report.id}/completions?limit=50`);
const timeline = (comps.body.completions || []).map((c) => ({ role: c.role, status: c.status, src: c.trigger_source || null, text: (c.completion?.content || c.prompt?.content || '').slice(0, 90) }));
console.log('WAVES:', JSON.stringify(subAgents.body.waves.map((w) => ({ status: w.status, total: w.total, runs: w.sub_agents.map((r) => [r.title, r.status, r.outcome, r.confidence, r.steps_used]) })), null, 1));
console.log('TIMELINE:', JSON.stringify(timeline, null, 1));
if (consoleErrors.length) console.log('CONSOLE ERRORS:', consoleErrors.slice(0, 10));

const wave = subAgents.body.waves[subAgents.body.waves.length - 1];
const ok = wave && wave.status === 'done' && wave.sub_agents.length >= 3 && timeline.some((c) => c.role === 'system' && c.src === 'sub_agent_wave' && c.status === 'success');
console.log(ok ? 'E2E PASS: wave fanned out, children finished, fan-in reply succeeded' : 'E2E FAIL: see dumps above');
process.exitCode = ok ? 0 : 1;
await browser.close();

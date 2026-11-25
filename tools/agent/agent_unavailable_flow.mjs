// Drive the "agent the run cannot load" flows as a real member, through the
// UI, and capture a screenshot at each stage.
//
//   cd frontend && node ../tools/agent/agent_unavailable_flow.mjs <scenario> <outDir> [--before]
//
// Scenarios (setup: tools/agent/seed_agent_unavailable.py, see
// docs/feedback-loops/2026-11-21-agent-unavailable-followups.md):
//   offline    Sales' connection is flagged unreachable; the reply must say so.
//   connector  Mailbox is the member's own connector, not signed in: Connect
//              card -> sign in -> Ask again -> their unread mail.
//   folder     A conversation scoped to the "Finance team" folder, whose only
//              agent needs the member's sign-in: Connect card, not an error.
// With --before (fix stashed) each scenario stops after the first reply.
//
// Env: BOW_ORIGIN (default http://localhost:3000), MEMBER_EMAIL/MEMBER_PASSWORD,
// DB_USER/DB_PASSWORD (the member's personal database login).
import { mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require(require.resolve('@playwright/test', { paths: [process.cwd()] }))); }
catch { ({ chromium } = require('playwright')); }

const SCENARIO = process.argv[2];
const OUT = process.argv[3] || '../media/agent-unavailable';
const BEFORE = process.argv.includes('--before');
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const EMAIL = process.env.MEMBER_EMAIL || 'member@example.com';
const PASSWORD = process.env.MEMBER_PASSWORD || 'Password123!';
const DB_USER = process.env.DB_USER || 'alice';
const DB_PASSWORD = process.env.DB_PASSWORD || '';
const QUESTIONS = {
  offline: 'What were total sales by region last quarter?',
  connector: 'Summarise my unread mail',
  folder: 'What is the budget by department this quarter?',
};
if (!QUESTIONS[SCENARIO]) { console.error('scenario must be one of', Object.keys(QUESTIONS).join(', ')); process.exit(2); }
const QUESTION = QUESTIONS[SCENARIO];
const tag = BEFORE ? 'before' : 'after';
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium' });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const failed = [];
page.on('response', (r) => { if (r.url().includes('/api/') && r.status() >= 400) failed.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`); });
const shot = async (name) => { await page.waitForTimeout(800); await page.screenshot({ path: `${OUT}/${name}` }); console.log('captured', name); };
const finish = async (code, msg) => {
  if (code) { await page.screenshot({ path: `${OUT}/FAIL-${SCENARIO}.png`, fullPage: true }); console.error('FAIL:', msg); if (failed.length) console.error('api errors:', failed.join(' | ')); }
  else console.log('PASS:', msg);
  await ctx.close(); await browser.close(); process.exit(code);
};
async function until(predicate, timeoutMs = 120000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    await page.waitForTimeout(1500);
    if (await predicate()) return true;
  }
  return false;
}
const bodyText = () => page.locator('body').innerText();

// Sign in as the member.
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.fill('#email', EMAIL);
await page.fill('#password', PASSWORD);
await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}), page.click('button[type=submit]')]);
await page.waitForTimeout(2000);

if (SCENARIO === 'folder') {
  // A conversation scoped to the folder, created the way the app does.
  const rid = await page.evaluate(async () => {
    const token = document.cookie.split('; ').find((c) => c.startsWith('auth.token='))?.split('=')[1];
    const auth = { Authorization: `Bearer ${decodeURIComponent(token)}` };
    const org = (await (await fetch('/api/organizations', { headers: auth })).json())[0].id;
    const headers = { ...auth, 'X-Organization-Id': org, 'Content-Type': 'application/json' };
    const folders = await (await fetch('/api/folders', { headers })).json();
    const folder = (folders.folders || folders).find((f) => f.name === 'Finance team');
    const r = await fetch('/api/reports', { method: 'POST', headers, body: JSON.stringify({ agent_folder_id: folder.id }) });
    return (await r.json()).id;
  });
  if (!rid) await finish(1, 'could not create the folder-scoped conversation');
  await page.goto(`${BASE}/reports/${rid}`, { waitUntil: 'networkidle' }).catch(() => {});
} else {
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle' }).catch(() => {});
}
await page.waitForTimeout(2500);

const box = page.locator('[contenteditable=true]').first();
await box.click();
await page.keyboard.type(QUESTION, { delay: 10 });
await page.keyboard.press('Enter');
await page.waitForURL(/\/reports\//, { timeout: 30000 }).catch(() => {});

const card = page.locator('[data-testid=request-agent-signin]');
const connect = card.locator('[data-testid=signin-connect]');
const replied = async () => (await connect.count()) > 0
  || /unreachable right now|None of the agents available|can't tell which agent/i.test(await bodyText())
  || failed.some((f) => f.includes('/completions'));
const got = await until(replied, 120000);
await page.waitForTimeout(BEFORE ? 5000 : 2500);
await shot(`${SCENARIO}-${tag}.png`);
if (BEFORE) await finish(0, `before captured (${got ? 'reply seen' : 'no reply'}); api errors: ${failed.join(' | ') || 'none'}`);
if (!got) await finish(1, 'no reply');

if (SCENARIO === 'offline') {
  const text = await bodyText();
  if (!/Sales/.test(text) || !/unreachable right now/i.test(text)) await finish(1, 'the reply does not say Sales is unreachable');
  if ((await connect.count()) > 0) await finish(1, 'an unreachable agent must not offer Connect');
  await finish(0, 'the reply names Sales as unreachable');
}

if (!(await connect.count())) await finish(1, 'no Connect card in the reply');
await card.scrollIntoViewIfNeeded();
await connect.first().click();
await page.locator('input[type=password]').first().waitFor({ timeout: 15000 }).catch(() => finish(1, 'credentials modal did not open'));
await page.locator('input[type=text]:visible').last().fill(DB_USER);
await page.locator('input[type=password]').first().fill(DB_PASSWORD);
await shot(`${SCENARIO}-credentials.png`);
await page.getByRole('button', { name: /^Save$/ }).click();
if (!(await until(async () => (await card.locator('[data-testid=signin-connected]').count()) > 0, 30000))) await finish(1, 'card did not flip to Connected');
await card.scrollIntoViewIfNeeded();
await shot(`${SCENARIO}-connected.png`);

await card.locator('[data-testid=signin-ask-again]').click();
if (!(await until(async () => /Here is what the \w+ agent returned/i.test(await bodyText()), 180000))) await finish(1, 're-run produced no answer');
await page.waitForTimeout(3000);
const title = SCENARIO === 'connector' ? 'Unread mail' : 'Budget by department';
await page.getByText(title).last().click().catch(() => {});
await page.waitForTimeout(2500);
await page.getByRole('tab', { name: 'Data' }).or(page.getByText('Data', { exact: true })).last().click().catch(() => {});
const expected = SCENARIO === 'connector' ? [/Q3 forecast review/, /Invoice 4471/] : [/Engineering/, /Marketing/];
const rows = await until(async () => { const t = await bodyText(); return expected.every((re) => re.test(t)); }, 30000);
await shot(`${SCENARIO}-answer.png`);
if (!rows) await finish(1, 'the re-run does not show rows from the member\'s own login');
await finish(0, 're-run answered from the database as the member');

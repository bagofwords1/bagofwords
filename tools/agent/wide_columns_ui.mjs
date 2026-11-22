// UI evidence driver: wide-table agents, column usage, column cap.
//
// Runs as a logged-in user against a stack booted by tools/agent/boot_stack.sh.
// Every chat is typed into the real report composer; the trace modal's
// Context panel and the agent Tables settings are opened by clicking.
//
//   node tools/agent/wide_columns_ui.mjs <command> [args]
//
// Commands
//   tables <agentName> <tableName> <out.png>            agent Tables settings, table expanded
//   chat <agentName> <prompt> <tag> [maxWaitSec]        new report on that agent, send prompt,
//                                                       screenshot the answer; prints REPORT_ID=
//   context <reportId> <tableName> <out.png>            open the last answer's trace -> Context
//   feedback <reportId> up|down <out.png>               click thumbs on the last answer
//   video <out.webm> <script.json>                      record a multi-step flow (see bottom)
//
// Env: OUT_DIR (default media/pr/wide-table-columns), BOW_ORIGIN (http://localhost:3000),
// BOW_API (http://localhost:8000), BOW_EMAIL / BOW_PASSWORD, BOW_LOCALE (en|es|he).
import { createRequire } from 'node:module';
import { mkdirSync, existsSync, renameSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
const req = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = req('@playwright/test');

const ORIGIN = process.env.BOW_ORIGIN || 'http://localhost:3000';
const API = process.env.BOW_API || 'http://localhost:8000';
const EMAIL = process.env.BOW_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_PASSWORD || 'Password123!';
const LOCALE = process.env.BOW_LOCALE || 'en';
const STATE = process.env.BOW_STATE || '/tmp/wide-columns-state.json';
const SCALE = Number(process.env.SCALE || 2);

async function api(path, { method = 'GET', body } = {}) {
  const tokRes = await fetch(`${API}/api/auth/jwt/login`, {
    method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ username: EMAIL, password: PASSWORD }),
  });
  const { access_token } = await tokRes.json();
  const orgs = await (await fetch(`${API}/api/organizations`, { headers: { Authorization: `Bearer ${access_token}` } })).json();
  const res = await fetch(`${API}/api${path}`, {
    method,
    headers: { Authorization: `Bearer ${access_token}`, 'X-Organization-Id': orgs[0].id, 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${method} ${path} -> ${res.status} ${await res.text()}`);
  return res.json();
}

async function agentId(name) {
  const all = await api('/data_sources');
  const ds = all.find((d) => d.name === name);
  if (!ds) throw new Error(`agent not found: ${name}`);
  return ds.id;
}

async function open({ video } = {}) {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
  const opts = { viewport: { width: 1440, height: 900 }, deviceScaleFactor: video ? 1 : SCALE };
  if (existsSync(STATE)) opts.storageState = STATE;
  if (video) opts.recordVideo = { dir: video, size: { width: 1440, height: 900 } };
  const ctx = await browser.newContext(opts);
  await ctx.addInitScript((loc) => { try { localStorage.setItem('bow.locale', loc); } catch {} }, LOCALE);
  const page = await ctx.newPage();
  page.setDefaultTimeout(30000);
  await page.goto(`${ORIGIN}/`, { waitUntil: 'domcontentloaded' });
  // Either the sign-in form or the signed-in app shell; a cold dev server or
  // an expired saved session can take a while to settle on one of them.
  await Promise.race([
    page.locator('#email').waitFor({ timeout: 60000 }),
    page.locator('[contenteditable=true]').first().waitFor({ timeout: 60000 }),
  ]).catch(() => {});
  if (page.url().includes('sign-in') || await page.locator('#email').count()) {
    await page.locator('#email').fill(EMAIL);
    await page.locator('#password').fill(PASSWORD);
    await Promise.all([
      page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 30000 }).catch(() => {}),
      page.locator('button[type=submit]').first().click(),
    ]);
    await page.waitForTimeout(2000);
    try { await page.getByText(/skip onboarding/i).click({ timeout: 3000 }); await page.waitForTimeout(1000); } catch {}
    await ctx.storageState({ path: STATE });
  }
  return { browser, ctx, page };
}

const OUT = process.env.OUT_DIR || 'media/pr/wide-table-columns';
const outPath = (p) => { const full = p.startsWith('/') ? p : join(process.cwd(), p.includes('/') ? p : join(OUT, p)); mkdirSync(dirname(full), { recursive: true }); return full; };

async function showTables(page, agent, table) {
  const id = await agentId(agent);
  await page.goto(`${ORIGIN}/agents/${id}/tables`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  // The Tables panel's own search box (the left tree has a global one).
  const search = page.locator('input[placeholder]').nth(1);
  if (await search.count()) { await search.fill(table); await page.waitForTimeout(1500); }
  // The row's expand button lives in the right-hand panel (the tree lists the
  // same name on the left).
  await page.locator('button', { hasText: table }).last().click();
  await page.waitForTimeout(2500);
}

async function waitForAnswer(reportId, maxSec) {
  const t0 = Date.now();
  while ((Date.now() - t0) / 1000 < maxSec) {
    await new Promise((r) => setTimeout(r, 5000));
    const comps = await api(`/reports/${reportId}/completions?limit=10`).catch(() => null);
    const list = Array.isArray(comps) ? comps : (comps?.completions || comps?.items || []);
    const sys = list.filter((c) => c.role === 'system');
    const last = sys[sys.length - 1];
    if (last && ['success', 'error', 'stopped'].includes(last.status)) return last;
  }
  return null;
}

async function chat(page, agent, prompt, tag, maxSec) {
  const id = await agentId(agent);
  const report = await api('/reports', { method: 'POST', body: { title: `${agent} — ${tag}`, data_sources: [id] } });
  await page.goto(`${ORIGIN}/reports/${report.id}`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  const box = page.locator('[contenteditable=true]').first();
  await box.click();
  await page.keyboard.type(prompt, { delay: 3 });
  await page.waitForTimeout(400);
  await page.keyboard.press('Enter');
  const last = await waitForAnswer(report.id, maxSec);
  await page.waitForTimeout(3000);
  console.log(`REPORT_ID=${report.id} STATUS=${last?.status}`);
  return report.id;
}

async function openContext(page, reportId, table) {
  await page.goto(`${ORIGIN}/reports/${reportId}`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(5000);
  await page.locator('button:has(svg), button').filter({ has: page.locator('[class*="bug-ant"], [name*="bug-ant"]') }).last().click().catch(async () => {
    await page.locator('button[title]').filter({ hasText: '' }).last().click();
  });
  await page.waitForTimeout(4000);
  const modal = page.locator('[role=dialog]').last();
  // The Context browser sits in the turn overview, below the memory block.
  await modal.getByText(/^\s*(Overview|סקירה|Resumen)\s*$/).first().click().catch(() => {});
  await page.waitForTimeout(1500);
  const schemasHeader = modal.locator('div.cursor-pointer', { hasText: /^\s*(Schemas|סכמות|Esquemas)\s*$/ }).first();
  await schemasHeader.scrollIntoViewIfNeeded();
  await schemasHeader.click();
  await page.waitForTimeout(800);
  if (process.env.DEBUG_SHOT) await page.screenshot({ path: process.env.DEBUG_SHOT });
  // A single agent block opens expanded; open it only when it is collapsed.
  const tableRows = modal.locator('div.py-1 > div.cursor-pointer');
  if (!(await tableRows.count())) {
    await modal.locator('div.rounded-md > div.cursor-pointer').first().click();
    await page.waitForTimeout(800);
  }
  if (table) {
    const row = tableRows.filter({ hasText: table }).first();
    await row.click();
    await page.waitForTimeout(1000);
    await row.evaluate((el) => el.scrollIntoView({ block: 'start' }));
    if (process.env.SCROLL_TO === 'fold') {
      const fold = modal.locator('[data-testid^="ctx-fold-"]').first();
      if (await fold.count()) await fold.evaluate((el) => el.scrollIntoView({ block: 'center' }));
    }
  }
  await page.waitForTimeout(500);
  return modal;
}

async function feedback(page, reportId, dir) {
  await page.goto(`${ORIGIN}/reports/${reportId}`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(5000);
  const icon = dir === 'up' ? 'hand-thumb-up' : 'hand-thumb-down';
  await page.locator(`button:has([class*="${icon}"]), button:has([name*="${icon}"])`).last().click();
  await page.waitForTimeout(1500);
  // Thumbs down asks "what went wrong?": answer and submit like a user would.
  const dialog = page.locator('[role=dialog]').last();
  if (dir === 'down' && await dialog.locator('textarea').count()) {
    await dialog.locator('textarea').fill(process.env.FEEDBACK_TEXT || 'Wrong breakdown: the tier column is not what I asked about.');
    if (process.env.SHOT_DIALOG) await page.screenshot({ path: process.env.SHOT_DIALOG });
    await dialog.locator('button').filter({ hasText: /submit|שלח|enviar/i }).first().click();
    await page.waitForTimeout(2000);
  }
}

// The whole story in one recording: Tables sorted by usage, a live
// chat that needs a folded column (describe_tables `columns`), the tool card,
// the Context panel ("N of M columns" + fold), and a thumbs up.
async function videoFlow(page, agent, table, prompt) {
  const pause = (ms) => page.waitForTimeout(ms);
  await showTables(page, agent, table);
  await page.mouse.wheel(0, 250); await pause(2500);
  const reportId = await chat(page, agent, prompt, 'video-flow', 420);
  await pause(1500);
  const card = page.getByText(/columns matching/).first();
  if (await card.count()) {
    await card.click(); await pause(1200);
    const item = page.locator('.tool-call-details li').first();
    if (await item.count()) { await item.click(); await pause(2500); }
  }
  await openContext(page, reportId, table);
  await pause(3000);
  const fold = page.locator('[role=dialog] [data-testid^="ctx-fold-"]').first();
  if (await fold.count()) { await fold.evaluate((el) => el.scrollIntoView({ block: 'center', behavior: 'smooth' })); await pause(2500); }
  await page.keyboard.press('Escape'); await pause(800);
  await feedback(page, reportId, 'up');
  await pause(1500);
  await showTables(page, agent, table);
  await pause(3000);
  return reportId;
}

const [cmd, ...args] = process.argv.slice(2);
if (cmd === 'video') {
  const [out, agent, table, prompt] = args;
  const dir = '/tmp/wide-columns-video';
  const { browser, ctx, page } = await open({ video: dir });
  try {
    const rid = await videoFlow(page, agent, table, prompt);
    console.log(`REPORT_ID=${rid}`);
  } finally {
    const v = page.video();
    await ctx.close();
    await browser.close();
    if (v) { const src = await v.path(); renameSync(src, outPath(out)); console.log('VIDEO', outPath(out)); }
  }
  process.exit(0);
}
const { browser, ctx, page } = await open({});
try {
  if (cmd === 'tables') {
    const [agent, table, out] = args;
    await showTables(page, agent, table);
    await page.screenshot({ path: outPath(out) });
  } else if (cmd === 'chat') {
    const [agent, prompt, tag, maxSec] = args;
    await chat(page, agent, prompt, tag, Number(maxSec || 240));
    await page.screenshot({ path: outPath(`${tag}-answer.png`) });
  } else if (cmd === 'context') {
    const [reportId, table, out] = args;
    await openContext(page, reportId, table);
    await page.screenshot({ path: outPath(out) });
  } else if (cmd === 'toolcard') {
    // Expand the describe_tables card of a report and its first table.
    const [reportId, out] = args;
    await page.goto(`${ORIGIN}/reports/${reportId}`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(5000);
    // Tool calls sit inside the collapsed "Worked for …" timeline.
    await page.getByText(/Worked for/).first().click().catch(() => {});
    await page.waitForTimeout(1500);
    const header = page.getByText(/columns matching/).first();
    await header.click();
    await page.waitForTimeout(1000);
    const item = page.locator('.tool-call-details li').first();
    await item.click();
    await page.waitForTimeout(1200);
    await item.evaluate((el) => el.scrollIntoView({ block: 'center' }));
    await page.waitForTimeout(500);
    await page.screenshot({ path: outPath(out) });
  } else if (cmd === 'feedback') {
    const [reportId, dir, out] = args;
    await feedback(page, reportId, dir);
    await page.screenshot({ path: outPath(out) });
  } else {
    console.error('unknown command', cmd);
    process.exitCode = 2;
  }
} finally {
  await ctx.close();
  await browser.close();
}

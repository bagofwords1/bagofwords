// "Stop kills the data query" probe — drives the real chat UI and watches
// Postgres to see whether pressing Stop actually ends the running statement.
//
// Prereqs (see docs/feedback-loops/stop-kills-running-data-query.md):
//   - stack up (tools/agent/boot_stack.sh --dev), org seeded (seed_org.py)
//   - tools/agent/stop_query_stub_llm.py running and set as default model
//   - a postgresql data source over the `warehouse` DB with public.orders active
//
//   cd frontend
//   node ../tools/agent/stop_query_probe.mjs <label> <data_source_id> <out_dir>
//
// Env: BOW_EMAIL, BOW_PASSWORD, BOW_ORIGIN (default http://localhost:3000),
//      WAREHOUSE_PSQL (default "psql -h 127.0.0.1 -U postgres -d warehouse"),
//      APP_PSQL (default "psql -h 127.0.0.1 -U postgres -d bow_app"),
//      PGPASSWORD (default postgres), WATCH_S (seconds to watch after Stop, 45)
//
// Writes <out_dir>/<label>-{01-running,02-after-stop,03-end}.png and
// <out_dir>/<label>.json (pg_stat_activity timeline + run status).
import { execSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';

// Resolve Playwright from the cwd (frontend/), falling back to the global one.
const require = createRequire(`${process.cwd()}/`);
let chromium;
try { ({ chromium } = require('@playwright/test')); } catch { ({ chromium } = require('playwright')); }

const [label, dsId, outDir] = process.argv.slice(2);
if (!label || !dsId || !outDir) {
  console.error('usage: node stop_query_probe.mjs <label> <data_source_id> <out_dir>');
  process.exit(2);
}
mkdirSync(outDir, { recursive: true });
const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
const email = process.env.BOW_EMAIL || 'admin@example.com';
const password = process.env.BOW_PASSWORD || 'Password123!';
const psql = process.env.WAREHOUSE_PSQL || 'psql -h 127.0.0.1 -U postgres -d warehouse';
const appPsql = process.env.APP_PSQL || 'psql -h 127.0.0.1 -U postgres -d bow_app';
const watchS = Number(process.env.WATCH_S || 45);
process.env.PGPASSWORD = process.env.PGPASSWORD || 'postgres';

// Active pg_sleep statements on the warehouse (excluding this probe's own query).
function runningSleeps() {
  const sql = "SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND query ILIKE '%pg_sleep(%' AND pid <> pg_backend_pid()";
  return Number(execSync(`${psql} -tAc "${sql}"`).toString().trim() || 0);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium' });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();

await page.goto(`${origin}/users/sign-in`, { waitUntil: 'load' });
await page.fill('#email', email);
await page.fill('#password', password);
await page.click('button[type=submit]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
if (page.url().includes('/onboarding')) {
  await page.getByText('Skip onboarding').click({ timeout: 10000 }).catch(() => {});
  await page.waitForTimeout(1500);
}

const api = (path, method = 'GET', body) => page.evaluate(async ({ path, method, body }) => {
  const token = decodeURIComponent((document.cookie.match(/auth\.token=([^;]+)/) || [])[1] || '').replace(/^Bearer\s+/, '');
  const orgs = await (await fetch('/api/organizations', { headers: { Authorization: `Bearer ${token}` } })).json();
  const r = await fetch(path, {
    method,
    headers: { Authorization: `Bearer ${token}`, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  return { status: r.status, json: await r.json().catch(() => null) };
}, { path, method, body });

if (runningSleeps() !== 0) throw new Error('a pg_sleep is already running on the warehouse — clean up first');

const created = await api('/api/reports', 'POST', { title: `Stop probe (${label})`, data_sources: [dsId] });
if (created.status >= 300) throw new Error(`report create failed: ${created.status} ${JSON.stringify(created.json)}`);
const reportId = created.json.id;
await page.goto(`${origin}/reports/${reportId}`, { waitUntil: 'load' });
const input = page.locator('[contenteditable="true"]').first();
await input.waitFor({ timeout: 120000 });
await input.click();
await page.keyboard.type('Total order amount by region');
await page.waitForTimeout(500);
await page.locator('button[class*="rounded-full"]').last().click();

// Wait for the data query to be running on Postgres.
const t0 = Date.now();
while (runningSleeps() === 0) {
  if (Date.now() - t0 > 90000) {
    await page.screenshot({ path: `${outDir}/${label}-00-no-query.png` });
    throw new Error('query never started on the warehouse');
  }
  await sleep(500);
}
await page.waitForTimeout(2000); // let the UI show the executing step
await page.screenshot({ path: `${outDir}/${label}-01-running.png` });
const beforeStop = runningSleeps();

const stopAt = Date.now();
await page.locator('[data-testid="stop-button"]').click({ timeout: 10000 });

const timeline = [];
let shotAfterStop = false;
let goneAfterS = null;
for (let s = 0; s <= watchS; s++) {
  const n = runningSleeps();
  timeline.push({ t_after_stop_s: s, active_pg_sleep: n });
  if (n === 0 && goneAfterS === null) goneAfterS = s;
  if (!shotAfterStop && s >= 3) {
    await page.screenshot({ path: `${outDir}/${label}-02-after-stop.png` });
    shotAfterStop = true;
  }
  if (goneAfterS !== null && s >= goneAfterS + 3 && s >= 5) break;
  await sleep(Math.max(0, stopAt + (s + 1) * 1000 - Date.now()));
}
await page.screenshot({ path: `${outDir}/${label}-03-end.png` });

// Settle, then read what the run persisted (app DB): tool status + steps.
await sleep(3000);
const appSql = (sql) => execSync(`${appPsql} -tAF '|' -c "${sql}"`).toString().trim();
const tools = appSql(
  `SELECT te.tool_name, te.status, te.success, coalesce(te.result_summary,'') FROM tool_executions te ` +
  `JOIN agent_executions ae ON ae.id = te.agent_execution_id JOIN completions c ON c.id = ae.completion_id ` +
  `WHERE c.report_id = '${reportId}' ORDER BY te.created_at`,
).split('\n').filter(Boolean).map((l) => {
  const [tool, status, success, summary] = l.split('|');
  return { tool, status, success: success === 't', summary: summary.slice(0, 80) };
});
// A failed/stopped create_data leaves its step as an invisible 'draft'; only
// 'success' steps surface as a query/result in the report.
const steps = appSql(`SELECT s.status FROM steps s JOIN queries q ON q.id = s.query_id WHERE q.report_id = '${reportId}' ORDER BY s.created_at`)
  .split('\n').filter(Boolean);
const completionStatus = appSql(`SELECT status FROM completions WHERE report_id = '${reportId}' AND role = 'system' ORDER BY created_at DESC LIMIT 1`);

const result = {
  label,
  report_id: reportId,
  active_pg_sleep_before_stop: beforeStop,
  query_gone_after_s: goneAfterS, // null = still running when we stopped watching
  watched_s: timeline.at(-1).t_after_stop_s,
  completion_status: completionStatus,
  tool_executions: tools,
  step_statuses: steps,
  timeline,
};
writeFileSync(`${outDir}/${label}.json`, JSON.stringify(result, null, 2));
console.log(JSON.stringify({ ...result, timeline: undefined }, null, 2));
await browser.close();

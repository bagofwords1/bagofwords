// Phase 2 — SQL Server, limit 1, slot wait 20s.
// A: a long query runs. B: waits, then gives up as "connection busy" — the
// agent explains instead of failing. C: waits; Dana kills A (SQL Server KILL),
// C takes the slot; Dana kills C. SQL Server's sys.dm_exec_requests is sampled
// the whole time.
import { browser, loginContext, SP, LIVE_JSON, sleep } from './lib.mjs';
import { startChat } from './chat.mjs';
import { openManageModal } from './modal.mjs';
import { execSync } from 'node:child_process';
import fs from 'node:fs';

const L = JSON.parse(fs.readFileSync(LIVE_JSON));
const PY = process.env.BACKEND_PYTHON || `${process.cwd()}/../backend/.venv/bin/python`;
const msActive = () => Number(execSync(`${PY} ${new URL('./mssql_active.py', import.meta.url).pathname}`).toString().trim());
const qrows = () => execSync(`psql -h 127.0.0.1 -U postgres -d bow_live -tAF'|' -c "select id, status, report_id from query_executions where connection_id='${L.mssql.conn}' order by queued_at"`, { env: { ...process.env, PGPASSWORD: 'postgres' } }).toString().trim().split('\n').filter(Boolean).map(l => { const [id, status, report] = l.split('|'); return { id, status, report }; });
const waitFor = async (pred, ms = 60000) => { const end = Date.now() + ms; while (Date.now() < end) { const v = pred(); if (v) return v; await sleep(400); } throw new Error('timeout'); };

let maxActive = 0;
const samples = [];
const t0 = Date.now();
const monitor = setInterval(() => { try { const n = msActive(); maxActive = Math.max(maxActive, n); samples.push({ t: (Date.now() - t0) / 1000, active: n }); } catch {} }, 700);

const heavy = tag => `Count rows in the sales orders. SQL: SELECT SUM(CAST(a.id ^ b.id ^ c.id ^ d.id AS bigint)) AS ${tag} FROM dbo.orders a CROSS JOIN dbo.orders b CROSS JOIN dbo.orders c CROSS JOIN dbo.orders d`;
const b = await browser();
const analyst = await loginContext(b, 'analyst@example.com');
const admin = await loginContext(b, 'admin@example.com');
const dba = await loginContext(b, 'dba@example.com');
const ad2 = await admin.ctx.newPage();

const manage = await openManageModal(dba.page, 0); // sales-mssql
await manage.click();
await dba.page.locator('[data-testid=connection-tab-queries]').click();
await dba.page.locator('[data-testid=queries-summary]').waitFor({ timeout: 30000 });

const A = await startChat(analyst.page, { title: 'MS-A analyst', dsId: L.mssql.ds, prompt: heavy('a') });
await waitFor(() => qrows().find(r => r.report === A && r.status === 'running'));
await waitFor(() => msActive() === 1, 30000);
const B = await startChat(admin.page, { title: 'MS-B admin', dsId: L.mssql.ds, prompt: heavy('b') });
await waitFor(() => qrows().find(r => r.report === B && r.status === 'queued'));
await admin.page.locator('[data-testid=waiting-for-slot-header]').first().waitFor({ timeout: 15000 });
await admin.page.screenshot({ path: `${SP}/p2-01-chat-waiting-1of1.png` });
await dba.page.waitForTimeout(2500);
await dba.page.screenshot({ path: `${SP}/p2-02-queries-1-running-1-waiting.png` });

// B gives up after the 20s slot wait: "connection busy".
await waitFor(() => qrows().find(r => r.report === B && r.status === 'busy'), 60000);
for (let i = 0; i < 60; i++) { if (!(await admin.page.locator('[data-testid="stop-button"]').count())) break; await sleep(1000); }
await admin.page.waitForTimeout(2000);
await admin.page.screenshot({ path: `${SP}/p2-03-chat-busy.png` });

// C waits; Dana kills A (running, SQL Server KILL); C takes the slot; Dana kills C.
const C = await startChat(ad2, { title: 'MS-C admin', dsId: L.mssql.ds, prompt: heavy('c') });
const cRow = await waitFor(() => qrows().find(r => r.report === C && r.status === 'queued'));
const aRow = qrows().find(r => r.report === A);
await dba.page.waitForTimeout(2500);
const killA = Date.now();
await dba.page.locator(`[data-testid=running-query-${aRow.id}] [data-testid=query-kill]`).click();
await dba.page.locator(`[data-testid=running-query-${aRow.id}] [data-testid=query-kill-confirm]`).click();
await waitFor(() => qrows().find(r => r.report === A && r.status === 'cancelled'), 30000);
const aCancelledMs = Date.now() - killA;
await waitFor(() => qrows().find(r => r.report === C && r.status === 'running'), 30000);
await dba.page.waitForTimeout(2500);
await dba.page.screenshot({ path: `${SP}/p2-04-A-killed-C-running.png` });
const killC = Date.now();
await dba.page.locator(`[data-testid=running-query-${cRow.id}] [data-testid=query-kill]`).click();
await dba.page.locator(`[data-testid=running-query-${cRow.id}] [data-testid=query-kill-confirm]`).click();
await waitFor(() => qrows().find(r => r.report === C && r.status === 'cancelled'), 30000);
const sourceIdle = await waitFor(() => msActive() === 0 ? Date.now() - killC : 0, 30000);
await dba.page.waitForTimeout(6500);
await dba.page.screenshot({ path: `${SP}/p2-05-history.png` });
await analyst.page.waitForTimeout(3000);
await analyst.page.screenshot({ path: `${SP}/p2-06-chat-A-killed.png` });

clearInterval(monitor);
const result = { limit: 1, maxActiveOnSqlServer: maxActive, aCancelledMs, cGoneFromSqlServerMs: sourceIdle, statuses: qrows().map(r => r.status), samples };
fs.writeFileSync(`${SP}/p2-result.json`, JSON.stringify(result, null, 2));
console.log(JSON.stringify({ ...result, samples: undefined }));
await b.close();

// Phase 3 — who can see and kill. Avi (plain member) starts a slow query,
// then tries every way in: the UI, the list API, the kill API (his own query,
// and via a connection he does not manage), and an id from another connection.
import { browser, loginContext, SP, LIVE_JSON, ORIGIN, sleep } from './lib.mjs';
import { startChat, api } from './chat.mjs';
import { execSync } from 'node:child_process';
import fs from 'node:fs';

const L = JSON.parse(fs.readFileSync(LIVE_JSON));
const qrows = conn => execSync(`psql -h 127.0.0.1 -U postgres -d bow_live -tAF'|' -c "select id, status, report_id from query_executions where connection_id='${conn}' order by queued_at"`, { env: { ...process.env, PGPASSWORD: 'postgres' } }).toString().trim().split('\n').filter(Boolean).map(l => { const [id, status, report] = l.split('|'); return { id, status, report }; });
const out = {};
const b = await browser();
const avi = await loginContext(b, 'analyst@example.com');
const dana = await loginContext(b, 'dba@example.com');

const rep = await startChat(avi.page, { title: 'Avi own query', dsId: L.pg.ds, prompt: 'Orders by region from the warehouse. SQL: WITH slow AS (SELECT pg_sleep(45)) SELECT region, COUNT(*) AS orders FROM public.orders, slow GROUP BY region ORDER BY region' });
let row;
for (let i = 0; i < 60 && !row; i++) { row = qrows(L.pg.conn).find(r => r.report === rep && r.status === 'running'); await sleep(500); }

// 1. UI: the connection card offers Avi no "Manage connection" — no Queries tab.
await avi.page.goto(`${ORIGIN}/agents`);
const bar = avi.page.locator('div.border-t', { has: avi.page.getByText('Connections', { exact: true }) }).last();
await bar.locator('button.relative').nth(1).click();
await avi.page.waitForTimeout(2500);
out.uiManageButtonForMember = await avi.page.getByRole('button', { name: 'Manage connection' }).count();
await avi.page.screenshot({ path: `${SP}/p3-01-member-connection-card.png` });

// 2. Direct API calls as Avi.
out.memberList = (await api(avi.page, `/api/connections/${L.pg.conn}/queries?scope=running`)).status;
out.memberHistory = (await api(avi.page, `/api/connections/${L.pg.conn}/queries`)).status;
out.memberKillOwnQuery = (await api(avi.page, `/api/connections/${L.pg.conn}/queries/${row.id}/kill`, 'POST')).status;
out.memberKillViaOtherConnection = (await api(avi.page, `/api/connections/${L.mssql.conn}/queries/${row.id}/kill`, 'POST')).status;
// 3. Dana manages both connections, but an id from one cannot be killed through the other.
out.managerKillWrongConnection = (await api(dana.page, `/api/connections/${L.mssql.conn}/queries/${row.id}/kill`, 'POST')).status;
out.managerKillUnknownId = (await api(dana.page, `/api/connections/${L.pg.conn}/queries/00000000-0000-0000-0000-000000000000/kill`, 'POST')).status;
await sleep(1500);
out.stillRunningAfterBypassAttempts = qrows(L.pg.conn).find(r => r.id === row.id)?.status;
// 4. A finished query cannot be "killed" after the fact.
const done = qrows(L.mssql.conn).find(r => r.status !== 'running' && r.status !== 'queued');
if (done) {
  const r = await api(dana.page, `/api/connections/${L.mssql.conn}/queries/${done.id}/kill`, 'POST');
  out.killFinished = { status: r.status, error_code: r.json?.error_code };
}
// Clean up: Dana kills Avi's query through the real path.
out.managerKill = (await api(dana.page, `/api/connections/${L.pg.conn}/queries/${row.id}/kill`, 'POST')).status;
for (let i = 0; i < 20; i++) { if (qrows(L.pg.conn).find(r => r.id === row.id)?.status === 'cancelled') break; await sleep(500); }
out.finalStatus = qrows(L.pg.conn).find(r => r.id === row.id)?.status;
fs.writeFileSync(`${SP}/p3-result.json`, JSON.stringify(out, null, 2));
console.log(JSON.stringify(out, null, 2));
await b.close();

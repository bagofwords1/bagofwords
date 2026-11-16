// Phase 4 — a worker dies holding a slot. Its lease must lapse so the slot
// frees (it never stays "running" forever), and the row reads as stale.
import { browser, loginContext, SP, LIVE_JSON, sleep } from './lib.mjs';
import { startChat } from './chat.mjs';
import { openManageModal } from './modal.mjs';
import { execSync } from 'node:child_process';
import fs from 'node:fs';

const L = JSON.parse(fs.readFileSync(LIVE_JSON));
const env = { ...process.env, PGPASSWORD: 'postgres' };
const qrows = () => execSync(`psql -h 127.0.0.1 -U postgres -d bow_live -tAF'|' -c "select id, status, report_id, split_part(worker_id, ':', 2), coalesce(lease_expires_at::text,'') from query_executions where connection_id='${L.pg.conn}' order by queued_at"`, { env }).toString().trim().split('\n').filter(Boolean).map(l => { const [id, status, report, pid, lease] = l.split('|'); return { id, status, report, pid, lease }; });
const pgActive = () => Number(execSync(`psql -h 127.0.0.1 -U postgres -d warehouse -tAc "select count(*) from pg_stat_activity where state='active' and query ilike '%pg_sleep(%' and pid<>pg_backend_pid()"`, { env }).toString().trim());
const sql = s => `Orders by region from the warehouse. SQL: WITH slow AS (SELECT pg_sleep(${s})) SELECT region, COUNT(*) AS orders FROM public.orders, slow GROUP BY region ORDER BY region`;
const out = {};
const b = await browser();
const admin = await loginContext(b, 'admin@example.com');
const dana = await loginContext(b, 'dba@example.com');
// One browser context per chat: each open chat holds SSE streams, and one
// context shares the browser's 6-connections-per-host budget.
const pages = [admin.page];
for (let i = 0; i < 3; i++) pages.push((await loginContext(b, 'admin@example.com')).page);

const A = await startChat(pages[0], { title: 'Crash A', dsId: L.pg.ds, prompt: sql(150) });
const B = await startChat(pages[1], { title: 'Crash B', dsId: L.pg.ds, prompt: sql(151) });
for (let i = 0; i < 60; i++) { if (qrows().filter(r => r.status === 'running').length === 2) break; await sleep(500); }
const victim = qrows().find(r => r.report === A);
out.workersBefore = execSync(`ps -eo pid,ppid,cmd | grep "multiprocessing.spawn" | grep -v grep | awk '{print $1}'`).toString().trim().split('\n');
execSync(`kill -9 ${victim.pid}`);
const crashAt = Date.now();
out.killedWorkerPid = victim.pid;

const C = await startChat(pages[2], { title: 'Crash C', dsId: L.pg.ds, prompt: sql(60) });
const D = await startChat(pages[3], { title: 'Crash D', dsId: L.pg.ds, prompt: sql(61) });
let firstRunAfterCrash = null;
for (let i = 0; i < 180; i++) {
  const rows = qrows();
  if (!firstRunAfterCrash && rows.some(r => (r.report === C || r.report === D) && r.status === 'running')) firstRunAfterCrash = Date.now() - crashAt;
  if (firstRunAfterCrash) break;
  await sleep(500);
}
out.secondsUntilSlotFreed = firstRunAfterCrash / 1000;
out.pgActiveAfterFree = pgActive(); // includes the dead worker's orphaned statement, if the source kept it

const manage = await openManageModal(dana.page, 1);
await manage.click();
await dana.page.locator('[data-testid=connection-tab-queries]').click();
await dana.page.waitForTimeout(4000);
await dana.page.screenshot({ path: `${SP}/p4-01-after-crash.png` });
out.rowsAfter = qrows().map(r => ({ report: [A, B, C, D].indexOf(r.report), status: r.status }));
out.workersAfter = execSync(`ps -eo pid,ppid,cmd | grep "multiprocessing.spawn" | grep -v grep | awk '{print $1}'`).toString().trim().split('\n');
// Clean up the rest through the UI-equivalent API path.
fs.writeFileSync(`${SP}/p4-result.json`, JSON.stringify(out, null, 2));
console.log(JSON.stringify(out, null, 2));
await b.close();

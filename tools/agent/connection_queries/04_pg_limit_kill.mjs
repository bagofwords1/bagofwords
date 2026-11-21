// Phase 1 — Postgres warehouse, limit 2, three backend workers.
// Two users start four slow chats at once. Dana (connection manager) watches
// the Queries tab, kills one running and one queued query. Postgres's own
// pg_stat_activity is sampled every 500ms the whole time.
import { browser, loginContext, SP, LIVE_JSON, sleep } from './lib.mjs';
import { startChat, api } from './chat.mjs';
import { openManageModal } from './modal.mjs';
import { execSync } from 'node:child_process';
import fs from 'node:fs';

const L = JSON.parse(fs.readFileSync(LIVE_JSON));
const pgCount = () => Number(execSync(`psql -h 127.0.0.1 -U postgres -d warehouse -tAc "select count(*) from pg_stat_activity where state='active' and query ilike '%pg_sleep(%' and pid<>pg_backend_pid()"`, { env: { ...process.env, PGPASSWORD: 'postgres' } }).toString().trim());
const qrows = () => execSync(`psql -h 127.0.0.1 -U postgres -d bow_live -tAF'|' -c "select id, status, report_id, coalesce(kill_requested_at::text,'') from query_executions where connection_id='${L.pg.conn}' order by queued_at"`, { env: { ...process.env, PGPASSWORD: 'postgres' } }).toString().trim().split('\n').filter(Boolean).map(l => { const [id, status, report, kill] = l.split('|'); return { id, status, report, kill }; });

const samples = [];
let maxActive = 0;
const t0 = Date.now();
const monitor = setInterval(() => { const n = pgCount(); maxActive = Math.max(maxActive, n); samples.push({ t: (Date.now() - t0) / 1000, active: n }); }, 500);

const b = await browser();
const analyst = await loginContext(b, 'analyst@example.com');
const admin = await loginContext(b, 'admin@example.com');
const dba = await loginContext(b, 'dba@example.com', { video: true });
const a2 = await analyst.ctx.newPage();
const ad2 = await admin.ctx.newPage();

const sql = s => `Orders by region from the warehouse. SQL: WITH slow AS (SELECT pg_sleep(${s})) SELECT region, COUNT(*) AS orders FROM public.orders, slow GROUP BY region ORDER BY region`;
const chats = [
  { page: analyst.page, who: 'analyst', title: 'Q1 analyst', s: 91 },
  { page: a2, who: 'analyst', title: 'Q2 analyst', s: 92 },
  { page: admin.page, who: 'admin', title: 'Q3 admin', s: 93 },
  { page: ad2, who: 'admin', title: 'Q4 admin', s: 94 },
];
// Dana opens the Queries tab first (empty), then everyone hits send together.
const manage = await openManageModal(dba.page, 1);
await manage.click();
await dba.page.locator('[data-testid=connection-tab-queries]').click();
await dba.page.locator('[data-testid=queries-running-empty]').waitFor({ timeout: 30000 });
await dba.page.waitForTimeout(1500);
await dba.page.screenshot({ path: `${SP}/p1-00-queries-empty.png` });

await Promise.all(chats.map(async c => { c.report = await startChat(c.page, { title: c.title, dsId: L.pg.ds, prompt: sql(c.s) }); }));
console.log('reports', chats.map(c => `${c.title}=${c.report}`).join(' '));

// Wait until the connection is saturated: 2 running + 2 queued.
for (let i = 0; i < 120; i++) {
  const rows = qrows();
  if (rows.filter(r => r.status === 'running').length === 2 && rows.filter(r => r.status === 'queued').length === 2) break;
  await sleep(500);
}
await dba.page.locator('[data-testid=queries-queued-count]').waitFor({ timeout: 30000 });
await dba.page.waitForTimeout(2500);
await dba.page.screenshot({ path: `${SP}/p1-01-two-running-two-queued.png` });

// The queued chats show "waiting for a free slot".
const rows1 = qrows();
const queued = rows1.filter(r => r.status === 'queued');
const runningRows = rows1.filter(r => r.status === 'running');
for (const r of queued) {
  const c = chats.find(c => c.report === r.report);
  await c.page.locator('[data-testid=waiting-for-slot-header]').first().waitFor({ timeout: 8000 }).catch(() => console.log('no waiting label on', c.title));
  await c.page.screenshot({ path: `${SP}/p1-02-chat-waiting-${c.title.replace(/\s/g, '_')}.png` });
}
for (const r of runningRows) {
  const c = chats.find(c => c.report === r.report);
  await c.page.screenshot({ path: `${SP}/p1-02-chat-running-${c.title.replace(/\s/g, '_')}.png` });
}

// Dana kills the first running query.
const victim = runningRows[0];
const victimChat = chats.find(c => c.report === victim.report);
const beforeKill = pgCount();
const killStart = Date.now();
const row = dba.page.locator(`[data-testid=running-query-${victim.id}]`);
await row.locator('[data-testid=query-kill]').click();
await dba.page.waitForTimeout(400);
await dba.page.screenshot({ path: `${SP}/p1-03-kill-confirm.png` });
await row.locator('[data-testid=query-kill-confirm]').click();
await dba.page.waitForTimeout(700);
await dba.page.screenshot({ path: `${SP}/p1-04-stopping.png` });
// The statement is gone from Postgres and the oldest queued query takes the slot.
let victimGoneMs = null;
for (let i = 0; i < 60; i++) {
  const live = execSync(`psql -h 127.0.0.1 -U postgres -d warehouse -tAc "select count(*) from pg_stat_activity where state='active' and query ilike '%pg_sleep(${victimChat.s})%' and pid<>pg_backend_pid()"`, { env: { ...process.env, PGPASSWORD: 'postgres' } }).toString().trim();
  if (live === '0') { victimGoneMs = Date.now() - killStart; break; }
  await sleep(250);
}
console.log('victim', victimChat.title, 'gone from pg_stat_activity after', victimGoneMs, 'ms; active before kill', beforeKill);
await dba.page.waitForTimeout(3000);
await dba.page.screenshot({ path: `${SP}/p1-05-after-kill-slot-handed-on.png` });

// Dana kills the query still waiting in line.
const stillQueued = qrows().filter(r => r.status === 'queued');
let queuedVictimChat = null;
if (stillQueued.length) {
  const qv = stillQueued[0];
  queuedVictimChat = chats.find(c => c.report === qv.report);
  const qrow = dba.page.locator(`[data-testid=running-query-${qv.id}]`);
  await qrow.locator('[data-testid=query-kill]').click();
  await qrow.locator('[data-testid=query-kill-confirm]').click();
  await dba.page.waitForTimeout(3000);
  await dba.page.screenshot({ path: `${SP}/p1-06-queued-killed.png` });
}

// Let the rest finish.
for (let i = 0; i < 200; i++) { if (!qrows().some(r => r.status === 'running' || r.status === 'queued')) break; await sleep(1000); }
await sleep(4000);
for (const c of [victimChat, queuedVictimChat].filter(Boolean)) {
  await c.page.reload(); await c.page.waitForTimeout(5000);
  await c.page.screenshot({ path: `${SP}/p1-07-chat-killed-${c.title.replace(/\s/g, '_')}.png` });
}
const okChat = chats.find(c => c !== victimChat && c !== queuedVictimChat);
await okChat.page.reload(); await okChat.page.waitForTimeout(5000);
await okChat.page.screenshot({ path: `${SP}/p1-07-chat-success-${okChat.title.replace(/\s/g, '_')}.png` });

// History: everything recorded, filter by Cancelled, open one.
await dba.page.waitForTimeout(6500);
await dba.page.screenshot({ path: `${SP}/p1-08-history.png`, fullPage: false });
await dba.page.locator('[data-testid=history-row-' + victim.id + ']').click();
await dba.page.waitForTimeout(800);
await dba.page.locator('[data-testid=query-detail]').first().scrollIntoViewIfNeeded();
await dba.page.screenshot({ path: `${SP}/p1-09-history-detail-killed.png` });

clearInterval(monitor);
const final = qrows();
const result = { maxActivePgSleep: maxActive, limit: 2, victimGoneMs, statuses: final.map(r => r.status), samples };
fs.writeFileSync(`${SP}/p1-result.json`, JSON.stringify(result, null, 2));
console.log(JSON.stringify({ ...result, samples: undefined }));
await dba.ctx.close();
await b.close();

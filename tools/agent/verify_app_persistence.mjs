// Acceptance run for the artifact app persistence demo (P11).
//
// Drives the demo artifact installed by tools/agent/seed_app_persistence_demo.py
// as three principals (report owner, a second org member, an anonymous
// visitor) through the real frontend and API, writes screenshots plus a JSON
// evidence summary, and exits non-zero when any check fails.
//
// Playwright is loaded from frontend/node_modules, so it runs from any cwd:
//
//   node tools/agent/verify_app_persistence.mjs \
//     [--base-url http://localhost:3000] \
//     [--report-title "Country Revenue by Genre (demo copy)"] \
//     [--source-artifact-title "Revenue by Country"] [--headed]
//
// Env (credentials ONLY from the environment; never logged or written):
//   BOW_DEMO_OWNER_EMAIL / BOW_DEMO_OWNER_PASSWORD   report owner
//   BOW_DEMO_USER_EMAIL  / BOW_DEMO_USER_PASSWORD    second org member
//   BOW_DEMO_EVIDENCE_DIR  screenshots + evidence.json (default: the session scratchpad p11 dir)
//   PW_CHROMIUM            optional Chromium binary (else Playwright's, else the newest cached one)
//
// Safety: before any write the report title must end with " (demo copy)" (the
// marker only exists in the migrated copy of the dev database). The run sets
// the report's artifact visibility to `public` for the anonymous checks (and
// to `internal` for the run when it was `none`) and restores the original in
// a finally block.
//
// Checks: a owner adds a note · b second user sees it, cannot edit it
// (forbidden in-page, dashboard intact), adds their own · c owner edits the
// second user's note · d per-user genre selections stay private across reloads ·
// e records survive an owner rerun and a new version · f anonymous sees
// highlights only (public_read: the owner's records only) and cannot write · g concurrent edit → conflict then the
// refreshed value · h tampered `mine` is refused by the server · i the original
// source artifact still renders (and its content is unchanged) · j an
// undeclared collection answers collection_not_declared.
import { createRequire } from 'node:module';
import { existsSync, mkdirSync, readdirSync, writeFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';

// ESM resolves packages next to this file, not the cwd: load Playwright from frontend/.
const { chromium } = createRequire(new URL('../../frontend/package.json', import.meta.url))('@playwright/test');

const DEMO_TITLE = 'Revenue by Country — with notes';
const COPY_MARKER = ' (demo copy)';
const DEFAULT_EVIDENCE_DIR = join(tmpdir(), 'bow-app-persistence-evidence');

function arg(name, fallback) {
  const i = process.argv.indexOf(name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
const ORIGIN = arg('--base-url', process.env.BOW_ORIGIN || 'http://localhost:3000').replace(/\/$/, '');
const REPORT_TITLE = arg('--report-title', 'Country Revenue by Genre (demo copy)');
const SOURCE_TITLE = arg('--source-artifact-title', 'Revenue by Country');
const HEADED = process.argv.includes('--headed');
const OUT = process.env.BOW_DEMO_EVIDENCE_DIR || DEFAULT_EVIDENCE_DIR;
const RUN = Date.now().toString(36);
const T = { page: 90_000, ui: 30_000 };

const OWNER = { email: process.env.BOW_DEMO_OWNER_EMAIL, password: process.env.BOW_DEMO_OWNER_PASSWORD };
const USER = { email: process.env.BOW_DEMO_USER_EMAIL, password: process.env.BOW_DEMO_USER_PASSWORD };
if (!OWNER.email || !OWNER.password || !USER.email || !USER.password) {
  console.error('set BOW_DEMO_OWNER_EMAIL, BOW_DEMO_OWNER_PASSWORD, BOW_DEMO_USER_EMAIL and BOW_DEMO_USER_PASSWORD');
  process.exit(2);
}
mkdirSync(OUT, { recursive: true });

const evidence = { run: RUN, origin: ORIGIN, report_title: REPORT_TITLE, started_at: new Date().toISOString(), checks: [], ids: {} };
function record(id, name, ok, detail = {}, screenshot = null) {
  evidence.checks.push({ id, name, ok: !!ok, detail, screenshot });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${id}: ${name}${ok ? '' : ' ' + JSON.stringify(detail)}`);
}
async function check(id, name, fn) {
  try {
    const res = await fn();
    record(id, name, res.ok, res.detail, res.screenshot || null);
  } catch (e) {
    record(id, name, false, { error: String(e && e.message || e).slice(0, 500) });
  }
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
async function api(method, path, { token, org, body, form } = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (org) headers['X-Organization-Id'] = org;
  let payload;
  if (form) { headers['Content-Type'] = 'application/x-www-form-urlencoded'; payload = new URLSearchParams(form).toString(); }
  else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
  const r = await fetch(ORIGIN + path, { method, headers, body: payload });
  let json = null;
  try { json = await r.json(); } catch {}
  return { status: r.status, json };
}
async function login(who) {
  const r = await api('POST', '/api/auth/jwt/login', { form: { username: who.email, password: who.password } });
  if (r.status !== 200) throw new Error(`login failed (${r.status})`);
  return r.json.access_token;
}
const listRecords = async (artifactId, collection, token) =>
  api('GET', `/api/artifacts/${artifactId}/data/${collection}`, { token });

// ---------------------------------------------------------------------------
// Browser
// ---------------------------------------------------------------------------
function chromiumPath() {
  if (process.env.PW_CHROMIUM) return process.env.PW_CHROMIUM;
  try { const p = chromium.executablePath(); if (p && existsSync(p)) return undefined; } catch {}
  const cache = join(homedir(), '.cache', 'ms-playwright');
  if (!existsSync(cache)) return undefined;
  const dirs = readdirSync(cache).filter(d => /^chromium-\d+$/.test(d)).sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1]));
  for (const d of dirs) {
    for (const sub of ['chrome-linux64/chrome', 'chrome-linux/chrome']) {
      const p = join(cache, d, sub);
      if (existsSync(p)) return p;
    }
  }
  return undefined;
}

async function uiLogin(context, who) {
  const page = await context.newPage();
  await page.goto(`${ORIGIN}/users/sign-in`, { waitUntil: 'domcontentloaded', timeout: T.page });
  await page.fill('#email', who.email, { timeout: T.page });
  await page.fill('#password', who.password);
  await Promise.all([
    page.waitForURL(u => !String(u).includes('/users/sign-in'), { timeout: T.page }),
    page.click('button[type=submit]'),
  ]);
  if (page.url().includes('/onboarding')) {
    await page.getByText('Skip onboarding', { exact: false }).click({ timeout: 5000 }).catch(() => {});
  }
  return page;
}

// The artifact renders inside a srcdoc iframe; find the frame holding `selector`.
async function frameWith(page, selector, timeout = T.page) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    for (const f of page.frames()) {
      if (f === page.mainFrame()) continue;
      try { if (await f.$(selector)) return f; } catch {}
    }
    await page.waitForTimeout(300);
  }
  throw new Error(`no frame with ${selector} on ${page.url()}`);
}
async function openApp(page, path) {
  await page.goto(`${ORIGIN}${path}`, { waitUntil: 'domcontentloaded', timeout: T.page });
  const f = await frameWith(page, '[data-testid=app-root]');
  // First list of every collection settled (spinners gone or an error shown).
  await f.waitForFunction(() => {
    const s = window.__appDataStore;
    return s && ['notes', 'highlights', 'selections'].every(n => !s.get(n).loading);
  }, null, { timeout: T.ui });
  return f;
}
async function shot(page, name) {
  const file = join(OUT, `${name}.png`);
  await page.screenshot({ path: file, fullPage: true }).catch(() => {});
  return file;
}
const noteByText = (f, text) => f.locator('[data-testid=note-item]', { has: f.locator('[data-testid=note-text]', { hasText: text }) });
async function editNote(f, text, next) {
  const item = noteByText(f, text).first();
  await item.locator('[data-testid=note-edit]').click({ timeout: T.ui });
  await f.fill('[data-testid=note-edit-input]', next);
  await f.click('[data-testid=note-save]');
}
async function addNote(f, text) {
  await f.fill('[data-testid=note-input]', text);
  await f.click('[data-testid=note-add]');
  await noteByText(f, text).first().waitFor({ timeout: T.ui });
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------
let browser;
let restoreVisibility = null;
let exitCode = 1;
try {
  const ownerToken = await login(OWNER);
  const userToken = await login(USER);
  const orgs = (await api('GET', '/api/organizations', { token: ownerToken })).json || [];
  let org = null;
  let report = null;
  for (const o of orgs) {
    const r = await api('GET', `/api/reports?limit=100&filter=my&search=${encodeURIComponent(REPORT_TITLE)}`, { token: ownerToken, org: o.id });
    report = ((r.json && r.json.reports) || []).find(x => x.title === REPORT_TITLE) || null;
    if (report) { org = o.id; break; }
  }
  if (!report) throw Object.assign(new Error(`report ${JSON.stringify(REPORT_TITLE)} not found for the owner`), { exit: 4 });
  // Binding proof before ANY write.
  if (!(typeof report.title === 'string' && report.title.endsWith(COPY_MARKER))) {
    throw Object.assign(new Error(`BINDING CHECK FAILED: ${JSON.stringify(report.title)} lacks ${JSON.stringify(COPY_MARKER)}; nothing written`), { exit: 3 });
  }
  record('binding', 'report carries the demo-copy marker (read before any write)', true, { report_id: report.id, title: report.title });
  const rid = report.id;
  const H = { token: ownerToken, org };

  const arts = (await api('GET', `/api/artifacts/report/${rid}`, H)).json || [];
  const latest = (title) => arts.filter(a => a.title === title).sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
  const demo = latest(DEMO_TITLE);
  const source = latest(SOURCE_TITLE);
  if (!demo) throw Object.assign(new Error('demo artifact not found; run seed_app_persistence_demo.py first'), { exit: 4 });
  if (!source) throw Object.assign(new Error(`source artifact ${JSON.stringify(SOURCE_TITLE)} not found`), { exit: 4 });
  const artifactId = demo.artifact_id;
  let demoVersionId = demo.id;
  const sourceBefore = (await api('GET', `/api/artifacts/${source.id}`, H)).json;
  const sourceHash = createHash('sha256').update(JSON.stringify(sourceBefore.content || {})).digest('hex');
  Object.assign(evidence.ids, { report_id: rid, artifact_id: artifactId, demo_version_id: demoVersionId, source_version_id: source.id });

  // Visibility: remember the original; the second user needs at least `internal`.
  const full = (await api('GET', `/api/reports/${rid}`, H)).json;
  const original = full.artifact_visibility;
  let originalShares = null;
  if (original === 'shared') originalShares = (await api('GET', `/api/reports/${rid}/shares/artifact`, H)).json || [];
  evidence.visibility = { original };
  restoreVisibility = async () => {
    const body = { visibility: original };
    if (original === 'shared') {
      body.shared_user_ids = originalShares.filter(s => s.user_id).map(s => s.user_id);
      body.shared_group_ids = originalShares.filter(s => s.group_id).map(s => s.group_id);
    }
    const r = await api('PUT', `/api/reports/${rid}/visibility/artifact`, { ...H, body });
    const now = (await api('GET', `/api/reports/${rid}`, H)).json;
    evidence.visibility.restored = now && now.artifact_visibility;
    evidence.visibility.restore_status = r.status;
    return r.status < 300 && now && now.artifact_visibility === original;
  };
  const setVisibility = async (v) => {
    const r = await api('PUT', `/api/reports/${rid}/visibility/artifact`, { ...H, body: { visibility: v } });
    if (r.status >= 300) throw new Error(`set visibility ${v}: ${r.status}`);
  };
  if (original === 'none') await setVisibility('internal');

  browser = await chromium.launch({ headless: !HEADED, executablePath: chromiumPath() });
  const ownerCtx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const userCtx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const anonCtx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const ownerPage = await uiLogin(ownerCtx, OWNER);
  const userPage = await uiLogin(userCtx, USER);
  const APP = `/r/${rid}`;

  const ownerText = `Owner note ${RUN}`;
  const userText = `Second user note ${RUN}`;
  const ownerEdit = `Edited by the owner ${RUN}`;
  let userNoteId = null;
  let ownerNoteId = null;

  await check('a', 'owner adds a note and it shows (mine)', async () => {
    const f = await openApp(ownerPage, APP);
    await addNote(f, ownerText);
    const item = noteByText(f, ownerText).first();
    ownerNoteId = await item.getAttribute('data-id');
    const mine = await item.getAttribute('data-mine');
    return { ok: mine === 'true' && !!ownerNoteId, detail: { note_id: ownerNoteId, mine }, screenshot: await shot(ownerPage, 'a_owner_added_note') };
  });

  await check('b', 'second user sees the note, cannot edit it (forbidden in-page, dashboard intact), adds own', async () => {
    const f = await openApp(userPage, APP);
    const item = noteByText(f, ownerText).first();
    await item.waitFor({ timeout: T.ui });
    const mineOnOwners = await item.getAttribute('data-mine');
    await editNote(f, ownerText, `hijack ${RUN}`);
    await f.locator('[data-testid=notes-error][data-code=forbidden]').waitFor({ timeout: T.ui });
    const dashboardVisible = await f.locator('[data-testid=revenue-chart]').isVisible() && await f.locator('[data-testid=app-root]').isVisible();
    const s1 = await shot(userPage, 'b_user_forbidden_edit');
    const apiPatch = await api('PATCH', `/api/artifacts/${artifactId}/data/notes/${ownerNoteId}`, { token: userToken, body: { data: { text: 'x' }, version: 1 } });
    const stillOwner = (await listRecords(artifactId, 'notes', ownerToken)).json.items.find(n => n.id === ownerNoteId);
    await addNote(f, userText);
    const own = noteByText(f, userText).first();
    userNoteId = await own.getAttribute('data-id');
    const ownMine = await own.getAttribute('data-mine');
    await shot(userPage, 'b_user_added_note');
    return {
      ok: mineOnOwners === 'false' && dashboardVisible && apiPatch.status === 403 && stillOwner && stillOwner.data.text === ownerText && ownMine === 'true',
      detail: { mine_on_owner_note: mineOnOwners, dashboard_visible: dashboardVisible, api_patch_status: apiPatch.status,
                owner_note_unchanged: stillOwner && stillOwner.data.text === ownerText, user_note_id: userNoteId, user_note_mine: ownMine },
      screenshot: s1,
    };
  });

  await check('c', "owner edits the second user's note", async () => {
    const f = await openApp(ownerPage, APP);
    const item = noteByText(f, userText).first();
    await item.waitFor({ timeout: T.ui });
    const mine = await item.getAttribute('data-mine');
    await editNote(f, userText, ownerEdit);
    await noteByText(f, ownerEdit).first().waitFor({ timeout: T.ui });
    const err = await f.locator('[data-testid=notes-error]').count();
    const rec = (await listRecords(artifactId, 'notes', ownerToken)).json.items.find(n => n.id === userNoteId);
    return {
      ok: mine === 'false' && err === 0 && rec && rec.data.text === ownerEdit && rec.mine === false,
      detail: { mine_for_owner: mine, error_shown: err > 0, api_text: rec && rec.data.text, author_still_second_user: rec && rec.mine === false },
      screenshot: await shot(ownerPage, 'c_owner_edited_user_note'),
    };
  });

  await check('d', "each user's genre selection is private and restored after reload", async () => {
    const fo = await openApp(ownerPage, APP);
    const sel = fo.locator('[data-testid=genre-select]');
    if (!(await sel.count())) return { ok: false, detail: { error: 'no genre parameter control on the page' } };
    const options = (await sel.locator('option').evaluateAll(os => os.map(o => o.value))).filter(v => v !== '');
    if (!options.length) return { ok: false, detail: { error: 'genre control has no options' } };
    const ownerGenre = options[0];
    const userGenre = options[1] ?? '';
    await sel.selectOption(ownerGenre);
    await fo.locator(`[data-testid=saved-genre][data-genre="${ownerGenre}"]`).waitFor({ timeout: T.ui });
    const fu = await openApp(userPage, APP);
    await fu.locator('[data-testid=genre-select]').selectOption(userGenre);
    await fu.locator(`[data-testid=saved-genre][data-genre="${userGenre}"]`).waitFor({ timeout: T.ui });
    // Reload both: each sees their own saved genre, restored into the parameter.
    const fo2 = await openApp(ownerPage, APP);
    const fu2 = await openApp(userPage, APP);
    const wantValue = (f, v) => f.waitForFunction(
      ([v]) => { const s = document.querySelector('[data-testid=genre-select]'); return s && s.value === v; }, [v], { timeout: T.ui },
    ).then(() => true, () => false);
    const ownerRestored = await wantValue(fo2, ownerGenre);
    const userRestored = await wantValue(fu2, userGenre);
    const so = await shot(ownerPage, 'd_owner_selection');
    await shot(userPage, 'd_user_selection');
    const apiOwner = (await listRecords(artifactId, 'selections', ownerToken)).json.items;
    const apiUser = (await listRecords(artifactId, 'selections', userToken)).json.items;
    const ownerOnly = apiOwner.every(x => x.mine) && apiOwner.at(-1)?.data.genre === ownerGenre;
    const userOnly = apiUser.every(x => x.mine) && (apiUser.at(-1)?.data.genre ?? '') === userGenre;
    const disjoint = !apiOwner.some(a => apiUser.some(b => b.id === a.id));
    return {
      ok: ownerRestored && userRestored && ownerOnly && userOnly && disjoint,
      detail: { owner_genre: ownerGenre, user_genre: userGenre || null, owner_restored: ownerRestored, user_restored: userRestored,
                api_owner_count: apiOwner.length, api_user_count: apiUser.length, owner_only: ownerOnly, user_only: userOnly, disjoint },
      screenshot: so,
    };
  });

  await check('e', 'records survive an owner rerun and a new version', async () => {
    const before = (await listRecords(artifactId, 'notes', ownerToken)).json.items.map(n => n.id).sort();
    const rerun = await api('POST', `/api/reports/${rid}/rerun?artifact_id=${demoVersionId}`, H);
    const afterRerun = (await listRecords(artifactId, 'notes', ownerToken)).json.items.map(n => n.id).sort();
    const dup = await api('POST', `/api/artifacts/${demoVersionId}/duplicate`, H);
    const newVersion = dup.json && dup.json.id;
    const sameIdentity = dup.json && dup.json.artifact_id === artifactId && newVersion !== demoVersionId;
    if (newVersion) { demoVersionId = newVersion; evidence.ids.duplicated_version_id = newVersion; }
    const afterDup = (await listRecords(artifactId, 'notes', ownerToken)).json.items.map(n => n.id).sort();
    const f = await openApp(ownerPage, APP);
    await noteByText(f, ownerText).first().waitFor({ timeout: T.ui });
    const shown = await f.locator('[data-testid=note-item]').count();
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    return {
      ok: rerun.status < 300 && dup.status < 300 && sameIdentity && same(before, afterRerun) && same(before, afterDup) && shown === before.length,
      detail: { rerun_status: rerun.status, duplicate_status: dup.status, new_version_id: newVersion, same_artifact_id: sameIdentity,
                notes_before: before.length, notes_after_rerun: afterRerun.length, notes_after_duplicate: afterDup.length, notes_shown: shown },
      screenshot: await shot(ownerPage, 'e_after_rerun_and_new_version'),
    };
  });

  await check('f', 'anonymous (public link) sees the owner highlights only, not notes/selections; writes refused', async () => {
    await setVisibility('public');
    try {
      const anonPage = await anonCtx.newPage();
      await anonPage.goto(`${ORIGIN}${APP}`, { waitUntil: 'domcontentloaded', timeout: T.page });
      const f = await frameWith(anonPage, '[data-testid=app-root]');
      await f.locator('[data-testid=highlight-item]').first().waitFor({ timeout: T.ui });
      await f.locator('[data-testid=notes-error]').waitFor({ timeout: T.ui });
      const highlights = await f.locator('[data-testid=highlight-item]').count();
      const notesShown = await f.locator('[data-testid=note-item]').count();
      const notesCode = await f.locator('[data-testid=notes-error]').getAttribute('data-code');
      const forms = await f.locator('[data-testid=note-input], [data-testid=highlight-input]').count();
      const view = await shot(anonPage, 'f_anonymous_view');
      const inPage = await f.evaluate(async () => {
        const st = window.__appDataStore;
        const code = (p) => p.then(() => 'ok', e => e.code);
        await st.get('selections').refresh().catch(() => {});
        return {
          highlight_add: await code(st.get('highlights').add({ text: 'anon' })),
          note_add: await code(st.get('notes').add({ country: 'X', text: 'anon' })),
          selections_items: st.get('selections').items.length,
          selections_error: st.get('selections').error && st.get('selections').error.code,
        };
      });
      const apiRead = await listRecords(artifactId, 'notes', null);
      const apiHighlights = await listRecords(artifactId, 'highlights', null);
      // public_read publishes only records the report owner wrote.
      const ownerHighlights = (await listRecords(artifactId, 'highlights', ownerToken)).json?.items || [];
      const ownerId = ownerHighlights.find(i => i.mine)?.user?.id;
      const anonItems = apiHighlights.json?.items || [];
      const ownerOnly = !!ownerId && anonItems.length >= 1 && anonItems.every(i => i.user?.id === ownerId);
      const apiWrite = await api('POST', `/api/artifacts/${artifactId}/data/highlights`, { body: { data: { text: 'anon' } } });
      const s = await shot(anonPage, 'f_anonymous_public');
      const readDenied = ['unauthenticated', 'forbidden'].includes(notesCode);
      return {
        ok: highlights >= 1 && notesShown === 0 && readDenied && forms === 0 && inPage.highlight_add === 'unauthenticated'
          && inPage.note_add === 'unauthenticated' && inPage.selections_items === 0 && [401, 403].includes(apiRead.status)
          && apiHighlights.status === 200 && ownerOnly && apiWrite.status === 401,
        detail: { highlights, notes_shown: notesShown, notes_error: notesCode, write_forms: forms, in_page: inPage,
                  api_notes_status: apiRead.status, api_highlights_status: apiHighlights.status, api_write_status: apiWrite.status,
                  api_highlights_owner_only: ownerOnly, api_highlights_count: anonItems.length,
                  refused_writes_screenshot: s },
        screenshot: view,
      };
    } finally {
      evidence.visibility.restored_after_f = await restoreVisibility();
      if (original === 'none') await setVisibility('internal');
    }
  });

  await check('g', 'concurrent edits: one succeeds, the other gets conflict and then the refreshed value', async () => {
    const p1 = await ownerCtx.newPage();
    const p2 = await ownerCtx.newPage();
    const f1 = await openApp(p1, APP);
    const f2 = await openApp(p2, APP);
    const first = `Concurrent edit A ${RUN}`;
    await editNote(f1, ownerText, first);
    await noteByText(f1, first).first().waitFor({ timeout: T.ui });
    await editNote(f2, ownerText, `Concurrent edit B ${RUN}`); // f2 still holds the old version
    await f2.locator('[data-testid=notes-error][data-code=conflict]').waitFor({ timeout: T.ui });
    const refreshed = await noteByText(f2, first).first().waitFor({ timeout: T.ui }).then(() => true, () => false);
    const rec = (await listRecords(artifactId, 'notes', ownerToken)).json.items.find(n => n.id === ownerNoteId);
    const s = await shot(p2, 'g_conflict_then_refreshed');
    await p1.close(); await p2.close();
    return { ok: refreshed && rec && rec.data.text === first, detail: { refreshed_value_shown: refreshed, api_text: rec && rec.data.text }, screenshot: s };
  });

  await check('h', "tampering: flipping `mine` in the iframe does not let the second user update the owner's note", async () => {
    const f = await openApp(userPage, APP);
    const before = (await listRecords(artifactId, 'notes', ownerToken)).json.items.find(n => n.id === ownerNoteId);
    const res = await f.evaluate(async (id) => {
      const s = window.__appDataStore.get('notes');
      const item = s.items.find(n => n.id === id);
      if (!item) return { found: false };
      const wasMine = item.mine;
      item.mine = true;
      const code = await s.update(id, { text: 'tampered' }).then(() => 'ok', e => e.code);
      return { found: true, was_mine: wasMine, code };
    }, ownerNoteId);
    const after = (await listRecords(artifactId, 'notes', ownerToken)).json.items.find(n => n.id === ownerNoteId);
    const s = await shot(userPage, 'h_tamper_refused');
    return {
      ok: res.found && res.was_mine === false && res.code === 'forbidden' && after.data.text === before.data.text,
      detail: { ...res, text_unchanged: after.data.text === before.data.text }, screenshot: s,
    };
  });

  await check('j', 'an undeclared collection answers collection_not_declared at runtime', async () => {
    const f = await openApp(ownerPage, APP);
    const code = await f.evaluate(() => window.__appDataStore.get('undeclared_demo').refresh().then(() => 'ok', e => e.code));
    return { ok: code === 'collection_not_declared', detail: { code } };
  });

  await check('i', 'the original source artifact still renders and is unchanged', async () => {
    const after = (await api('GET', `/api/artifacts/${source.id}`, H)).json;
    const hash = createHash('sha256').update(JSON.stringify(after.content || {})).digest('hex');
    await ownerPage.goto(`${ORIGIN}/reports/${rid}`, { waitUntil: 'domcontentloaded', timeout: T.page });
    await frameWith(ownerPage, '#root');
    await ownerPage.evaluate((id) => window.dispatchEvent(new CustomEvent('artifact:select', { detail: { artifact_id: id } })), source.id);
    const end = Date.now() + T.page;
    let rendered = null;
    while (Date.now() < end && !rendered) {
      for (const fr of ownerPage.frames()) {
        if (fr === ownerPage.mainFrame()) continue;
        const info = await fr.evaluate(() => {
          const root = document.getElementById('root');
          if (!root || document.querySelector('[data-testid=app-root]')) return null;
          return { children: root.childElementCount, text: (root.innerText || '').trim().length };
        }).catch(() => null);
        if (info && info.children > 0 && info.text > 20) { rendered = info; break; }
      }
      if (!rendered) await ownerPage.waitForTimeout(500);
    }
    await ownerPage.waitForTimeout(1500);
    const s = await shot(ownerPage, 'i_original_artifact');
    return { ok: !!rendered && hash === sourceHash && !(after.content || {}).storage,
             detail: { rendered, content_unchanged: hash === sourceHash, has_storage: !!(after.content || {}).storage }, screenshot: s };
  });

  exitCode = evidence.checks.every(c => c.ok) ? 0 : 1;
} catch (e) {
  record('run', 'verification run', false, { error: String(e && e.message || e).slice(0, 500) });
  exitCode = e && e.exit ? e.exit : 1;
} finally {
  if (restoreVisibility) {
    const ok = await restoreVisibility().catch(() => false);
    record('restore', 'artifact visibility restored to the original', ok, evidence.visibility);
    if (!ok && exitCode === 0) exitCode = 1;
  }
  if (browser) await browser.close().catch(() => {});
  evidence.finished_at = new Date().toISOString();
  evidence.passed = evidence.checks.filter(c => c.ok).length;
  evidence.failed = evidence.checks.filter(c => !c.ok).length;
  writeFileSync(join(OUT, 'evidence.json'), JSON.stringify(evidence, null, 2));
  console.log(`evidence: ${join(OUT, 'evidence.json')} (${evidence.passed} passed, ${evidence.failed} failed)`);
  process.exit(exitCode);
}

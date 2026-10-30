// Shared Chromium harness for the chat component tests (D-28). Not a test: it
// lives in a subdirectory so nothing that globs tests/unit/*.mjs runs it.
//
// The parent page plays the host: it answers BOW_RUNTIME_READY with a
// MessageChannel port, logs every request the SDK sends (including the
// {id, cancel:true} frames of aborted requests) and lets the test answer each
// request, stream events into it or fail it, in any order. Adapted from
// artifactUseConversation.mjs, which stays as it is.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';

const root = new URL('../../../', import.meta.url);
if (!fs.existsSync(new URL('public/libs/babel-standalone.min.js', root))) {
  console.log('SKIP: vendored libs missing (scripts/download-vendor-libs.sh)');
  process.exit(0);
}
const source = fs.readFileSync(new URL('utils/artifactIframe.ts', root), 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;

export const CHROME_PATH = process.env.PW_CHROMIUM_PATH || process.env.PW_CHROMIUM || process.env.BOW_CHROMIUM_EXECUTABLE || undefined;
export const browser = await chromium.launch({headless: true, executablePath: CHROME_PATH});
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── the default test artifact ───────────────────────────────────────────────
// A Chat component that exposes the hook's object as window.__chat (on every
// render), bumps window.__msgsVersion whenever chat.messages changes identity
// and renders each message through ChatMessage. `opts` go to useConversation.
export const hookApp = (opts, {noBow = false} = {}) => `<script>
(function () {
  var h = React.createElement;
  ${noBow ? 'delete window.bow;' : ''}
  window.__msgsVersion = 0;
  var last = null;
  window.__hookType = typeof window.useConversation;
  function Chat(props) {
    var chat = useConversation(props.opts);
    window.__chat = chat;
    if (chat.messages !== last) { last = chat.messages; window.__msgsVersion++; }
    return h('div', {id: 'chat'}, chat.messages.map(function (m) { return h(ChatMessage, {key: m.id, message: m}); }));
  }
  function App() {
    var s = React.useState({mounted: true, opts: ${JSON.stringify(opts === undefined ? null : opts)}});
    window.__setApp = s[1];
    return s[0].mounted ? h(Chat, {opts: s[0].opts}) : h('p', {id: 'unmounted'}, 'unmounted');
  }
  ReactDOM.createRoot(document.getElementById('root')).render(h(App));
})();
</script>`;

// ── the fake host (parent page) ─────────────────────────────────────────────
function installHost() {
  window.__msgs = [];
  window.__host = {log: [], port: null, auto: {}};
  window.addEventListener('message', (e) => {
    const d = e.data;
    if (!d || typeof d.type !== 'string') return;
    if (d.type.startsWith('ARTIFACT_')) window.__msgs.push(d.type + (d.payload && d.payload.message ? ': ' + d.payload.message : ''));
    if (d.type !== 'BOW_RUNTIME_READY') return;
    const host = window.__host, ch = new MessageChannel();
    host.port = ch.port1;
    ch.port1.onmessage = (ev) => {
      const m = ev.data || {};
      const op = m.cancel ? 'cancel' : m.method === 'conversationSend' ? 'send' : (m.args && m.args.op) || m.method;
      host.log.push({seq: host.log.length, id: m.id, op, args: m.args || null});
      const a = host.auto[op];
      if (op === 'cancel' || a === undefined || a === 'hold') return;
      if (a === 'echo') { host.port.postMessage({id: m.id, result: {id: m.args.messageId, status: 'stopped'}}); return; }
      if (a && a.__error) { host.port.postMessage({id: m.id, error: a.__error}); return; }
      host.port.postMessage({id: m.id, result: a});
    };
    e.source.postMessage({type: 'BOW_RUNTIME_PORT', nonce: d.nonce}, '*', [ch.port2]);
  });
}

export const CONV = {id: 'c1', title: 'q1', created_at: '2026-10-30T10:00:00Z', updated_at: '2026-10-30T10:00:00Z', agents: []};
export const DEFAULT_AUTO = {policy: {available: true, scope: 'data_only', agents: []}, list: {items: [], next_before: null}, create: CONV, stop: 'echo'};
let frames = 0;

export async function openPage(context, {clock = false} = {}) {
  const page = await context.newPage();
  // The clock is installed before the first navigation (Phase-1 D-8).
  if (clock) await page.clock.install({time: new Date('2026-10-30T10:00:00Z')});
  const consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => consoleErrors.push(String(e)));
  await page.route('**/*', async (route) => {
    const u = new URL(route.request().url());
    if (u.pathname.startsWith('/libs/')) {
      const file = new URL('public' + u.pathname, root);
      if (fs.existsSync(file)) return route.fulfill({path: file.pathname, headers: {'Access-Control-Allow-Origin': '*'}});
    }
    return route.fulfill({status: 200, body: '<html><body></body></html>', contentType: 'text/html'});
  });
  await page.goto('http://chat-harness.test/');
  await page.addScriptTag({content: 'window.exports={};' + compiled});
  await page.evaluate(installHost);
  return {page, consoleErrors, clock};
}

// One scenario = one fresh artifact frame (and a fresh host log). `code` is the
// artifact (default: hookApp(opts)); `viewport` the frame size; `fixtureMode`
// runs the SDK's fixture mode (no host).
export async function start(env, {code, opts, auto = {}, viewport = {width: 1000, height: 3000}, waitLoad = true, noBow = false, fixtureMode = false} = {}) {
  const {page, consoleErrors} = env;
  consoleErrors.length = 0;
  if (env.clock) await page.clock.resume();  // the app loads with the clock running
  const name = 'art-' + (++frames);
  await page.evaluate(({code, name, auto, viewport, fixtureMode}) => {
    document.querySelectorAll('iframe').forEach((f) => f.remove());
    window.__msgs = [];
    window.__host.log = [];
    window.__host.auto = auto;
    const f = document.createElement('iframe');
    f.name = name;
    f.sandbox = 'allow-scripts';
    f.style.width = viewport.width + 'px';
    f.style.height = viewport.height + 'px';
    f.style.border = '0';
    f.srcdoc = exports.buildArtifactIframeHtml({data: {visualizations: [], runtime: {version: 11}}, code, reactBuild: 'development', fixtureMode});
    document.body.appendChild(f);
  }, {code: code || hookApp(opts, {noBow}), name, auto: {...DEFAULT_AUTO, ...auto}, viewport, fixtureMode});
  const end = Date.now() + 20000;
  while (!(await page.evaluate(() => window.__msgs.length))) {
    if (Date.now() > end) throw new Error('the artifact never signalled');
    await sleep(25);
  }
  assert.deepEqual(await page.evaluate(() => window.__msgs.slice()), ['ARTIFACT_READY']);
  const frame = page.frame({name});
  const taken = new Set();
  const s = {
    page, frame, consoleErrors,
    log: () => page.evaluate(() => window.__host.log),
    async take(op, pred = () => true, what = op) {
      const deadline = Date.now() + 8000;
      for (;;) {
        const hit = (await s.log()).find((e) => e.op === op && !taken.has(e.seq) && pred(e));
        if (hit) { taken.add(hit.seq); return hit; }
        if (Date.now() > deadline) throw new Error('no request: ' + what + ' in ' + JSON.stringify((await s.log()).map((e) => e.op)));
        await sleep(15);
      }
    },
    count: async (op, pred = () => true) => (await s.log()).filter((e) => e.op === op && pred(e)).length,
    reply: (id, result) => page.evaluate(([id, result]) => window.__host.port.postMessage({id, result}), [id, result]),
    event: (id, event) => page.evaluate(([id, event]) => window.__host.port.postMessage({id, event}), [id, event]),
    events: async (id, list) => { for (const e of list) await s.event(id, e); },
    fail: (id, error) => page.evaluate(([id, error]) => window.__host.port.postMessage({id, error}), [id, error]),
    // Every non-function key of the hook's object, plus the sorted key list.
    chat: () => frame.evaluate(() => {
      const c = window.__chat, out = {keys: Object.keys(c).sort(), version: window.__msgsVersion};
      for (const k of Object.keys(c)) if (typeof c[k] !== 'function') out[k] = c[k];
      return JSON.parse(JSON.stringify(out));
    }),
    call: (method, ...args) => frame.evaluate(([m, args]) => { window.__chat[m].apply(null, args); }, [method, args]),
    async until(pred, what, timeout = 8000) {
      const deadline = Date.now() + timeout;
      for (;;) {
        const c = await s.chat();
        if (pred(c)) return c;
        if (Date.now() > deadline) throw new Error('timed out waiting for ' + what + ': ' + JSON.stringify(c));
        await sleep(15);
      }
    },
    async waitFor(fn, arg, what, timeout = 8000) {
      const deadline = Date.now() + timeout;
      for (;;) {
        const v = await frame.evaluate(fn, arg);
        if (v) return v;
        if (Date.now() > deadline) throw new Error('timed out waiting for ' + what);
        await sleep(20);
      }
    },
    dom: (fn, arg) => frame.evaluate(fn, arg),
    async done() {
      assert.deepEqual(await page.evaluate(() => window.__msgs.slice()), ['ARTIFACT_READY'], 'no artifact error');
      assert.deepEqual(consoleErrors, [], 'no console errors');
    },
  };
  if (waitLoad && !noBow && !fixtureMode) await s.until((c) => !c.loading, 'the initial load');
  if (env.clock) {
    // Paused before the test triggers any history load; then one step = runFor(5000).
    const now = await page.evaluate(() => Date.now());
    await page.clock.pauseAt(now + 1000);
  }
  return s;
}

// History items, as messages() returns them.
export const u = (id, text, extra = {}) => ({id, role: 'user', text, status: 'success', created_at: '2026-10-30T10:00:00Z', ...extra});
export const a = (id, text, extra = {}) => ({id, role: 'assistant', text, status: 'success', created_at: '2026-10-30T10:00:01Z', tools: [], results: [], ...extra});
export const pageOf = (items, next_before = null) => ({items, next_before});
export const result = (id, extra = {}) => ({id, block: 'b1', title: 'Revenue ' + id, columns: [{field: 'm', headerName: 'Month', type: 'string'}, {field: 'v', headerName: 'Value', type: 'number'}],
  rows: [{m: 'Jan', v: 1}, {m: 'Feb', v: 2}], row_count: 2, column_count: 2, truncated: false, chart: null, ...extra});
export const ids = (c) => c.messages.map((m) => m.id);
// n alternating user/assistant history items with ids `${prefix}0..n-1`.
export const history = (prefix, n) => Array.from({length: n}, (_, i) => (i % 2 ? a : u)(prefix + i, 'text ' + prefix + i));

// A send through create + started: returns the send request.
export async function sendStarted(s, text, mid) {
  await s.call('send', text);
  const req = await s.take('send', (e) => e.args.text === text);
  if (mid) await s.event(req.id, {type: 'started', message_id: mid, prompt_id: 'p-' + mid});
  return req;
}
// completed + the stream end + the re-read answered with `hist`.
export async function finish(s, req, hist, completed = {type: 'completed', status: 'success'}) {
  if (completed) await s.event(req.id, completed);
  await s.reply(req.id, null);
  const read = await s.take('messages');
  await s.reply(read.id, hist);
  await s.until((c) => !c.sending, 'the run end');
  return read;
}

// Runs the scenarios (ONLY=<regex> filters them); clock scenarios share a
// dedicated context. Prints `<basename>: OK` last, only when all passed.
export async function run(basename, scenarios, {viewport = {width: 1100, height: 900}} = {}) {
  const only = process.env.ONLY ? new RegExp(process.env.ONLY) : null;
  let failed = 0;
  try {
    const env = await openPage(await browser.newContext({viewport}));
    let clockEnv = null;
    for (const sc of scenarios) {
      if (only && !only.test(sc.name)) continue;
      if (sc.clock && !clockEnv) clockEnv = await openPage(await browser.newContext({viewport}), {clock: true});
      try {
        await sc.fn(sc.clock ? clockEnv : env);
        console.log('PASS: ' + sc.name);
      } catch (err) {
        failed++;
        console.log('FAIL: ' + sc.name + '\n  ' + (err && err.stack || err));
      }
    }
  } finally {
    await browser.close();
  }
  if (failed) { console.log(failed + ' scenario(s) failed'); process.exit(1); }
  console.log(basename + ': OK');
}

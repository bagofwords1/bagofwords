// Real Chromium: useConversation (artifact-globals.js) against a fake live host.
//
// The parent page plays the host: it answers BOW_RUNTIME_READY with a
// MessageChannel port, logs every request the SDK sends (including the
// {id, cancel:true} frames of aborted requests) and lets the test answer each
// request, stream events into it or fail it, in any order. The hook's state is
// read through window.__chat, which the test artifact assigns on every render.
//
//   cd frontend && PW_CHROMIUM_PATH=/path/to/chrome node tests/unit/artifactUseConversation.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';

const root = new URL('../../', import.meta.url);
if (!fs.existsSync(new URL('public/libs/babel-standalone.min.js', root))) {
  console.log('SKIP: vendored libs missing (scripts/download-vendor-libs.sh)');
  process.exit(0);
}
const source = fs.readFileSync(new URL('utils/artifactIframe.ts', root), 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
const executablePath = process.env.PW_CHROMIUM_PATH || process.env.BOW_CHROMIUM_EXECUTABLE || undefined;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── the test artifact ───────────────────────────────────────────────────────
// It counts dispatches through a wrapper around React.useReducer (installed
// before the hook first renders), bumps window.__msgsVersion whenever
// chat.messages changes identity, and spies on echarts.init / setOption.
const APP = (opts, {noBow = false} = {}) => `<script>
(function () {
  var h = React.createElement;
  ${noBow ? 'delete window.bow;' : ''}
  window.__dispatches = 0; window.__msgsVersion = 0; window.__seen = new WeakSet(); window.__charts = [];
  var useReducer = React.useReducer, wrapped = new WeakMap();
  React.useReducer = function () {
    var out = useReducer.apply(React, arguments), d = out[1], w = wrapped.get(d);
    if (!w) { w = function (a) { window.__dispatches++; return d(a); }; wrapped.set(d, w); }
    return [out[0], w];
  };
  var init = echarts.init;
  echarts.init = function (el, theme, o) {
    var inst = init.call(echarts, el, theme, o);
    var rec = window.__charts.find(function (c) { return c.el === el; });
    if (rec) rec.inits++; else { rec = {el: el, inits: 1, sets: 0}; window.__charts.push(rec); }
    var setOption = inst.setOption;
    inst.setOption = function () { rec.sets++; return setOption.apply(inst, arguments); };
    return inst;
  };
  var last = null;
  window.__hookType = typeof window.useConversation;
  function Chat(props) {
    if (window.__hookType !== 'function') return h('p', {id: 'missing'}, 'useConversation is not defined');
    var chat = useConversation(props.opts);
    window.__chat = chat;
    if (chat.messages !== last) { last = chat.messages; window.__msgsVersion++; }
    return h('div', {id: 'chat'}, chat.messages.map(function (m) { return h(ChatMessage, {key: m.id, message: m}); }));
  }
  function App() {
    var s = React.useState({mounted: true, opts: ${JSON.stringify(opts)}});
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

const CONV = {id: 'c1', title: 'q1', created_at: '2026-10-30T10:00:00Z', updated_at: '2026-10-30T10:00:00Z', agents: []};
const DEFAULT_AUTO = {policy: {available: true, scope: 'data_only', agents: []}, list: {items: [], next_before: null}, create: CONV, stop: 'echo'};
const browser = await chromium.launch({headless: true, executablePath});
let frames = 0;

async function openPage(context, {clock = false} = {}) {
  const page = await context.newPage();
  // The clock is installed before the first navigation (D-8).
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
  await page.goto('http://use-conversation.test/');
  await page.addScriptTag({content: 'window.exports={};' + compiled});
  await page.evaluate(installHost);
  return {page, consoleErrors, clock};
}

// One scenario = one fresh artifact frame (and a fresh host log).
async function start(env, {opts = undefined, auto = {}, noBow = false, waitLoad = true} = {}) {
  const {page, consoleErrors} = env;
  consoleErrors.length = 0;
  if (env.clock) await page.clock.resume();  // the app loads with the clock running
  const name = 'art-' + (++frames);
  await page.evaluate(({code, name, auto}) => {
    document.querySelectorAll('iframe').forEach((f) => f.remove());
    window.__msgs = [];
    window.__host.log = [];
    window.__host.auto = auto;
    const f = document.createElement('iframe');
    f.name = name;
    f.sandbox = 'allow-scripts';
    f.style.width = '1000px';
    f.style.height = '3000px';
    f.srcdoc = exports.buildArtifactIframeHtml({data: {visualizations: [], runtime: {version: 11}}, code, reactBuild: 'development'});
    document.body.appendChild(f);
  }, {code: APP(opts === undefined ? null : opts, {noBow}), name, auto: {...DEFAULT_AUTO, ...auto}});
  const end = Date.now() + 20000;
  while (!(await page.evaluate(() => window.__msgs.length))) {
    if (Date.now() > end) throw new Error('the artifact never signalled');
    await sleep(25);
  }
  assert.deepEqual(await page.evaluate(() => window.__msgs.slice()), ['ARTIFACT_READY']);
  const frame = page.frame({name});
  assert.equal(await frame.evaluate(() => window.__hookType), 'function', 'window.useConversation is a hook');
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
    chat: () => frame.evaluate(() => {
      const c = window.__chat;
      return JSON.parse(JSON.stringify({messages: c.messages, conversations: c.conversations, conversationId: c.conversationId, policy: c.policy,
        loading: c.loading, sending: c.sending, error: c.error, keys: Object.keys(c).sort(), version: window.__msgsVersion, dispatches: window.__dispatches}));
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
    dom: (fn, arg) => frame.evaluate(fn, arg),
    async done() {
      assert.deepEqual(await page.evaluate(() => window.__msgs.slice()), ['ARTIFACT_READY'], 'no artifact error');
      assert.deepEqual(consoleErrors, [], 'no console errors');
    },
  };
  if (waitLoad && !noBow) await s.until((c) => !c.loading, 'the initial load');
  if (env.clock) {
    // Paused before the test triggers any history load; then one step = runFor(5000).
    const now = await page.evaluate(() => Date.now());
    await page.clock.pauseAt(now + 1000);
  }
  return s;
}

// History items, as messages() returns them.
const u = (id, text, extra = {}) => ({id, role: 'user', text, status: 'success', created_at: '2026-10-30T10:00:00Z', ...extra});
const a = (id, text, extra = {}) => ({id, role: 'assistant', text, status: 'success', created_at: '2026-10-30T10:00:01Z', tools: [], results: [], ...extra});
const page = (...items) => ({items, next_before: null});
const result = (id, extra = {}) => ({id, block: 'b1', title: 'Revenue ' + id, columns: [{field: 'm', headerName: 'Month', type: 'string'}, {field: 'v', headerName: 'Value', type: 'number'}],
  rows: [{m: 'Jan', v: 1}, {m: 'Feb', v: 2}], row_count: 2, column_count: 2, truncated: false, chart: null, ...extra});
const isLocal = (m) => String(m.id).startsWith('local-');
const texts = (c, role) => c.messages.filter((m) => m.role === role).map((m) => m.text);
const ids = (c) => c.messages.map((m) => m.id);

// A send through create + started: returns the send request.
async function sendStarted(s, text, mid) {
  await s.call('send', text);
  const req = await s.take('send', (e) => e.args.text === text);
  if (mid) await s.event(req.id, {type: 'started', message_id: mid, prompt_id: 'p-' + mid});
  return req;
}
// completed + the stream end + the re-read answered with `history`.
async function finish(s, req, history, completed = {type: 'completed', status: 'success'}) {
  if (completed) await s.event(req.id, completed);
  await s.reply(req.id, null);
  const read = await s.take('messages');
  await s.reply(read.id, history);
  await s.until((c) => !c.sending, 'the run end');
  return read;
}

const scenarios = [];
const scenario = (name, fn, {clock = false} = {}) => scenarios.push({name, fn, clock});

// ── L1, L2, L3: streaming shape ─────────────────────────────────────────────
scenario('L1 stream order, blocks, tools by id, results deduped by id, then the re-read', async (env) => {
  const s = await start(env);
  const first = await s.chat();
  assert.deepEqual(first.keys, ['conversationId', 'conversations', 'error', 'loading', 'messages', 'newConversation', 'policy', 'reload', 'select', 'send', 'sending', 'stop']);
  assert.deepEqual(first.policy, DEFAULT_AUTO.policy);
  assert.equal(await s.count('list', (e) => e.args.limit === 20), 1);
  await s.call('send', '  Revenue per month?  ');
  let c = await s.chat();
  assert.equal(c.sending, true);
  assert.deepEqual(c.messages.map((m) => [m.role, m.status, m.text]), [['user', 'pending', 'Revenue per month?'], ['assistant', 'in_progress', '']]);
  assert.deepEqual(c.messages[1].steps, [], 'a new answer has no steps yet');
  assert.ok(c.messages.every(isLocal));
  const create = await s.take('create');
  assert.deepEqual(create.args, {op: 'create', title: 'Revenue per month?', agents: undefined});
  const req = await s.take('send');
  assert.deepEqual([req.args.id, req.args.text], ['c1', 'Revenue per month?']);
  c = await s.until((c) => c.conversationId === 'c1', 'created');
  assert.deepEqual(c.conversations.map((x) => x.id), ['c1']);
  await s.event(req.id, {type: 'result', ...result('r0')});
  await s.event(req.id, {type: 'started', message_id: 'm1'});
  await s.events(req.id, [
    {type: 'text_delta', block: 'b1', text: 'Hel'},
    {type: 'tool', id: 't1', name: 'run_query', title: 'Running the query', status: 'running'},
    {type: 'text_delta', block: 'b1', text: 'lo'},
    {type: 'text_delta', block: 'b2', text: 'World'},
    {type: 'tool', id: 't1', name: 'run_query', title: 'Running the query', status: 'success'},
    {type: 'tool', name: 'make_chart', title: 'Chart', status: 'success'},
    {type: 'surprise', text: 'ignored'},
    {type: 'result', ...result('r1')},
    {type: 'result', ...result('r1', {title: 'duplicate'})},
    {type: 'result', ...result('r2')},
    {type: 'completed', status: 'success'},
    {type: 'text_delta', block: 'b1', text: ' after completed'},
  ]);
  c = await s.until((c) => c.messages[1].status === 'success', 'completed');
  const [user, asst] = c.messages;
  assert.equal(user.status, 'success', 'the user item is stored once started');
  assert.equal(asst.id, 'm1', 'the assistant takes the started message id');
  // As messages() reads it back: the last text block is the answer, earlier ones its steps.
  assert.equal(asst.text, 'World');
  assert.deepEqual(asst.steps, ['Hello']);
  assert.deepEqual(asst.tools, [{id: 't1', name: 'run_query', title: 'Running the query', status: 'success'}, {name: 'make_chart', title: 'Chart', status: 'success'}]);
  assert.deepEqual(asst.results.map((r) => [r.id, r.title, 'type' in r]), [['r0', 'Revenue r0', false], ['r1', 'Revenue r1', false], ['r2', 'Revenue r2', false]]);
  assert.equal(asst.results_incomplete, false);
  assert.equal(c.sending, true, 'still sending until the re-read');
  await s.dom(() => window.__chat.messages[1].results.forEach((r) => window.__seen.add(r)));
  await s.reply(req.id, null);
  const read = await s.take('messages');
  assert.deepEqual(read.args, {op: 'messages', id: 'c1', limit: 30, before: undefined});
  await s.reply(read.id, page(u('u1', 'Revenue per month?'), a('m1', 'World', {steps: ['Hello'], results: [result('r0'), result('r1'), result('r2')], tools: [{name: 'run_query', title: 'Running the query', status: 'success'}]})));
  c = await s.until((c) => !c.sending, 'the run end');
  assert.deepEqual(ids(c), ['u1', 'm1'], 'stored ids replace the optimistic ones');
  assert.deepEqual(c.messages[1].tools, [{name: 'run_query', title: 'Running the query', status: 'success'}], 'the stored message wins');
  assert.deepEqual([c.messages[1].text, c.messages[1].steps], ['World', ['Hello']], 'the re-read keeps the same answer and steps');
  assert.equal(await s.dom(() => window.__chat.messages[1].results.every((r) => window.__seen.has(r))), true, 'same-shape results keep their objects');
  assert.equal(c.error, null);
  await s.done();
});

scenario('L2 a text event replaces its block; a null block and a missing status have defaults', async (env) => {
  const s = await start(env);
  const req = await sendStarted(s, 'q1', 'm1');
  await s.events(req.id, [
    {type: 'text_delta', block: 'b1', text: 'Draft'},
    {type: 'text_delta', block: 'b2', text: 'Second'},
    {type: 'text', block: 'b1', text: 'Final'},
    {type: 'text_delta', block: null, text: 'Tail'},
  ]);
  let c = await s.until((c) => c.messages[1].text === 'Tail', 'the last block as the answer');
  assert.deepEqual(c.messages[1].steps, ['Final', 'Second'], 'earlier blocks are steps, in order, the replaced one included');
  await s.event(req.id, {type: 'completed'});
  c = await s.until((c) => c.messages[1].status === 'success', 'completed without status is success');
  await finish(s, req, page(u('u1', 'q1'), a('m1', 'Tail', {steps: ['Final', 'Second']})), null);
  await s.done();
});

for (const fixed of [true, false]) {
  scenario(`L3${fixed ? 'a' : 'b'} results_incomplete, re-read ${fixed ? 'fixes it' : 'still incomplete'}`, async (env) => {
    const s = await start(env);
    const req = await sendStarted(s, 'q1', 'm1');
    await s.events(req.id, [{type: 'text_delta', block: 'b1', text: 'Answer'}, {type: 'completed', status: 'success', results_incomplete: true}]);
    await s.until((c) => c.messages[1].results_incomplete === true, 'the flag');
    const stored = fixed ? a('m1', 'Answer', {results: [result('r1')]}) : a('m1', 'Answer', {results: [], results_incomplete: true});
    const c = await finish(s, req, page(u('u1', 'q1'), stored), null).then(() => s.chat());
    assert.equal(c.messages[1].results_incomplete, !fixed);
    assert.equal(c.messages[1].results.length, fixed ? 1 : 0);
    const note = await s.dom(() => document.querySelectorAll('[data-testid="bow-chat-incomplete"]').length);
    assert.equal(note, fixed ? 0 : 1);
    if (fixed) assert.equal(await s.dom(() => document.querySelectorAll('[data-testid="bow-chat-result"]').length), 1);
    await s.done();
  });
}

// ── L4: P6 under streaming ──────────────────────────────────────────────────
scenario('L4 a streamed chart keeps one init and its setOption count through deltas, completed and the re-read', async (env) => {
  const s = await start(env);
  const chart = {type: 'bar', x: 'm', y: ['v'], series: null};
  const req = await sendStarted(s, 'q1', 'm1');
  await s.event(req.id, {type: 'result', ...result('r1', {chart})});
  await s.dom(() => new Promise((r) => { const t = setInterval(() => { if (document.querySelector('canvas')) { clearInterval(t); r(); } }, 10); }));
  await s.dom(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  const base = await s.dom(() => window.__charts.map((c) => ({inits: c.inits, sets: c.sets})));
  assert.equal(base.length, 1);
  assert.ok(base[0].sets >= 1);
  for (let i = 0; i < 5; i++) {
    await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'x'});
    await s.until((c) => c.messages[1].text.length === i + 1, 'delta ' + i);
  }
  await finish(s, req, page(u('u1', 'q1'), a('m1', 'xxxxx', {results: [result('r1', {chart})]})));
  await s.dom(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  assert.deepEqual(await s.dom(() => window.__charts.map((c) => ({inits: c.inits, sets: c.sets}))), base);
  await s.done();
});

// ── L5, L6: stop ────────────────────────────────────────────────────────────
scenario('L5 stop after started: the stop request with the started id once, then the cancel frame, then a re-read', async (env) => {
  const s = await start(env);
  const req = await sendStarted(s, 'q1', 'm1');
  await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'Partial'});
  await s.until((c) => c.messages[1].text === 'Partial', 'the delta');
  await s.call('stop');
  let c = await s.chat();
  assert.equal(c.sending, false, 'sending false at once');
  assert.deepEqual(c.messages.map((m) => m.status), ['success', 'stopped']);
  const stop = await s.take('stop');
  assert.deepEqual(stop.args, {op: 'stop', id: 'c1', messageId: 'm1'});
  const cancel = await s.take('cancel', (e) => e.id === req.id);
  assert.ok(stop.seq < cancel.seq, 'the server stop goes before the local cancel');
  const read = await s.take('messages');
  await s.reply(read.id, page(u('u1', 'q1'), a('m1', 'Partial', {status: 'stopped'})));
  c = await s.until((c) => c.messages[0].id === 'u1', 'the re-read');
  assert.deepEqual(c.messages.map((m) => [m.id, m.status]), [['u1', 'success'], ['m1', 'stopped']]);
  await s.call('stop');
  await sleep(150);
  assert.equal(await s.count('stop'), 1, 'stopping twice sends one stop');
  await s.done();
});

scenario('L6 stop before started: the stop waits for started, then uses its id', async (env) => {
  const s = await start(env);
  const req = await sendStarted(s, 'q1', null);
  await s.call('stop');
  let c = await s.chat();
  assert.equal(c.sending, false);
  assert.deepEqual(c.messages.map((m) => m.status), ['success', 'stopped']);
  await sleep(200);
  assert.equal(await s.count('stop'), 0, 'no stop request before started');
  assert.equal(await s.count('cancel'), 0, 'the stream stays open until started');
  await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'ignored'});
  await s.event(req.id, {type: 'started', message_id: 'm9'});
  const stop = await s.take('stop');
  assert.deepEqual(stop.args, {op: 'stop', id: 'c1', messageId: 'm9'});
  const cancel = await s.take('cancel', (e) => e.id === req.id);
  assert.ok(stop.seq < cancel.seq);
  c = await s.chat();
  assert.equal(c.messages[1].text, '', 'events after a stop are ignored');
  const read = await s.take('messages');
  await s.reply(read.id, page(u('u1', 'q1'), a('m9', '', {status: 'stopped'})));
  c = await s.until((c) => ids(c).join() === 'u1,m9', 'the re-read');
  assert.equal(c.messages[1].status, 'stopped');
  await s.done();
});

// ── L7, L8: errors, policy ──────────────────────────────────────────────────
scenario('L7 an error frame after started: chat.error, assistant error, user success, a re-read', async (env) => {
  const s = await start(env);
  const req = await sendStarted(s, 'q1', 'm1');
  await s.fail(req.id, {code: 'QUOTA_EXCEEDED', message: 'The monthly quota is used up'});
  let c = await s.until((c) => c.error, 'the error');
  assert.deepEqual(c.error, {code: 'QUOTA_EXCEEDED', message: 'The monthly quota is used up'});
  assert.deepEqual(c.messages.map((m) => m.status), ['success', 'error']);
  const read = await s.take('messages');
  await s.reply(read.id, page(u('u1', 'q1'), a('m1', '', {status: 'error'})));
  c = await s.until((c) => !c.sending && c.messages[0].id === 'u1', 'the re-read');
  assert.deepEqual(c.messages.map((m) => m.status), ['success', 'error']);
  assert.equal(c.error.code, 'QUOTA_EXCEEDED', 'the error stays visible');
  await s.done();
});

scenario('L7 an error frame before started: user and assistant error, no re-read', async (env) => {
  const s = await start(env);
  const req = await sendStarted(s, 'q1', null);
  await s.fail(req.id, {code: 'UNAVAILABLE', message: 'The agents are unavailable'});
  const c = await s.until((c) => !c.sending, 'the run end');
  assert.deepEqual(c.error, {code: 'UNAVAILABLE', message: 'The agents are unavailable'});
  assert.deepEqual(c.messages.map((m) => m.status), ['error', 'error']);
  await sleep(200);
  assert.equal(await s.count('messages'), 0);
  await s.done();
});

scenario('L8 policy unavailable: no list, and send makes no request', async (env) => {
  const s = await start(env, {auto: {policy: {available: false, reason: 'not_member', scope: 'data_only', agents: []}}});
  await s.call('send', 'hello');
  const c = await s.chat();
  assert.deepEqual(c.error, {code: 'FORBIDDEN', message: 'not_member'});
  assert.equal(c.sending, false);
  assert.deepEqual(c.messages, []);
  assert.equal(c.policy.available, false);
  await sleep(150);
  assert.deepEqual((await s.log()).map((e) => e.op), ['policy']);
  await s.done();
});

// ── L9: unmount ─────────────────────────────────────────────────────────────
// MR-11 (reversed): leaving a running answer aborts only the local stream; the
// answer keeps running on the server and only chat.stop() stops it.
for (const started of [true, false]) {
  scenario(`L9 unmount during a send ${started ? 'after' : 'before'} started: the cancel frame, no server stop, no dispatch after unmount`, async (env) => {
    const s = await start(env);
    const req = await sendStarted(s, 'q1', started ? 'm1' : null);
    if (started) await s.until((c) => c.messages[1].id === 'm1', 'started');
    const before = (await s.log()).length;
    await s.dom(() => window.__setApp({mounted: false}));
    await s.dom(() => new Promise((r) => { const t = setInterval(() => { if (document.getElementById('unmounted')) { clearInterval(t); r(); } }, 5); }));
    await s.take('cancel', (e) => e.id === req.id);
    await sleep(50);
    const dispatches = await s.dom(() => window.__dispatches);
    if (!started) await s.event(req.id, {type: 'started', message_id: 'm1'});
    await s.events(req.id, [{type: 'text_delta', block: 'b1', text: 'late'}, {type: 'result', ...result('r1')}, {type: 'completed', status: 'success'}]);
    await s.reply(req.id, null);
    await sleep(300);
    assert.equal(await s.dom(() => window.__dispatches), dispatches, 'no dispatch after unmount');
    const after = (await s.log()).slice(before).map((e) => e.op);
    assert.deepEqual(after, ['cancel'], 'only the cancel frame after unmount: no server stop');
    await s.done();
  });
}

scenario('L9 unmount while a select load is in flight: its answer dispatches nothing', async (env) => {
  const s = await start(env);
  await s.call('select', 'A');
  const load = await s.take('messages', (e) => e.args.id === 'A');
  const before = (await s.log()).length;
  await s.dom(() => window.__setApp({mounted: false}));
  await s.dom(() => new Promise((r) => { const t = setInterval(() => { if (document.getElementById('unmounted')) { clearInterval(t); r(); } }, 5); }));
  await sleep(50);
  const dispatches = await s.dom(() => window.__dispatches);
  await s.reply(load.id, page(u('a1', 'from A')));
  await sleep(250);
  assert.equal(await s.dom(() => window.__dispatches), dispatches, 'no dispatch after unmount');
  assert.equal((await s.log()).length, before, 'no request after unmount');
  await s.done();
});

// ── L10, L11, L12: loads ────────────────────────────────────────────────────
scenario('L10 two selects answered out of order: the later selection wins', async (env) => {
  const s = await start(env);
  await s.call('select', 'A');
  const ra = await s.take('messages', (e) => e.args.id === 'A');
  await s.call('select', 'B');
  const rb = await s.take('messages', (e) => e.args.id === 'B');
  assert.equal((await s.chat()).loading, true);
  await s.reply(rb.id, page(u('b1', 'from B')));
  await s.until((c) => !c.loading, 'B loaded');
  await s.reply(ra.id, page(u('a1', 'from A')));
  await sleep(200);
  const c = await s.chat();
  assert.deepEqual([c.conversationId, ids(c), c.loading], ['B', ['b1'], false]);
  await s.done();
});

scenario('L11 an initial conversationId that is NOT_FOUND leaves conversationId null', async (env) => {
  const s = await start(env, {opts: {conversationId: 'gone'}, waitLoad: false});
  const read = await s.take('messages', (e) => e.args.id === 'gone');
  assert.equal((await s.chat()).loading, true);
  await s.fail(read.id, {code: 'NOT_FOUND', message: 'Conversation not found'});
  const c = await s.until((c) => !c.loading, 'the load');
  assert.deepEqual([c.conversationId, c.error && c.error.code], [null, 'NOT_FOUND']);
  await s.call('send', 'q1');
  await s.take('create');
  await s.done();
});

scenario('L12 no window.bow: chat.error UNAVAILABLE, send is a no-op', async (env) => {
  const s = await start(env, {noBow: true});
  let c = await s.until((c) => c.error, 'the error');
  assert.deepEqual([c.error.code, c.loading, c.sending], ['UNAVAILABLE', false, false]);
  await s.call('send', 'hello');
  await s.call('reload');
  await s.call('select', 'x');
  c = await s.chat();
  assert.equal(c.sending, false);
  await s.done();
});

// ── L13-L17: no duplicates after stop / error / failed re-read ───────────────
scenario('L13 stop then a second send: one copy of each user message, reload is idempotent', async (env) => {
  const s = await start(env);
  const r1 = await sendStarted(s, 'q1', 'm1');
  await s.call('stop');
  await s.take('stop');
  const read1 = await s.take('messages');
  await s.reply(read1.id, page(u('u1', 'q1'), a('m1', '', {status: 'stopped'})));
  await s.until((c) => c.messages[0].id === 'u1', 'the stopped re-read');
  const r2 = await sendStarted(s, 'q2', 'm2');
  await s.event(r2.id, {type: 'result', ...result('r2')});
  const full = page(u('u1', 'q1'), a('m1', '', {status: 'stopped'}), u('u2', 'q2'), a('m2', 'Answer', {results: [result('r2')]}));
  await finish(s, r2, full);
  let c = await s.chat();
  assert.deepEqual(texts(c, 'user'), ['q1', 'q2']);
  assert.ok(!c.messages.some(isLocal) && !c.messages.some((m) => m.status === 'pending'));
  await s.dom(() => window.__chat.messages.forEach((m) => m.results.forEach((r) => window.__seen.add(r))));
  const before = c;
  await s.call('reload');
  const again = await s.take('messages');
  await s.reply(again.id, full);
  c = await s.until((c) => c.version > before.version, 'the reload');
  assert.deepEqual(c.messages, before.messages, 'the same page merged twice is unchanged');
  assert.equal(await s.dom(() => window.__chat.messages.every((m) => m.results.every((r) => window.__seen.has(r)))), true, 'result objects are kept');
  void r1;
  await s.done();
});

scenario('L14 an error after started then a second send: one copy of each user message', async (env) => {
  const s = await start(env);
  const r1 = await sendStarted(s, 'q1', 'm1');
  await s.fail(r1.id, {code: 'INTERRUPTED', message: 'Request interrupted'});
  const read1 = await s.take('messages');
  await s.reply(read1.id, page(u('u1', 'q1'), a('m1', '', {status: 'error'})));
  await s.until((c) => !c.sending, 'the run end');
  const r2 = await sendStarted(s, 'q2', 'm2');
  await finish(s, r2, page(u('u1', 'q1'), a('m1', '', {status: 'error'}), u('u2', 'q2'), a('m2', 'Answer')));
  const c = await s.chat();
  assert.deepEqual(texts(c, 'user'), ['q1', 'q2']);
  assert.ok(!c.messages.some(isLocal));
  await s.done();
});

for (const stored of ['q1', 'q1 ', 'Q1']) {
  scenario(`L15 a failed re-read then a second send (stored text ${JSON.stringify(stored)}): one copy each`, async (env) => {
    const s = await start(env);
    const r1 = await sendStarted(s, 'q1', 'm1');
    await s.event(r1.id, {type: 'completed', status: 'success'});
    await s.reply(r1.id, null);
    const read1 = await s.take('messages');
    await s.fail(read1.id, {code: 'UNAVAILABLE', message: 'down'});
    let c = await s.until((c) => !c.sending, 'the run end');
    assert.deepEqual(c.messages.map((m) => m.status), ['success', 'success'], 'nothing stays pending');
    assert.equal(c.error, null, 'a failed re-read is silent');
    const r2 = await sendStarted(s, 'q2', 'm2');
    await finish(s, r2, page(u('u1', stored), a('m1', 'A1'), u('u2', 'q2'), a('m2', 'A2')));
    c = await s.chat();
    assert.deepEqual(ids(c), ['u1', 'm1', 'u2', 'm2']);
    await s.done();
  });
}

scenario('L16 stop before started, then a second send before the first started arrives', async (env) => {
  const s = await start(env);
  const r1 = await sendStarted(s, 'q1', null);
  await s.call('stop');
  assert.equal((await s.chat()).sending, false);
  const r2 = await sendStarted(s, 'q2', null);
  assert.equal((await s.chat()).sending, true);
  await s.event(r1.id, {type: 'started', message_id: 'm1'});
  const stop = await s.take('stop');
  assert.deepEqual(stop.args, {op: 'stop', id: 'c1', messageId: 'm1'});
  await s.take('cancel', (e) => e.id === r1.id);
  await sleep(150);
  assert.equal(await s.count('messages'), 0, "run 1's re-read is skipped while run 2 is active");
  assert.equal(await s.count('cancel', (e) => e.id === r2.id), 0, 'run 2 is untouched');
  await s.event(r2.id, {type: 'started', message_id: 'm2'});
  await finish(s, r2, page(u('u1', 'q1'), a('m1', '', {status: 'stopped'}), u('u2', 'q2'), a('m2', 'A2')));
  const c = await s.chat();
  assert.deepEqual(texts(c, 'user'), ['q1', 'q2']);
  assert.deepEqual(ids(c), ['u1', 'm1', 'u2', 'm2']);
  assert.equal(await s.count('messages'), 1);
  await s.done();
});

scenario('L17 create rejects: sending false, chat.error, both items error, a retry creates again', async (env) => {
  const s = await start(env, {auto: {create: 'hold'}});
  await s.call('send', 'q1');
  const create = await s.take('create');
  await s.fail(create.id, {code: 'QUOTA_EXCEEDED', message: 'No more conversations'});
  let c = await s.until((c) => !c.sending, 'the run end');
  assert.deepEqual([c.error.code, c.conversationId], ['QUOTA_EXCEEDED', null]);
  assert.deepEqual(c.messages.map((m) => m.status), ['error', 'error']);
  assert.equal(await s.count('send'), 0);
  await s.call('send', 'q1 again');
  const again = await s.take('create');
  await s.reply(again.id, CONV);
  await s.take('send');
  c = await s.chat();
  assert.equal(c.error, null, 'a new send clears the error');
  await s.done();
});

// ── L18, L18b: send while loading, double click ─────────────────────────────
scenario('L18 a send while a select is loading waits for it; a double click sends once', async (env) => {
  const s = await start(env);
  await s.call('select', 'A');
  const load = await s.take('messages', (e) => e.args.id === 'A');
  await s.call('send', 'x');
  let c = await s.chat();
  assert.equal(c.sending, true);
  await sleep(150);
  assert.equal(await s.count('send') + await s.count('create'), 0, 'no request while loading');
  await s.reply(load.id, page(u('a0', 'hello'), a('a1', 'hi')));
  const req = await s.take('send');
  assert.deepEqual([req.args.id, req.args.text], ['A', 'x']);
  c = await s.chat();
  assert.deepEqual(c.messages.map((m) => [m.role, m.text]), [['user', 'hello'], ['assistant', 'hi'], ['user', 'x'], ['assistant', '']]);
  await s.event(req.id, {type: 'started', message_id: 'm1'});
  await finish(s, req, page(u('a0', 'hello'), a('a1', 'hi'), u('u1', 'x'), a('m1', 'ok')));
  await s.dom(() => { window.__chat.send('twice'); window.__chat.send('twice'); });
  await s.take('send', (e) => e.args.text === 'twice');
  await sleep(150);
  assert.equal(await s.count('send', (e) => e.args.text === 'twice'), 1, 'two synchronous sends start one run');
  await s.done();
});

scenario('L18b the policy resolves unavailable while a send waits for the load', async (env) => {
  const s = await start(env, {auto: {policy: 'hold'}, waitLoad: false});
  const pol = await s.take('policy');
  await s.call('send', 'x');
  let c = await s.chat();
  assert.equal(c.sending, true);
  await s.reply(pol.id, {available: false, reason: 'not_member', scope: 'data_only', agents: []});
  c = await s.until((c) => !c.sending, 'the run end');
  assert.deepEqual(c.error, {code: 'FORBIDDEN', message: 'not_member'});
  assert.deepEqual(c.messages, []);
  await sleep(150);
  assert.equal(await s.count('create') + await s.count('send'), 0);
  await s.done();
});

// ── L19, L20, L21: stop and leave timing ────────────────────────────────────
scenario('L19 stop during the post-completed re-read is a no-op', async (env) => {
  const s = await start(env);
  const req = await sendStarted(s, 'q1', 'm1');
  await s.event(req.id, {type: 'completed', status: 'success'});
  await s.reply(req.id, null);
  const read = await s.take('messages');
  await s.call('stop');
  await sleep(150);
  let c = await s.chat();
  assert.deepEqual([c.sending, c.messages[1].status], [true, 'success']);
  assert.equal(await s.count('stop'), 0);
  await s.reply(read.id, page(u('u1', 'q1'), a('m1', 'A')));
  c = await s.until((c) => !c.sending, 'the run end');
  assert.equal(c.messages[1].status, 'success');
  await s.done();
});

for (const fetched of ['success', 'in_progress']) {
  scenario(`L20 completed after a local stop is ignored; the re-read says ${fetched}`, async (env) => {
    const s = await start(env);
    const req = await sendStarted(s, 'q1', 'm1');
    await s.until((c) => c.messages[1].id === 'm1', 'started');
    await s.call('stop');
    await s.take('stop');
    await s.event(req.id, {type: 'completed', status: 'success'});
    const read = await s.take('messages');
    let c = await s.chat();
    assert.equal(c.messages[1].status, 'stopped', 'the first terminal wins locally');
    await s.reply(read.id, page(u('u1', 'q1'), a('m1', 'All of it', {status: fetched})));
    c = await s.until((c) => c.messages[0].id === 'u1', 'the re-read');
    assert.equal(c.messages[1].status, fetched === 'success' ? 'success' : 'stopped', 'server truth, except a fetched in_progress');
    if (fetched === 'in_progress') {
      await sleep(5500);
      assert.equal(await s.count('messages'), 1, 'no refresh for a held stopped answer');
    }
    await s.done();
  });
}

scenario('L20 stop before started, the re-read returns the started id as in_progress: it shows stopped', async (env) => {
  const s = await start(env);
  const req = await sendStarted(s, 'q1', null);
  await s.call('stop');
  await s.event(req.id, {type: 'started', message_id: 'm9'});
  await s.take('stop');
  const read = await s.take('messages');
  await s.reply(read.id, page(u('u1', 'q1'), a('m9', '', {status: 'in_progress'})));
  const c = await s.until((c) => c.messages[0].id === 'u1', 'the re-read');
  assert.deepEqual(c.messages.map((m) => [m.id, m.status]), [['u1', 'success'], ['m9', 'stopped']]);
  assert.ok(!c.messages.some(isLocal));
  await s.done();
});

for (const [how, started] of [['select', true], ['select', false], ['newConversation', true], ['newConversation', false]]) {
  scenario(`L21 ${how}() during a run ${started ? 'after' : 'before'} started aborts it locally and does not stop it on the server`, async (env) => {
    const s = await start(env);
    const req = await sendStarted(s, 'q1', started ? 'm1' : null);
    if (started) await s.until((c) => c.messages[1].id === 'm1', 'started');
    if (how === 'select') await s.call('select', 'B'); else await s.call('newConversation');
    let c = await s.chat();
    assert.equal(c.sending, false, 'sending false at once');
    assert.deepEqual(c.messages, []);
    await s.take('cancel', (e) => e.id === req.id);
    if (!started) await s.event(req.id, {type: 'started', message_id: 'm1'});
    await s.events(req.id, [{type: 'text_delta', block: 'b1', text: 'late'}, {type: 'completed', status: 'success'}]);
    if (how === 'select') {
      const load = await s.take('messages', (e) => e.args.id === 'B');
      await s.reply(load.id, page(u('b1', 'from B')));
      await s.until((c) => !c.loading, 'B loaded');
    }
    await sleep(200);
    c = await s.chat();
    assert.deepEqual(ids(c), how === 'select' ? ['b1'] : []);
    assert.equal(c.conversationId, how === 'select' ? 'B' : null);
    assert.equal(await s.count('stop'), 0, 'leaving sends no server stop');
    assert.equal(await s.count('messages', (e) => e.args.id === 'c1'), 0, 'no re-read of the left conversation');
    await s.done();
  });
}

for (const stopFirst of [true, false]) {
  scenario(`L21 ${stopFirst ? 'stop while creating, then leave' : 'leave while creating'}: no server stop, no send, nothing applied`, async (env) => {
    const s = await start(env, {auto: {create: 'hold'}});
    await s.call('send', 'q1');
    const create = await s.take('create');
    if (stopFirst) await s.call('stop');
    await s.call('newConversation');
    await s.reply(create.id, CONV);
    await sleep(250);
    const c = await s.chat();
    assert.deepEqual([c.sending, c.messages.length, c.conversationId], [false, 0, null]);
    assert.deepEqual([await s.count('send'), await s.count('stop')], [0, 0]);
    await s.done();
  });
}

scenario('L21 a leave after completed (during the re-read) sends no stop', async (env) => {
  const s = await start(env);
  const req = await sendStarted(s, 'q1', 'm1');
  await s.event(req.id, {type: 'completed', status: 'success'});
  await s.reply(req.id, null);
  const read = await s.take('messages');
  await s.call('newConversation');
  await s.reply(read.id, page(u('u1', 'q1'), a('m1', 'A')));
  await sleep(200);
  const c = await s.chat();
  assert.deepEqual([c.sending, c.messages.length, await s.count('stop')], [false, 0, 0]);
  await s.done();
});

// ── remediation: initial-load errors, reload errors, late leave, created kept ─
scenario('R1 the initial list rejects after an early select: chat.error is set', async (env) => {
  const s = await start(env, {auto: {list: 'hold'}, waitLoad: false});
  const list = await s.take('list');
  await s.call('select', 'A');
  const read = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(read.id, page(u('a1', 'from A')));
  await s.until((c) => !c.loading && c.messages.length === 1, 'A loaded');
  await s.fail(list.id, {code: 'UNAVAILABLE', message: 'list down'});
  const c = await s.until((c) => c.error, 'the list error');
  assert.deepEqual([c.error.code, c.error.message, c.conversationId, ids(c)], ['UNAVAILABLE', 'list down', 'A', ['a1']]);
  await s.done();
});

scenario('R1 the policy rejects after newConversation: chat.error is set', async (env) => {
  const s = await start(env, {auto: {policy: 'hold'}, waitLoad: false});
  const policy = await s.take('policy');
  await s.call('newConversation');
  await s.fail(policy.id, {code: 'UNAVAILABLE', message: 'policy down'});
  const c = await s.until((c) => c.error, 'the policy error');
  assert.deepEqual([c.error.code, c.error.message, c.conversationId], ['UNAVAILABLE', 'policy down', null]);
  await s.done();
});

scenario('R2 a reload whose messages() rejects sets chat.error; a following successful reload clears it', async (env) => {
  const s = await start(env);
  await s.call('select', 'A');
  const load = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(load.id, page(u('a1', 'from A')));
  await s.until((c) => !c.loading && c.messages.length === 1, 'A loaded');
  await s.call('reload');
  const r1 = await s.take('messages', (e) => e.args.id === 'A');
  await s.fail(r1.id, {code: 'UNAVAILABLE', message: 'reread down'});
  let c = await s.until((c) => c.error, 'the reload error');
  assert.deepEqual([c.error.code, c.error.message, ids(c)], ['UNAVAILABLE', 'reread down', ['a1']]);
  await s.call('reload');
  const r2 = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(r2.id, page(u('a1', 'from A'), a('a2', 'more')));
  c = await s.until((c) => c.messages.length === 2 && !c.error, 'the cleared error');
  assert.equal(c.error, null);
  await s.done();
});

scenario('R2 a reload whose list() rejects sets chat.error', async (env) => {
  const s = await start(env);
  await s.page.evaluate(() => { window.__host.auto.list = {__error: {code: 'UNAVAILABLE', message: 'list down'}}; });
  await s.call('reload');
  const c = await s.until((c) => c.error, 'the reload list error');
  assert.deepEqual([c.error.code, c.error.message], ['UNAVAILABLE', 'list down']);
  await s.done();
});

for (const how of ['unmount', 'select']) {
  scenario(`R3 a leave (${how}) after completed but before the stream ends issues no re-read`, async (env) => {
    const s = await start(env);
    const req = await sendStarted(s, 'q1', 'm1');
    await s.event(req.id, {type: 'completed', status: 'success'});
    await s.until((c) => c.messages[1].status === 'success', 'completed');
    const before = (await s.log()).length;
    if (how === 'unmount') {
      await s.dom(() => window.__setApp({mounted: false}));
      await s.dom(() => new Promise((r) => { const t = setInterval(() => { if (document.getElementById('unmounted')) { clearInterval(t); r(); } }, 5); }));
    } else {
      await s.call('select', 'B');
    }
    await s.reply(req.id, null);
    await sleep(300);
    const after = (await s.log()).slice(before);
    assert.deepEqual(after.filter((e) => e.op === 'messages' && e.args.id !== 'B').map((e) => e.args.id), [], 'no re-read after the leave');
    assert.equal(after.filter((e) => e.op === 'stop').length, 0, 'no stop after completed');
    await s.done();
  });
}

scenario('R4 the late initial list keeps a conversation created meanwhile', async (env) => {
  const s = await start(env, {auto: {list: 'hold'}, waitLoad: false});
  const list = await s.take('list');
  await s.call('newConversation');
  const req = await sendStarted(s, 'q1', 'm1');
  await s.until((c) => c.conversationId === 'c1' && c.conversations.length === 1, 'created');
  await s.reply(list.id, page({id: 'c0', title: 'older', created_at: null, updated_at: null, agents: []}));
  const c = await s.until((c) => c.conversations.length > 1 || c.conversations[0].id !== 'c1', 'the late list');
  assert.deepEqual(c.conversations.map((x) => x.id), ['c1', 'c0']);
  await finish(s, req, page(u('u1', 'q1'), a('m1', 'A')));
  await s.done();
});

scenario('R5 a reload in flight does not clear an error a send applied meanwhile (create rejects)', async (env) => {
  const s = await start(env);
  await s.page.evaluate(() => { window.__host.auto.list = 'hold'; window.__host.auto.create = 'hold'; });
  const before = (await s.log()).length;
  await s.call('reload');
  const list = await s.take('list', (e) => e.seq >= before);
  await s.call('send', 'q1');
  const create = await s.take('create');
  await s.fail(create.id, {code: 'QUOTA_EXCEEDED', message: 'No more conversations'});
  await s.until((c) => !c.sending && c.error, 'the create error');
  await s.reply(list.id, page());
  await sleep(200);
  const c = await s.chat();
  assert.deepEqual(c.messages.map((m) => m.status), ['error', 'error']);
  assert.deepEqual(c.error, {code: 'QUOTA_EXCEEDED', message: 'No more conversations'}, 'the send error stays visible');
  await s.done();
});

scenario('R5 a reload in flight across an early end does not clear INTERRUPTED (its list answered during the re-read)', async (env) => {
  const s = await start(env);
  const req = await sendStarted(s, 'q1', 'm1');
  await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'Part'});
  await s.page.evaluate(() => { window.__host.auto.list = 'hold'; });
  const before = (await s.log()).length;
  await s.call('reload');
  const list = await s.take('list', (e) => e.seq >= before);
  await s.reply(req.id, null);
  await s.until((c) => c.error && c.error.code === 'INTERRUPTED', 'the early end');
  const read = await s.take('messages');
  await s.reply(list.id, page(CONV));
  await sleep(200);
  let c = await s.chat();
  assert.equal(c.error && c.error.code, 'INTERRUPTED', 'the reload list does not clear it during the re-read');
  await s.reply(read.id, page(u('u1', 'q1'), a('m1', 'Part', {status: 'error'})));
  c = await s.until((c) => !c.sending, 'the run end');
  assert.equal(c.error && c.error.code, 'INTERRUPTED', 'still visible after the run end');
  await s.done();
});

scenario('R6 the initial list rejects after the initial history error was cleared by select: chat.error is set', async (env) => {
  const s = await start(env, {opts: {conversationId: 'X'}, auto: {list: 'hold'}, waitLoad: false});
  const list = await s.take('list');
  const hist = await s.take('messages', (e) => e.args.id === 'X');
  await s.fail(hist.id, {code: 'UNAVAILABLE', message: 'history down'});
  await s.until((c) => c.error, 'the history error');
  await s.call('select', 'B');
  const load = await s.take('messages', (e) => e.args.id === 'B');
  await s.reply(load.id, page(u('b1', 'from B')));
  await s.until((c) => !c.loading && c.messages.length === 1 && !c.error, 'B loaded, error cleared');
  await s.fail(list.id, {code: 'UNAVAILABLE', message: 'list down'});
  const c = await s.until((c) => c.error, 'the list error', 1500).catch((e) => { throw new Error('the list failure is silent: ' + e.message); });
  assert.deepEqual([c.error.code, c.error.message, c.conversationId, c.conversations], ['UNAVAILABLE', 'list down', 'B', []]);
  await s.done();
});

// ── L23, L24, L25, L26b ─────────────────────────────────────────────────────
scenario('L23 the completed re-read returns the answer as in_progress: the local message is kept whole', async (env) => {
  const s = await start(env);
  const req = await sendStarted(s, 'q1', 'm1');
  await s.events(req.id, [{type: 'text_delta', block: 'b1', text: 'Full answer'}, {type: 'result', ...result('r1')}]);
  await s.until((c) => c.messages[1].results.length === 1, 'the result');
  await s.dom(() => window.__chat.messages[1].results.forEach((r) => window.__seen.add(r)));
  await finish(s, req, page(u('u1', 'q1'), a('m1', '', {status: 'in_progress'})));
  const c = await s.chat();
  assert.deepEqual([c.messages[1].id, c.messages[1].status, c.messages[1].text], ['m1', 'success', 'Full answer']);
  assert.equal(await s.dom(() => window.__seen.has(window.__chat.messages[1].results[0])), true);
  assert.equal(c.messages[0].id, 'u1');
  await s.done();
});

scenario('L24 options: conversationId is read once at mount, agents and title at each send', async (env) => {
  const s = await start(env, {opts: {agents: ['ag1']}});
  await s.dom(() => window.__setApp({mounted: true, opts: {conversationId: 'X', agents: ['ag2'], title: 'Sales chat'}}));
  await sleep(200);
  assert.equal(await s.count('messages'), 0, 'a later conversationId is ignored');
  assert.equal((await s.chat()).conversationId, null);
  await s.call('send', 'q1');
  const create = await s.take('create');
  assert.deepEqual(create.args, {op: 'create', title: 'Sales chat', agents: ['ag2']});
  await s.done();
});

for (const rejects of [false, true]) {
  scenario(`L25 stop while creating, then send at once: one thread${rejects ? ' (the held create rejects)' : ''}`, async (env) => {
    const s = await start(env, {auto: {create: 'hold'}});
    await s.call('send', 'q1');
    const c1 = await s.take('create');
    await s.call('stop');
    let c = await s.chat();
    assert.deepEqual([c.sending, ...c.messages.map((m) => m.status)], [false, 'stopped', 'stopped']);
    await s.call('send', 'q2');
    await sleep(150);
    assert.equal(await s.count('create'), 1, 'no second create while the first is pending');
    if (rejects) {
      await s.fail(c1.id, {code: 'UNAVAILABLE', message: 'down'});
      const own = await s.take('create');
      assert.equal(own.args.title, 'q2');
      await s.reply(own.id, {...CONV, id: 'c2', title: 'q2'});
    } else {
      await s.reply(c1.id, CONV);
    }
    const conv = rejects ? 'c2' : 'c1';
    const req = await s.take('send');
    assert.deepEqual([req.args.id, req.args.text], [conv, 'q2']);
    c = await s.until((c) => c.conversationId === conv, 'the conversation');
    assert.deepEqual(c.conversations.map((x) => x.id), [conv]);
    await s.event(req.id, {type: 'started', message_id: 'm2'});
    await finish(s, req, page(u('u2', 'q2'), a('m2', 'A2')));
    c = await s.chat();
    assert.deepEqual(ids(c), ['u2', 'm2']);
    assert.deepEqual(texts(c, 'user'), ['q2'], 'no q1 copy');
    assert.equal(await s.count('send'), 1);
    assert.equal(await s.count('create'), rejects ? 2 : 1);
    await s.done();
  });
}

scenario('L26b a stopped run\'s re-read answered after a later run ended is dropped; so is an older reload', async (env) => {
  const s = await start(env);
  await sendStarted(s, 'q1', 'm1');
  await s.until((c) => c.messages[1].id === 'm1', 'started');
  await s.call('stop');
  const held = await s.take('messages');
  const r2 = await sendStarted(s, 'q2', 'm2');
  await finish(s, r2, page(u('u1', 'q1'), a('m1', '', {status: 'stopped'}), u('u2', 'q2'), a('m2', 'v0')));
  const before = await s.chat();
  await s.reply(held.id, page(u('u1', 'q1'), a('m1', '', {status: 'in_progress'})));
  await sleep(250);
  assert.deepEqual((await s.chat()).messages, before.messages, 'the stale re-read is dropped');
  await s.call('reload');
  const x1 = await s.take('messages');
  await s.call('reload');
  const x2 = await s.take('messages');
  await s.reply(x2.id, page(u('u1', 'q1'), a('m1', '', {status: 'stopped'}), u('u2', 'q2'), a('m2', 'v2')));
  await s.until((c) => c.messages[3].text === 'v2', 'the newer reload');
  await s.reply(x1.id, page(u('u1', 'q1'), a('m1', '', {status: 'stopped'}), u('u2', 'q2'), a('m2', 'v1')));
  await sleep(250);
  assert.equal((await s.chat()).messages[3].text, 'v2', 'the older reload is dropped');
  await s.done();
});

// ── clock context: L22, L22b, L22c, L26a ────────────────────────────────────
// page.clock fakes setTimeout (the hook's refresh timer); every request is
// answered before the clock moves on; waits are node-side polls.
async function step(s) {
  const n = await s.count('messages');
  await s.page.clock.runFor(5000);
  return n;
}
async function applied(s, version) { return s.until((c) => c.version > version, 'the applied refresh'); }
async function settle() { await sleep(60); }
const inProgress = (id) => a(id, 'Working', {status: 'in_progress'});

scenario('L22 a history answer in progress: indicator, a refresh every 5 s until terminal, at most 24', async (env) => {
  const s = await start(env);
  await s.call('select', 'A');
  let load = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(load.id, page(u('u0', 'q0'), inProgress('a0')));
  let c = await s.until((c) => !c.loading, 'A loaded');
  assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-typing"]')?.textContent), 'Answer in progress...');
  await settle();
  await step(s);
  const r = await s.take('messages', (e) => e.args.id === 'A');
  assert.equal(await s.count('messages'), 2, 'exactly one refresh after one interval');
  await s.reply(r.id, page(u('u0', 'q0'), a('a0', 'Done')));
  c = await applied(s, c.version);
  assert.equal(c.messages[1].status, 'success');
  assert.equal(await s.dom(() => document.querySelectorAll('[data-testid="bow-chat-typing"]').length), 0);
  await settle();
  await step(s);
  await settle();
  assert.equal(await s.count('messages'), 2, 'no refresh once terminal');

  // The cap.
  await s.call('select', 'B');
  load = await s.take('messages', (e) => e.args.id === 'B');
  await s.reply(load.id, page(u('u0', 'q0'), inProgress('b0')));
  c = await s.until((c) => !c.loading && c.conversationId === 'B', 'B loaded');
  for (let i = 0; i < 24; i++) {
    await settle();
    await step(s);
    const req = await s.take('messages', (e) => e.args.id === 'B', 'refresh ' + (i + 1));
    await s.reply(req.id, page(u('u0', 'q0'), inProgress('b0')));
    c = await applied(s, c.version);
  }
  await settle();
  await step(s);
  await settle();
  assert.equal(await s.count('messages', (e) => e.args.id === 'B'), 25, 'one load plus exactly 24 refreshes');

  // newConversation() cancels a pending refresh.
  await s.call('select', 'C');
  load = await s.take('messages', (e) => e.args.id === 'C');
  await s.reply(load.id, page(u('u0', 'q0'), inProgress('c0')));
  await s.until((c) => !c.loading && c.conversationId === 'C', 'C loaded');
  await settle();
  await s.call('newConversation');
  await step(s);
  await settle();
  assert.equal(await s.count('messages', (e) => e.args.id === 'C'), 1, 'no refresh after newConversation()');
  await s.done();
}, {clock: true});

for (const how of ['error', 'early end']) {
  scenario(`L22${how === 'error' ? 'b' : 'c'} ${how} after started while the server still runs it: one refresh, then terminal`, async (env) => {
    const s = await start(env);
    const req = await sendStarted(s, 'q1', 'm1');
    if (how === 'error') await s.fail(req.id, {code: 'UNAVAILABLE', message: 'The connection dropped'});
    else await s.reply(req.id, null);
    const read = await s.take('messages');
    await s.reply(read.id, page(u('u1', 'q1'), inProgress('m1')));
    let c = await s.until((c) => !c.sending, 'the run end');
    assert.equal(c.error.code, how === 'error' ? 'UNAVAILABLE' : 'INTERRUPTED');
    assert.equal(c.messages[1].status, 'in_progress');
    assert.equal(await s.dom(() => document.querySelectorAll('[data-testid="bow-chat-typing"]').length), 1);
    await settle();
    await step(s);
    const r = await s.take('messages');
    assert.equal(await s.count('messages'), 2);
    await s.reply(r.id, page(u('u1', 'q1'), a('m1', 'Done')));
    c = await applied(s, c.version);
    assert.equal(c.messages[1].status, 'success');
    assert.equal(await s.dom(() => document.querySelectorAll('[data-testid="bow-chat-typing"]').length), 0);
    await settle();
    await step(s);
    await settle();
    assert.equal(await s.count('messages'), 2, 'no further refresh');
    await s.done();
  }, {clock: true});
}

scenario('L26a a refresh in flight when a send starts, answered after that run ended, is dropped', async (env) => {
  const s = await start(env);
  await s.call('select', 'A');
  const load = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(load.id, page(u('u0', 'q0'), inProgress('a0')));
  await s.until((c) => !c.loading, 'A loaded');
  await settle();
  await step(s);
  const stale = await s.take('messages', (e) => e.args.id === 'A');
  const req = await sendStarted(s, 'q1', 'm1');
  assert.equal(req.args.id, 'A');
  await finish(s, req, page(u('u0', 'q0'), a('a0', 'Done'), u('u1', 'q1'), a('m1', 'A1')));
  const before = await s.chat();
  await s.reply(stale.id, page(u('u0', 'q0'), inProgress('a0')));
  await sleep(250);
  const c = await s.chat();
  assert.deepEqual(c.messages, before.messages);
  assert.equal(c.messages[1].status, 'success');
  assert.equal(await s.dom(() => document.querySelectorAll('[data-testid="bow-chat-typing"]').length), 0);
  await s.done();
}, {clock: true});

scenario('L26a variant: the run\'s re-read fails; the refresh issued before the send is still dropped', async (env) => {
  const s = await start(env);
  await s.call('select', 'A');
  const load = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(load.id, page(u('u0', 'q0'), inProgress('a0')));
  await s.until((c) => !c.loading, 'A loaded');
  await settle();
  await step(s);
  const stale = await s.take('messages', (e) => e.args.id === 'A');
  const req = await sendStarted(s, 'q1', 'm1');
  await s.event(req.id, {type: 'completed', status: 'success'});
  await s.reply(req.id, null);
  const read = await s.take('messages', (e) => e.seq > stale.seq);
  await s.fail(read.id, {code: 'UNAVAILABLE', message: 'down'});
  const before = await s.until((c) => !c.sending, 'the run end');
  await s.reply(stale.id, page(u('u0', 'q0'), a('a0', 'Done')));
  await sleep(250);
  const c = await s.chat();
  assert.equal(c.version, before.version, 'the refresh issued before the send never applies');
  assert.equal(c.messages[1].status, 'in_progress');
  await s.done();
}, {clock: true});

scenario('L27 leave a running answer, select it back: history shows it in progress, the refresh turns it to success', async (env) => {
  const s = await start(env);
  const req = await sendStarted(s, 'q1', 'm1');
  await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'Part'});
  await s.until((c) => c.messages[1].text === 'Part', 'the first delta');
  await s.call('newConversation');
  await s.take('cancel', (e) => e.id === req.id);
  await s.call('select', 'c1');
  const load = await s.take('messages', (e) => e.args.id === 'c1');
  await s.event(req.id, {type: 'text_delta', block: 'b1', text: ' late'});
  await s.reply(load.id, page(u('u1', 'q1'), a('m1', 'Part', {status: 'in_progress'})));
  let c = await s.until((c) => !c.loading && c.conversationId === 'c1', 'c1 loaded');
  assert.deepEqual([ids(c), c.messages[1].status, c.messages[1].text, c.sending], [['u1', 'm1'], 'in_progress', 'Part', false]);
  assert.equal(await s.dom(() => document.querySelectorAll('[data-testid="bow-chat-typing"]').length), 1);
  await settle();
  await step(s);
  const r = await s.take('messages', (e) => e.args.id === 'c1');
  await s.reply(r.id, page(u('u1', 'q1'), a('m1', 'Part and the rest')));
  c = await applied(s, c.version);
  assert.deepEqual([c.messages[1].status, c.messages[1].text], ['success', 'Part and the rest']);
  assert.equal(await s.dom(() => document.querySelectorAll('[data-testid="bow-chat-typing"]').length), 0);
  assert.equal(await s.count('stop'), 0, 'leaving never stopped the server run');
  await s.done();
}, {clock: true});

// ── run ─────────────────────────────────────────────────────────────────────
const only = process.env.ONLY ? new RegExp(process.env.ONLY) : null;
let failed = 0;
try {
  const env = await openPage(await browser.newContext({viewport: {width: 1100, height: 900}}));
  let clockEnv = null;
  for (const sc of scenarios) {
    if (only && !only.test(sc.name)) continue;
    // Timer-driven rules run in a dedicated context used for nothing else.
    if (sc.clock && !clockEnv) clockEnv = await openPage(await browser.newContext({viewport: {width: 1100, height: 900}}), {clock: true});
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
console.log('artifactUseConversation: OK');

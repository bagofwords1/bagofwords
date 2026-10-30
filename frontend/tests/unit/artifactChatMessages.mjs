// Real Chromium: <ChatMessages chat={chat}/> (artifact-globals.js), the
// scrolling transcript, driven by useConversation against the fake live host.
// It opens at the newest message, follows an answer only while the user is at
// the bottom, keeps the row under a reader in place (growth above or below,
// older pages), and shows "Jump to latest", the loading/empty slots and errors.
// Scroll checks never run under page.clock; waits are node-side polls, ±2 px.
//
//   cd frontend && PW_CHROMIUM_PATH=/path/to/chrome node tests/unit/artifactChatMessages.mjs
import assert from 'node:assert/strict';
import {start, run, u, a, pageOf, result, history, sendStarted, finish, sleep} from './support/chatHarness.mjs';

// Every chart instance records its init count and setOption calls, by the
// message row it renders in.
const SPY = `
  window.__charts = [];
  (function () {
    var init = echarts.init;
    echarts.init = function (el, theme, o) {
      var inst = init.call(echarts, el, theme, o);
      var row = el.closest('[data-message-id]');
      var rec = window.__charts.find(function (c) { return c.el === el; });
      if (rec) rec.inits++; else { rec = {el: el, row: row ? row.getAttribute('data-message-id') : null, inits: 1, sets: 0}; window.__charts.push(rec); }
      var setOption = inst.setOption;
      inst.setOption = function () { rec.sets++; return setOption.apply(inst, arguments); };
      return inst;
    };
  })();`;

// Geometry helpers inside the frame: the resolved scroller (the list when it
// overflows, else the document), the distance to the bottom, a row's offset
// from the scroller's top edge and the first row visible at the top.
const GEO = `
  window.__list = function () { return document.querySelector('[data-testid="bow-chat-messages"]'); };
  window.__sc = function () { var el = window.__list(); return el && el.scrollHeight > el.clientHeight + 1 ? el : document.scrollingElement; };
  window.__geo = function () {
    var el = window.__list(), sc = window.__sc();
    return {doc: sc !== el, top: sc.scrollTop, dist: sc.scrollHeight - sc.scrollTop - sc.clientHeight, max: sc.scrollHeight - sc.clientHeight, client: sc.clientHeight};
  };
  window.__edge = function () { var sc = window.__sc(); return sc === document.scrollingElement ? 0 : sc.getBoundingClientRect().top; };
  window.__offset = function (id) {
    var row = document.querySelector('[data-testid="bow-chat-row"][data-message-id="' + id + '"]');
    return row ? row.getBoundingClientRect().top - window.__edge() : null;
  };
  window.__firstVisible = function () {
    var edge = window.__edge(), rows = document.querySelectorAll('[data-testid="bow-chat-row"]');
    for (var i = 0; i < rows.length; i++) { var r = rows[i].getBoundingClientRect(); if (r.bottom > edge + 1) return {id: rows[i].getAttribute('data-message-id'), offset: r.top - edge}; }
    return null;
  };
  window.__scrollTo = function (top) { window.__sc().scrollTop = top; };`;

// mode: 'bounded' (a 400 px flex column), 'unbounded' (no height: the document
// scrolls), 'hidden' (display:none until window.__show()), 'tabs' (the second
// tab of the owner's Tabs; window.__setTab).
const msgApp = ({opts = null, mode = 'bounded', labels = 'undefined', empty = 'undefined', pre = ''} = {}) => `${pre}<script>
(function () {
  var h = React.createElement;
  ${SPY}
  ${GEO}
  window.__componentType = typeof window.ChatMessages;
  window.__msgsVersion = 0;
  var MODE = ${JSON.stringify(mode)};
  function List(props) {
    if (typeof window.ChatMessages !== 'function') return h('p', {id: 'missing'}, 'ChatMessages is not defined');
    return h(window.ChatMessages, {chat: props.chat, labels: ${labels}, empty: ${empty}});
  }
  function Box(props) { return h('div', {id: 'box', style: {height: '400px', display: 'flex', flexDirection: 'column'}}, h(List, {chat: props.chat})); }
  function App() {
    var chat = useConversation(${JSON.stringify(opts)});
    window.__chat = chat;
    // Shown outside React (a style change, no re-render): only the list's own
    // size observer can notice it.
    window.__show = function () { document.getElementById('hidebox').style.display = 'block'; };
    var tab = React.useState('a');
    window.__setTab = tab[1];
    if (MODE === 'unbounded') return h('div', null, h(List, {chat: chat}));
    if (MODE === 'footer') return h('div', null, [h(List, {key: 'l', chat: chat}), h('div', {key: 'f', id: 'footer', style: {height: '1500px'}}, 'a footer')]);
    if (MODE === 'cardBelow') return h('div', null, [
      h('div', {key: 'tall', id: 'tall', style: {height: '2000px'}}, 'the page above'),
      h('div', {key: 'card', id: 'box', style: {height: '300px', display: 'flex', flexDirection: 'column'}}, h(List, {chat: chat}))]);
    if (MODE === 'card') return h('div', null, [
      h('div', {key: 'card', id: 'box', style: {height: '300px', display: 'flex', flexDirection: 'column'}}, h(List, {chat: chat})),
      h('div', {key: 'tall', id: 'tall', style: {height: '2000px'}}, 'the rest of the page')]);
    if (MODE === 'hidden') return h('div', {id: 'hidebox', style: {display: 'none'}}, h(Box, {chat: chat}));
    if (MODE === 'tabs') return h(Tabs, {value: tab[0], onChange: tab[1], items: [
      {value: 'a', label: 'A', content: h('p', null, 'first tab')},
      {value: 'b', label: 'B', content: function () { return h(Box, {chat: chat}); }}
    ]});
    return h(Box, {chat: chat});
  }
  ReactDOM.createRoot(document.getElementById('root')).render(h(App));
})();
</script>`;

const CHART = {type: 'bar', x: 'm', y: ['v'], series: null};
const withResult = (items, idx, id, extra = {}) => { items[idx] = {...items[idx], results: [result(id, {chart: CHART, ...extra})]}; return items; };
const scenarios = [];
const scenario = (name, fn, opt = {}) => scenarios.push({name, fn, ...opt});
const frames2 = (s) => s.dom(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))));
const settle = async (s) => { await frames2(s); await sleep(40); await frames2(s); };
const geo = (s) => s.dom(() => window.__geo());
async function atBottom(s, what, timeout = 3000) {
  const end = Date.now() + timeout;
  for (;;) {
    const g = await geo(s);
    if (g.dist <= 2) return g;
    if (Date.now() > end) throw new Error('not at the bottom: ' + what + ' ' + JSON.stringify(g));
    await sleep(30);
  }
}
async function expectComponent(s) {
  assert.equal(await s.dom(() => window.__componentType), 'function', 'window.ChatMessages is a component');
}
// select(id) answered with `items` (cursor `next`).
async function load(s, id, items, next = null) {
  await s.call('select', id);
  const req = await s.take('messages', (e) => e.args.id === id && e.args.before == null);
  await s.reply(req.id, pageOf(items, next));
  await s.until((c) => !c.loading && c.conversationId === id && c.messages.length === items.length, id + ' loaded');
}
async function charts(s, n) {
  await s.waitFor((n) => document.querySelectorAll('[data-testid="bow-chat-messages"] canvas').length >= n, n, n + ' charts');
  await sleep(300);
  await settle(s);
}

// ── CM1: opens at the newest message ────────────────────────────────────────
scenario('CM1 a conversation opens at its newest message and stays there while charts settle; select(B) opens at B\'s newest', async (env) => {
  const s = await start(env, {code: msgApp({opts: {conversationId: 'A'}}), waitLoad: false});
  await expectComponent(s);
  const req = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(req.id, pageOf(withResult(withResult(history('n', 40), 37, 'r37'), 39, 'r39')));
  await s.until((c) => !c.loading && c.messages.length === 40, 'A loaded');
  let g = await atBottom(s, 'after the load');
  assert.equal(g.doc, false, 'the list scrolls itself in a bounded column');
  assert.ok(g.max > 1000, 'the transcript overflows');
  await charts(s, 2);
  await atBottom(s, 'after the charts settled');
  await load(s, 'B', withResult(history('b', 40), 39, 'rb39'));
  await atBottom(s, 'B after select');
  await charts(s, 1);
  await atBottom(s, 'B after its chart settled');
  await s.done();
});

scenario('CM1 a list loaded while hidden (display:none) is at the bottom once shown', async (env) => {
  const s = await start(env, {code: msgApp({opts: {conversationId: 'A'}, mode: 'hidden'}), waitLoad: false});
  await expectComponent(s);
  const req = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(req.id, pageOf(withResult(history('n', 40), 39, 'r39')));
  await s.until((c) => !c.loading && c.messages.length === 40, 'A loaded');
  await settle(s);
  await s.dom(() => window.__show());
  await atBottom(s, 'after it was shown');
  await charts(s, 1);
  await atBottom(s, 'after its chart settled');
  await s.done();
});

scenario('CM1 inside the owner\'s Tabs: the second tab, visited later and after a switch while hidden, is at the bottom', async (env) => {
  const s = await start(env, {code: msgApp({mode: 'tabs'})});
  await load(s, 'A', history('n', 40));
  await s.dom(() => window.__setTab('b'));
  await s.waitFor(() => window.__list(), null, 'the second tab');
  await expectComponent(s);
  await atBottom(s, 'the first visit');
  await s.dom(() => window.__setTab('a'));
  await settle(s);
  await load(s, 'B', withResult(history('b', 40), 39, 'rb39'));
  await settle(s);
  await s.dom(() => window.__setTab('b'));
  await atBottom(s, 'B shown in the tab');
  await charts(s, 1);
  await atBottom(s, 'B after its chart settled');
  await s.done();
});

// ── CM2: follow at the bottom, never move a reader ──────────────────────────
async function stream(s, check) {
  const req = await sendStarted(s, 'q', 'm100');
  await check('after started');
  return req;
}
async function streamRest(s, req, check) {
  for (let i = 0; i < 10; i++) {
    await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'word' + i + (i % 3 === 2 ? '\n\n' : ' ')});
    await s.until((c) => c.messages[c.messages.length - 1].text.includes('word' + i), 'delta ' + i);
    await settle(s);
    await check('delta ' + i);
  }
  await s.event(req.id, {type: 'result', ...result('r100', {chart: CHART})});
  await charts(s, 1);
  await check('the result chart');
  const answer = (await s.chat()).messages.at(-1).text;
  await finish(s, req, pageOf([...history('n', 40), u('uq', 'q'), a('m100', answer, {results: [result('r100', {chart: CHART})]})]));
  await settle(s);
  await check('completed and the re-read');
}

scenario('CM2 at the bottom: deltas, a chart and completed keep the list at the bottom', async (env) => {
  const s = await start(env, {code: msgApp()});
  await expectComponent(s);
  await load(s, 'A', history('n', 40));
  await atBottom(s, 'loaded');
  const check = (what) => atBottom(s, what);
  const req = await stream(s, check);
  await streamRest(s, req, check);
  assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-jump"]')), null, 'no jump button at the bottom');
  await s.done();
});

scenario('CM2 scrolled up by 300 px: the stream keeps the anchored row in place; Jump to latest shows, marked unseen, and reaches the bottom', async (env) => {
  const s = await start(env, {code: msgApp()});
  await expectComponent(s);
  await load(s, 'A', history('n', 40));
  await atBottom(s, 'loaded');
  const req = await stream(s, (what) => atBottom(s, what));
  const g = await geo(s);
  await s.dom((top) => window.__scrollTo(top), g.top - 300);
  await settle(s);
  const anchor = await s.dom(() => window.__firstVisible());
  assert.ok(anchor && anchor.id.startsWith('n'), 'an older row is at the top');
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-jump"]'), null, 'the jump button');
  const check = async (what) => {
    const off = await s.dom((id) => window.__offset(id), anchor.id);
    assert.ok(Math.abs(off - anchor.offset) <= 2, `${what}: the anchored row moved from ${anchor.offset} to ${off}`);
  };
  await streamRest(s, req, check);
  const jump = await s.dom(() => { const b = document.querySelector('[data-testid="bow-chat-jump"]'); return b && [b.getAttribute('data-unseen'), b.textContent, b.getAttribute('dir'), !!b.querySelector('svg')]; });
  assert.deepEqual(jump, ['true', 'Jump to latest', 'auto', true]);
  assert.ok((await geo(s)).dist > 200, 'still scrolled up');
  await s.frame.click('[data-testid="bow-chat-jump"]');
  await atBottom(s, 'after Jump to latest');
  await s.waitFor(() => !document.querySelector('[data-testid="bow-chat-jump"]'), null, 'the jump button gone');
  await s.done();
});

// ── CM3: older pages keep the view ──────────────────────────────────────────
scenario('CM3 Load earlier messages keeps the first visible row in place; the button goes at the end', async (env) => {
  const s = await start(env, {code: msgApp()});
  await expectComponent(s);
  await load(s, 'A', history('n', 40), 'k1');
  await atBottom(s, 'loaded');
  await s.dom(() => window.__scrollTo(0));
  await settle(s);
  const older = await s.dom(() => { const b = document.querySelector('[data-testid="bow-chat-older"]'); return b && [b.textContent, b.disabled, b.getAttribute('dir')]; });
  assert.deepEqual(older, ['Load earlier messages', false, 'auto']);
  const anchor = await s.dom(() => window.__firstVisible());
  assert.equal(anchor.id, 'n0');
  await s.frame.click('[data-testid="bow-chat-older"]');
  const req = await s.take('messages', (e) => e.args.before === 'k1');
  await s.waitFor(() => { const b = document.querySelector('[data-testid="bow-chat-older"]'); return b && b.disabled && b.querySelector('svg'); }, null, 'the button busy');
  await s.reply(req.id, pageOf(history('o', 10), null));
  await s.until((c) => c.messages.length === 50, 'the older page');
  await settle(s);
  const off = await s.dom((id) => window.__offset(id), 'n0');
  assert.ok(Math.abs(off - anchor.offset) <= 2, `n0 moved from ${anchor.offset} to ${off}`);
  assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-older"]')), null, 'no older pages left');
  assert.ok((await geo(s)).top > 200, 'the older rows are above the view');
  await s.done();
});

// ── CM4: the user's own send sticks ─────────────────────────────────────────
scenario('CM4 a send while scrolled up goes to the bottom', async (env) => {
  const s = await start(env, {code: msgApp()});
  await expectComponent(s);
  await load(s, 'A', history('n', 40));
  await atBottom(s, 'loaded');
  const g = await geo(s);
  await s.dom((top) => window.__scrollTo(top), g.top - 300);
  await settle(s);
  assert.ok((await geo(s)).dist > 250);
  const req = await sendStarted(s, 'q', 'm100');
  await atBottom(s, 'after the send');
  await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'growing answer'});
  await settle(s);
  await atBottom(s, 'following the answer');
  await finish(s, req, pageOf([...history('n', 40), u('uq', 'q'), a('m100', 'growing answer')]));
  await s.done();
});

// ── CM5: growth above the anchor ────────────────────────────────────────────
scenario('CM5 an earlier result growing above the view does not move the anchored row', async (env) => {
  const s = await start(env, {code: msgApp()});
  await expectComponent(s);
  const items = withResult(history('n', 40), 3, 'r3', {chart: null});
  await load(s, 'A', items);
  await atBottom(s, 'loaded');
  const g = await geo(s);
  await s.dom((top) => window.__scrollTo(top), Math.round(g.max / 2));
  await settle(s);
  const anchor = await s.dom(() => window.__firstVisible());
  const r3 = await s.dom(() => document.querySelector('[data-message-id="n3"]').getBoundingClientRect().bottom - window.__edge());
  assert.ok(r3 < 0, 'the growing result is above the view');
  const rows = Array.from({length: 8}, (_, i) => ({m: 'M' + i, v: i}));
  const grown = withResult(history('n', 40), 3, 'r3', {chart: null, rows, row_count: 8});
  await s.call('reload');
  const req = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(req.id, pageOf(grown));
  await s.waitFor(() => document.querySelectorAll('[data-message-id="n3"] tbody tr').length === 8, null, 'the grown table');
  await settle(s);
  const off = await s.dom((id) => window.__offset(id), anchor.id);
  assert.ok(Math.abs(off - anchor.offset) <= 2, `the anchored row moved from ${anchor.offset} to ${off}`);
  await s.done();
});

// ── CM6: no height: the document scrolls ────────────────────────────────────
scenario('CM6 without a bounded height the document scroller opens at the bottom and follows', async (env) => {
  const s = await start(env, {code: msgApp({opts: {conversationId: 'A'}, mode: 'unbounded'}), viewport: {width: 1000, height: 600}, waitLoad: false});
  await expectComponent(s);
  const req = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(req.id, pageOf(history('n', 40)));
  await s.until((c) => !c.loading && c.messages.length === 40, 'A loaded');
  const g = await atBottom(s, 'the document after the load');
  assert.equal(g.doc, true, 'the document is the scroller');
  assert.ok(g.max > 600);
  const send = await sendStarted(s, 'q', 'm100');
  for (let i = 0; i < 4; i++) {
    await s.event(send.id, {type: 'text_delta', block: 'b1', text: 'line ' + i + '\n\n'});
    await settle(s);
    await atBottom(s, 'the document follows delta ' + i);
  }
  await finish(s, send, pageOf([...history('n', 40), u('uq', 'q'), a('m100', 'done')]));
  await s.done();
});

// ── CM7: loading, empty, error ──────────────────────────────────────────────
scenario('CM7 loading shows a spinner, an empty conversation the empty slot, a failed load the error', async (env) => {
  let s = await start(env, {code: msgApp({opts: {conversationId: 'A'}, empty: "h('p', {id: 'empty-node'}, 'Ask anything')"}), waitLoad: false});
  await expectComponent(s);
  const req = await s.take('messages', (e) => e.args.id === 'A');
  assert.equal(await s.dom(() => !!document.querySelector('[data-testid="bow-chat-loading"] svg')), true, 'the loading spinner');
  assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-empty"]')), null, 'no empty slot while loading');
  await s.reply(req.id, pageOf([]));
  await s.until((c) => !c.loading, 'loaded');
  assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-loading"]')), null);
  assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-empty"] #empty-node')?.textContent), 'Ask anything');
  await s.call('select', 'X');
  const bad = await s.take('messages', (e) => e.args.id === 'X');
  await s.fail(bad.id, {code: 'NOT_FOUND', message: 'Conversation not found'});
  await s.until((c) => !c.loading && c.error, 'the error');
  const err = await s.dom(() => { const p = document.querySelector('[data-testid="bow-chat-error"]'); return p && [p.tagName, p.getAttribute('role'), p.getAttribute('dir'), p.textContent]; });
  assert.deepEqual(err, ['P', 'alert', 'auto', 'Conversation not found']);
  const list = await s.dom(() => { const l = window.__list(); return [l.getAttribute('role'), l.getAttribute('aria-busy'), l.style.overflowAnchor]; });
  assert.deepEqual(list, ['log', 'false', 'none']);
  await s.done();
});

// ── CM8: labels ─────────────────────────────────────────────────────────────
scenario('CM8 Hebrew labels on the buttons; an inline labels object does not re-render earlier charts', async (env) => {
  const labels = "{olderMessages: 'טען הודעות קודמות', jumpToLatest: 'לחדשות ביותר'}";
  const s = await start(env, {code: msgApp({labels})});
  await expectComponent(s);
  await load(s, 'A', withResult(history('n', 40), 37, 'r37'), 'k1');
  await charts(s, 1);
  const base = await s.dom(() => window.__charts.map((c) => [c.row, c.inits, c.sets]));
  assert.equal(base.length, 1);
  const req = await sendStarted(s, 'q', 'm100');
  await atBottom(s, 'after the send');
  for (let i = 0; i < 5; i++) {
    await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'w' + i + ' '});
    await s.until((c) => c.messages.at(-1).text.includes('w' + i), 'delta ' + i);
  }
  await settle(s);
  assert.deepEqual(await s.dom(() => window.__charts.map((c) => [c.row, c.inits, c.sets])), base, 'no new setOption through 5 deltas with inline labels');
  await s.dom(() => window.__scrollTo(0));
  await settle(s);
  const texts = await s.dom(() => ['bow-chat-older', 'bow-chat-jump'].map((t) => { const b = document.querySelector(`[data-testid="${t}"]`); return b && [b.textContent, b.getAttribute('dir'), getComputedStyle(b).direction]; }));
  assert.deepEqual(texts, [['טען הודעות קודמות', 'auto', 'rtl'], ['לחדשות ביותר', 'auto', 'rtl']]);
  await finish(s, req, pageOf([...withResult(history('n', 40), 37, 'r37'), u('uq', 'q'), a('m100', 'w0 w1 w2 w3 w4')]));
  await s.done();
});

// ── CM9: an app's own ChatMessage does not hijack the list ──────────────────
scenario('CM9 an app that declares function ChatMessage still gets the built-in rows', async (env) => {
  const s = await start(env, {code: msgApp({pre: '<script>function ChatMessage() { return null; }</script>'})});
  await expectComponent(s);
  assert.equal(await s.dom(() => typeof window.ChatMessage === 'function' && window.ChatMessage.length === 0), true, 'the app replaced window.ChatMessage');
  await load(s, 'A', history('n', 4));
  assert.equal(await s.dom(() => document.querySelectorAll('[data-testid="bow-chat-messages"] [data-testid="bow-chat-row"] [data-testid="bow-chat-message"]').length), 4);
  await s.done();
});

// ── CM10: fixture mode ──────────────────────────────────────────────────────
scenario('CM10 fixture mode: one send through ChatMessages renders two messages and two results', async (env) => {
  const s = await start(env, {code: msgApp(), fixtureMode: true});
  await expectComponent(s);
  await s.until((c) => !c.loading, 'the initial load');
  await s.call('send', 'Revenue per month?');
  await s.until((c) => !c.sending && c.messages.length === 2 && !String(c.messages[0].id).startsWith('local-'), 'the fixture answer');
  await s.waitFor(() => document.querySelectorAll('[data-testid="bow-chat-messages"] [data-testid="bow-chat-result"]').length === 2, null, 'two results');
  assert.equal(await s.dom(() => document.querySelectorAll('[data-testid="bow-chat-row"]').length), 2);
  await s.done();
});

// ── review fixes ────────────────────────────────────────────────────────────
scenario('CM6b a short fixed-height list above tall page content never scrolls the page', async (env) => {
  const s = await start(env, {code: msgApp({opts: {conversationId: 'A'}, mode: 'card'}), viewport: {width: 1000, height: 600}, waitLoad: false});
  await expectComponent(s);
  const doc = () => s.dom(() => document.scrollingElement.scrollTop);
  assert.ok(await s.dom(() => document.scrollingElement.scrollHeight > 2000), 'the page is taller than the frame');
  const req = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(req.id, pageOf(history('n', 2)));
  await s.until((c) => !c.loading && c.messages.length === 2, 'A loaded');
  await settle(s);
  assert.equal(await doc(), 0, 'the page stays at the top after the load');
  const send = await sendStarted(s, 'q', 'm1');
  await s.event(send.id, {type: 'text_delta', block: 'b1', text: 'a short answer'});
  await settle(s);
  assert.equal(await doc(), 0, 'the page stays at the top through a send');
  await finish(s, send, pageOf([...history('n', 2), u('uq', 'q'), a('m1', 'a short answer')]));
  await settle(s);
  assert.equal(await doc(), 0, 'and after the re-read');
  await s.done();
});

scenario('CM6c a short fixed-height list below tall page content never scrolls the page down to it', async (env) => {
  const s = await start(env, {code: msgApp({opts: {conversationId: 'A'}, mode: 'cardBelow'}), viewport: {width: 1000, height: 600}, waitLoad: false});
  await expectComponent(s);
  const doc = () => s.dom(() => document.scrollingElement.scrollTop);
  const req = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(req.id, pageOf(history('n', 2)));
  await s.until((c) => !c.loading && c.messages.length === 2, 'A loaded');
  await settle(s);
  assert.equal(await doc(), 0, 'the page stays at the top after the load');
  const send = await sendStarted(s, 'q', 'm1');
  await s.event(send.id, {type: 'text_delta', block: 'b1', text: 'a short answer'});
  await settle(s);
  assert.equal(await doc(), 0, 'the page stays at the top through a send');
  await finish(s, send, pageOf([...history('n', 2), u('uq', 'q'), a('m1', 'a short answer')]));
  await s.done();
});

scenario('CM6d without a height, the document follows the transcript\'s end, not the page content after it', async (env) => {
  const s = await start(env, {code: msgApp({opts: {conversationId: 'A'}, mode: 'footer'}), viewport: {width: 1000, height: 600}, waitLoad: false});
  await expectComponent(s);
  const req = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(req.id, pageOf(history('n', 40)));
  await s.until((c) => !c.loading && c.messages.length === 40, 'A loaded');
  await settle(s);
  const end = () => s.dom(() => document.querySelector('[data-testid="bow-chat-messages"] > div').getBoundingClientRect().bottom - document.scrollingElement.clientHeight);
  assert.ok(Math.abs(await end()) <= 2, 'the transcript\'s end is at the bottom of the view: ' + await end());
  assert.ok((await s.dom(() => document.scrollingElement.scrollHeight - document.scrollingElement.scrollTop - document.scrollingElement.clientHeight)) > 1000, 'the footer stays below');
  const send = await sendStarted(s, 'q', 'm1');
  await s.event(send.id, {type: 'text_delta', block: 'b1', text: 'line one\n\nline two\n\nline three'});
  await settle(s);
  assert.ok(Math.abs(await end()) <= 2, 'it follows the answer');
  await finish(s, send, pageOf([...history('n', 40), u('uq', 'q'), a('m1', 'line one')]));
  await s.done();
});

scenario('CM2c a user scroll up during the Jump to latest scroll is kept: the next delta does not snap back', async (env) => {
  const s = await start(env, {code: msgApp()});
  await expectComponent(s);
  await load(s, 'A', history('n', 40));
  await atBottom(s, 'loaded');
  const req = await stream(s, (what) => atBottom(s, what));
  const g = await geo(s);
  await s.dom((top) => window.__scrollTo(top), g.top - 600);
  await settle(s);
  await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'first '});
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-jump"]'), null, 'the jump button');
  await s.frame.click('[data-testid="bow-chat-jump"]');
  await frames2(s);
  await s.dom(() => window.__scrollTo(100));
  await settle(s);
  await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'second'});
  await s.until((c) => c.messages.at(-1).text.includes('second'), 'the delta');
  await settle(s);
  assert.ok(Math.abs((await geo(s)).top - 100) <= 2, 'the reader stays where they scrolled');
  await s.done();
});

scenario('CM7b a failed older page shows its error by the Load earlier button, not below the last message', async (env) => {
  const s = await start(env, {code: msgApp()});
  await expectComponent(s);
  await load(s, 'A', history('n', 40), 'k1');
  await s.dom(() => window.__scrollTo(0));
  await settle(s);
  await s.frame.click('[data-testid="bow-chat-older"]');
  let req = await s.take('messages', (e) => e.args.before === 'k1');
  await s.fail(req.id, {code: 'RATE_LIMITED', message: 'Too many reads'});
  await s.until((c) => c.error && !c.loadingOlder, 'the error');
  await settle(s);
  const seen = await s.dom(() => {
    const p = document.querySelector('[data-testid="bow-chat-older-error"]');
    if (!p) return null;
    const r = p.getBoundingClientRect(), box = window.__list().getBoundingClientRect();
    return [p.getAttribute('role'), p.getAttribute('dir'), p.textContent, r.top >= box.top && r.bottom <= box.bottom, !!document.querySelector('[data-testid="bow-chat-error"]')];
  });
  assert.deepEqual(seen, ['alert', 'auto', 'Too many reads', true, false], 'in view, next to the button, once');
  await s.frame.click('[data-testid="bow-chat-older"]');
  req = await s.take('messages', (e) => e.args.before === 'k1');
  await s.reply(req.id, pageOf(history('o', 4), null));
  await s.until((c) => c.messages.length === 44 && !c.error, 'the retry');
  await settle(s);
  assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-older-error"]')), null, 'gone after the retry');
  await s.done();
});

await run('artifactChatMessages', scenarios);

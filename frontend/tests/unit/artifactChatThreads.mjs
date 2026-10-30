// Real Chromium: <ChatThreads chat={chat}/> (artifact-globals.js), the
// viewer's conversations: select, new, rename inline, archive confirmed in the
// owner's Modal (focus moves to the next thread), a spinner while an answer
// runs, and a Drawer below 640 px. Stub `chat` objects with spies for the
// component, the fake live host and the fixture mode for the integration.
//
//   cd frontend && PW_CHROMIUM_PATH=/path/to/chrome node tests/unit/artifactChatThreads.mjs
import assert from 'node:assert/strict';
import {start, run, pageOf, sendStarted, sleep} from './support/chatHarness.mjs';

const conv = (id, extra = {}) => ({id, title: 'T' + id, created_at: '2026-10-30T10:00:00Z', updated_at: '2026-10-30T10:00:00Z', in_progress: false, ...extra});
const CONVS = [conv('c1'), conv('c2'), conv('c3')];

// A stub chat: select/newConversation/rename/archive record their calls in
// window.__calls; archive also removes the item (as the hook does);
// window.__setChat(patch) merges into the stub's state.
const stubApp = ({props = '{}', wrapDir = null, htmlDir = null, pre = '', convs = CONVS, active = 'c1'} = {}) => `${pre}<script>
(function () {
  var h = React.createElement;
  ${htmlDir ? `document.documentElement.dir = ${JSON.stringify(htmlDir)};` : ''}
  window.__componentType = typeof window.ChatThreads;
  window.__calls = [];
  function App() {
    var s = React.useState({conversations: ${JSON.stringify(convs)}, conversationId: ${JSON.stringify(active)}, loading: false});
    window.__setChat = function (p) { s[1](function (prev) { return Object.assign({}, prev, p); }); };
    var chat = Object.assign({messages: [], policy: {available: true}, sending: false, answering: false, error: null}, s[0], {
      select: function (id) { window.__calls.push(['select', id]); },
      newConversation: function () { window.__calls.push(['newConversation']); },
      rename: function (id, t) { window.__calls.push(['rename', id, t]); },
      archive: function (id) {
        window.__calls.push(['archive', id]);
        s[1](function (prev) { return Object.assign({}, prev, {conversations: prev.conversations.filter(function (c) { return c.id !== id; })}); });
      }
    });
    if (typeof window.ChatThreads !== 'function') return h('p', {id: 'missing'}, 'ChatThreads is not defined');
    return h('div', {dir: ${JSON.stringify(wrapDir)} || undefined, style: {display: 'flex', height: '500px'}},
      [h(window.ChatThreads, Object.assign({key: 't', chat: chat}, ${props})), h('div', {key: 'm', id: 'main', tabIndex: -1, style: {flex: 1}}, 'main')]);
  }
  ReactDOM.createRoot(document.getElementById('root')).render(h(App));
})();
</script>`;

// The hook with ChatThreads (inline) and ChatMessages.
const liveApp = (mode = 'list') => `<script>
(function () {
  var h = React.createElement;
  window.__componentType = typeof window.ChatThreads;
  function App() {
    var chat = useConversation();
    window.__chat = chat;
    if (typeof window.ChatThreads !== 'function') return h('p', {id: 'missing'}, 'ChatThreads is not defined');
    return h('div', {style: {display: 'flex', height: '500px'}}, [
      h(ChatThreads, {key: 't', chat: chat, mode: ${JSON.stringify(mode)}}),
      h('div', {key: 'c', style: {display: 'flex', flexDirection: 'column', flex: 1, minWidth: 0}}, h(ChatMessages, {chat: chat}))]);
  }
  ReactDOM.createRoot(document.getElementById('root')).render(h(App));
})();
</script>`;

const scenarios = [];
const scenario = (name, fn, opt = {}) => scenarios.push({name, fn, ...opt});
const calls = (s) => s.dom(() => window.__calls.slice());
const item = (id, testid) => `[data-testid="bow-chat-thread"][data-id="${id}"] [data-testid="${testid}"]`;
const INPUT = '[data-testid="bow-chat-thread-input"]';
const DIALOG = '[role="dialog"]';
const active = (s) => s.dom(() => { const el = document.activeElement; const li = el && el.closest('[data-testid="bow-chat-thread"]'); return [el && el.getAttribute('data-testid'), li && li.getAttribute('data-id')]; });
async function stub(env, opts = {}) {
  const s = await start(env, {code: stubApp(opts), waitLoad: false, viewport: opts.viewport || {width: 1000, height: 700}});
  assert.equal(await s.dom(() => window.__componentType), 'function', 'window.ChatThreads is a component');
  return s;
}
const gone = (s, sel, what) => s.waitFor((sel) => !document.querySelector(sel), sel, what);

scenario('TH1 the list: titles dir="auto", the open one aria-current; a click selects (not the open one); New starts over', async (env) => {
  const s = await stub(env);
  await s.waitFor(() => document.querySelectorAll('[data-testid="bow-chat-thread"]').length === 3, null, 'three threads');
  const rows = await s.dom(() => Array.from(document.querySelectorAll('[data-testid="bow-chat-thread"]')).map((li) => {
    const b = li.querySelector('[data-testid="bow-chat-thread-select"]');
    return [li.getAttribute('data-id'), li.getAttribute('data-active'), b.getAttribute('aria-current'), b.querySelector('span[dir="auto"]').textContent];
  }));
  assert.deepEqual(rows, [['c1', 'true', 'true', 'Tc1'], ['c2', 'false', null, 'Tc2'], ['c3', 'false', null, 'Tc3']]);
  assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-threads"]').tagName), 'UL');
  await s.frame.click(item('c2', 'bow-chat-thread-select'));
  await s.frame.click(item('c1', 'bow-chat-thread-select'));
  await s.frame.click('[data-testid="bow-chat-new"]');
  assert.deepEqual(await calls(s), [['select', 'c2'], ['newConversation']]);
  assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-new"]').textContent), 'New chat');
  await s.done();
});

scenario('TH2 rename inline: Enter and blur commit; Escape, blank and unchanged send nothing', async (env) => {
  const s = await stub(env);
  await s.frame.click(item('c2', 'bow-chat-thread-rename'));
  const input = await s.dom((sel) => { const i = document.querySelector(sel); return i && [i.value, i.getAttribute('dir'), document.activeElement === i, i.maxLength]; }, INPUT);
  assert.deepEqual(input, ['Tc2', 'auto', true, 120], 'prefilled, focused, dir="auto", 120 characters');
  await s.frame.fill(INPUT, 'x');
  await s.frame.press(INPUT, 'Enter');
  await gone(s, INPUT, 'edit mode left');
  await s.frame.click(item('c2', 'bow-chat-thread-rename'));
  await s.frame.fill(INPUT, 'y');
  await s.dom((sel) => document.querySelector(sel).blur(), INPUT);
  await gone(s, INPUT, 'edit mode left on blur');
  assert.deepEqual(await calls(s), [['rename', 'c2', 'x'], ['rename', 'c2', 'y']]);
  await s.frame.click(item('c3', 'bow-chat-thread-rename'));
  await s.frame.fill(INPUT, 'never');
  await s.frame.press(INPUT, 'Escape');
  await gone(s, INPUT, 'Escape leaves');
  for (const v of ['   ', 'Tc3']) {
    await s.frame.click(item('c3', 'bow-chat-thread-rename'));
    await s.frame.fill(INPUT, v);
    await s.frame.press(INPUT, 'Enter');
    await gone(s, INPUT, 'edit mode left');
  }
  assert.equal((await calls(s)).length, 2, 'Escape, a blank and an unchanged title send nothing');
  await s.done();
});

scenario('TH2b the rename input keeps at most 120 characters', async (env) => {
  const s = await stub(env);
  await s.frame.click(item('c2', 'bow-chat-thread-rename'));
  await s.frame.press(INPUT, 'Control+A');
  await s.frame.type(INPUT, 'a'.repeat(150));
  assert.equal(await s.dom((sel) => document.querySelector(sel).value.length, INPUT), 120);
  await s.done();
});

async function openConfirm(s, id) {
  await s.frame.click(item(id, 'bow-chat-thread-archive'));
  await s.waitFor((sel) => document.querySelector(sel), DIALOG, 'the confirmation');
}
scenario('TH3 archive asks in a Modal; Cancel returns the focus; Confirm archives and focuses the next, else the previous, else New', async (env) => {
  const s = await stub(env);
  await openConfirm(s, 'c2');
  assert.deepEqual(await s.dom((sel) => { const d = document.querySelector(sel); return [d.querySelector('h2').textContent, d.querySelector('[data-testid="bow-chat-archive-cancel"]').textContent, d.querySelector('[data-testid="bow-chat-archive-confirm"]').textContent]; }, DIALOG),
    ['Archive this conversation?', 'Cancel', 'Archive']);
  await s.frame.click('[data-testid="bow-chat-archive-cancel"]');
  await gone(s, DIALOG, 'the Modal closed');
  assert.deepEqual(await calls(s), [], 'Cancel archives nothing');
  assert.deepEqual(await active(s), ['bow-chat-thread-archive', 'c2'], 'the focus is back on the archive button');
  await openConfirm(s, 'c2');
  await s.frame.click('[data-testid="bow-chat-archive-confirm"]');
  await gone(s, DIALOG, 'the Modal closed');
  assert.deepEqual(await calls(s), [['archive', 'c2']]);
  assert.deepEqual(await active(s), ['bow-chat-thread-select', 'c3'], 'the next thread');
  await openConfirm(s, 'c3');
  await s.frame.click('[data-testid="bow-chat-archive-confirm"]');
  await gone(s, DIALOG, 'the Modal closed');
  assert.deepEqual(await active(s), ['bow-chat-thread-select', 'c1'], 'the last one: the previous thread');
  await openConfirm(s, 'c1');
  await s.frame.click('[data-testid="bow-chat-archive-confirm"]');
  await gone(s, DIALOG, 'the Modal closed');
  assert.deepEqual(await active(s), ['bow-chat-new', null], 'the only one: New');
  await s.done();
});

scenario('TH4 a conversation whose answer runs shows a spinner labelled in progress', async (env) => {
  const s = await stub(env, {convs: [conv('c1'), conv('c2', {in_progress: true})]});
  const busy = await s.dom(() => Array.from(document.querySelectorAll('[data-testid="bow-chat-thread"]')).map((li) => {
    const b = li.querySelector('[data-testid="bow-chat-thread-busy"]');
    return [li.getAttribute('data-id'), li.getAttribute('data-in-progress'), b && b.getAttribute('role'), b && b.getAttribute('aria-label'), !!(b && b.querySelector('svg'))];
  }));
  assert.deepEqual(busy, [['c1', 'false', null, null, false], ['c2', 'true', 'status', 'Answer in progress...', true]]);
  await s.done();
});

const panelSide = (s) => s.dom((sel) => { const p = document.querySelector(sel); const r = p.getBoundingClientRect(); return {left: Math.round(r.left), right: Math.round(window.innerWidth - r.right)}; }, DIALOG);
scenario('TH5 narrow: a toggle opens a Drawer at the start side; selecting closes it; Escape in the rename input keeps it; wide: inline', async (env) => {
  let s = await stub(env, {viewport: {width: 500, height: 700}});
  assert.deepEqual(await s.dom(() => [!!document.querySelector('[data-testid="bow-chat-threads-toggle"]'), !!document.querySelector('[data-testid="bow-chat-threads"]')]), [true, false], 'only the toggle');
  await s.frame.click('[data-testid="bow-chat-threads-toggle"]');
  await s.waitFor((sel) => document.querySelector(sel + ' [data-testid="bow-chat-threads"]'), DIALOG, 'the Drawer');
  await sleep(350);
  assert.equal((await panelSide(s)).left, 0, 'at the left edge in an LTR frame');
  await s.frame.click(item('c2', 'bow-chat-thread-rename'));
  await s.frame.press(INPUT, 'Escape');
  await gone(s, INPUT, 'edit mode left');
  await sleep(250);
  assert.equal(await s.dom((sel) => !!document.querySelector(sel), DIALOG), true, 'Escape in the input keeps the Drawer open');
  await s.frame.click(item('c2', 'bow-chat-thread-select'));
  await gone(s, DIALOG, 'the Drawer closed');
  assert.deepEqual(await calls(s), [['select', 'c2']]);
  await s.done();

  s = await stub(env, {viewport: {width: 500, height: 700}, htmlDir: 'rtl'});
  await s.frame.click('[data-testid="bow-chat-threads-toggle"]');
  await s.waitFor((sel) => document.querySelector(sel), DIALOG, 'the Drawer');
  await sleep(350);
  assert.equal((await panelSide(s)).right, 0, 'at the right edge in an RTL frame');
  await s.frame.click('[data-testid="bow-chat-new"]');
  await gone(s, DIALOG, 'New closes it');
  await s.done();

  s = await stub(env);
  assert.deepEqual(await s.dom(() => [!!document.querySelector('[data-testid="bow-chat-threads-toggle"]'), !!document.querySelector('[data-testid="bow-chat-threads"]')]), [false, true], '1000 px: inline');
  await s.done();
  s = await stub(env, {props: "{mode: 'drawer'}"});
  assert.equal(await s.dom(() => !!document.querySelector('[data-testid="bow-chat-threads-toggle"]')), true, 'mode="drawer" at 1000 px');
  await s.done();
});

scenario('TH6 RTL: the actions sit at the inline end; no physical direction classes', async (env) => {
  const s = await stub(env, {wrapDir: 'rtl'});
  const geo = await s.dom(() => {
    const li = document.querySelector('[data-testid="bow-chat-thread"][data-id="c1"]');
    const sel = li.querySelector('[data-testid="bow-chat-thread-select"]').getBoundingClientRect();
    const act = li.querySelector('[data-testid="bow-chat-thread-archive"]').getBoundingClientRect();
    return act.right <= sel.left + 1;
  });
  assert.equal(geo, true, 'the actions are left of the title in RTL');
  const physical = await s.dom(() => {
    const root = document.querySelector('[data-testid="bow-chat-threads"]').closest('[data-testid="bow-chat-threads-root"]');
    return Array.from(root.querySelectorAll('*')).concat([root]).map((el) => el.getAttribute('class') || '').filter((c) => /(^|\s|:)-?(ml|mr|pl|pr|left|right)-/.test(c));
  });
  assert.deepEqual(physical, []);
  await s.done();
});

scenario('TH7 an app\'s own Drawer and Modal do not change ChatThreads', async (env) => {
  const pre = '<script>function Drawer() { return null; } function Modal() { return null; }</script>';
  let s = await stub(env, {pre});
  assert.equal(await s.dom(() => window.Modal() === null && window.Drawer() === null), true, 'the app replaced both');
  await openConfirm(s, 'c2');
  await s.frame.click('[data-testid="bow-chat-archive-confirm"]');
  await gone(s, DIALOG, 'the Modal closed');
  assert.deepEqual(await calls(s), [['archive', 'c2']]);
  await s.done();
  s = await stub(env, {pre, viewport: {width: 500, height: 700}});
  await s.frame.click('[data-testid="bow-chat-threads-toggle"]');
  await s.waitFor((sel) => document.querySelector(sel + ' [data-testid="bow-chat-threads"]'), DIALOG, 'the built-in Drawer');
  await s.done();
});

scenario('TH8 with useConversation: rename and archive through the UI; a thread left while streaming spins until a list poll says it is done', async (env) => {
  const LIST = {items: [conv('c3'), conv('c2'), conv('c1')], next_before: null};
  const s = await start(env, {code: liveApp(), auto: {list: LIST}, viewport: {width: 1000, height: 700}});
  assert.equal(await s.dom(() => window.__componentType), 'function', 'window.ChatThreads is a component');
  await s.frame.click(item('c2', 'bow-chat-thread-rename'));
  await s.frame.fill(INPUT, '  New title ');
  await s.frame.press(INPUT, 'Enter');
  const ren = await s.take('rename');
  assert.deepEqual([ren.args.id, ren.args.title], ['c2', 'New title']);
  await s.reply(ren.id, {id: 'c2', title: 'New title'});
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-thread"][data-id="c2"]').textContent.includes('New title'), null, 'the new title');
  await openConfirm(s, 'c2');
  await s.frame.click('[data-testid="bow-chat-archive-confirm"]');
  const ar = await s.take('archive');
  assert.equal(ar.args.id, 'c2');
  await s.reply(ar.id, null);
  await gone(s, '[data-testid="bow-chat-thread"][data-id="c2"]', 'c2 gone');

  await s.page.evaluate(() => { window.__host.auto.list = 'hold'; });
  await s.frame.click(item('c1', 'bow-chat-thread-select'));
  const load = await s.take('messages', (e) => e.args.id === 'c1');
  await s.reply(load.id, pageOf([]));
  await s.until((c) => !c.loading && c.conversationId === 'c1', 'c1 open');
  const send = await sendStarted(s, 'q', 'm1');
  await s.event(send.id, {type: 'text_delta', block: 'b1', text: 'part'});
  await s.until((c) => c.messages[1] && c.messages[1].text === 'part', 'streaming');
  await s.frame.click(item('c3', 'bow-chat-thread-select'));
  const l3 = await s.take('messages', (e) => e.args.id === 'c3');
  await s.reply(l3.id, pageOf([]));
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-thread"][data-id="c1"] [data-testid="bow-chat-thread-busy"]'), null, 'c1 spins');
  const seq = (await s.log()).length - 1;
  const poll = await s.take('list', (e) => e.seq > seq, 'the list poll');
  await s.reply(poll.id, {items: [conv('c3'), conv('c1')], next_before: null});
  await gone(s, '[data-testid="bow-chat-thread"][data-id="c1"] [data-testid="bow-chat-thread-busy"]', 'the spinner off');
  await s.done();
}, {slow: true});

scenario('TH9 fixture mode: rename and archive through the UI', async (env) => {
  const s = await start(env, {code: liveApp(), fixtureMode: true, viewport: {width: 1000, height: 700}});
  await s.until((c) => !c.loading, 'loaded');
  await s.call('send', 'Revenue?');
  const c = await s.until((c) => !c.sending && c.conversationId && c.messages.length === 2, 'the fixture answer');
  const id = c.conversationId;
  await s.frame.click(item(id, 'bow-chat-thread-rename'));
  await s.frame.fill(INPUT, 'Renamed');
  await s.frame.press(INPUT, 'Enter');
  await s.until((c) => c.conversations.some((x) => x.id === id && x.title === 'Renamed'), 'renamed');
  await openConfirm(s, id);
  await s.frame.click('[data-testid="bow-chat-archive-confirm"]');
  await s.until((c) => c.conversationId === null && !c.conversations.some((x) => x.id === id), 'archived');
  await s.done();
});

scenario('TH10 Hebrew labels on New, the toggle and the confirmation', async (env) => {
  const labels = "{labels: {conversations: 'שיחות', newConversation: 'שיחה חדשה', rename: 'שינוי שם', archive: 'העברה לארכיון', archiveConfirm: 'להעביר לארכיון?', cancel: 'ביטול'}}";
  let s = await stub(env, {props: labels});
  assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-new"]').textContent), 'שיחה חדשה');
  assert.deepEqual(await s.dom(() => { const li = document.querySelector('[data-testid="bow-chat-thread"]'); return [li.querySelector('[data-testid="bow-chat-thread-rename"]').getAttribute('aria-label'), li.querySelector('[data-testid="bow-chat-thread-archive"]').getAttribute('aria-label')]; }), ['שינוי שם', 'העברה לארכיון']);
  await openConfirm(s, 'c2');
  assert.deepEqual(await s.dom((sel) => { const d = document.querySelector(sel); return [d.querySelector('h2').textContent, d.querySelector('[data-testid="bow-chat-archive-cancel"]').textContent, d.querySelector('[data-testid="bow-chat-archive-confirm"]').textContent]; }, DIALOG), ['להעביר לארכיון?', 'ביטול', 'העברה לארכיון']);
  await s.done();
  s = await stub(env, {props: labels, viewport: {width: 500, height: 700}});
  assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-threads-toggle"]').textContent), 'שיחות');
  await s.done();
});

const messagesOnlyApp = `<script>
(function () {
  var h = React.createElement;
  function App() {
    var chat = useConversation();
    window.__chat = chat;
    return h('div', {style: {height: '500px', display: 'flex', flexDirection: 'column'}}, h(ChatMessages, {chat: chat}));
  }
  ReactDOM.createRoot(document.getElementById('root')).render(h(App));
})();
</script>`;
const LIST3 = {items: [conv('c3'), conv('c2'), conv('c1')], next_before: null};
const threadsAlert = (s) => s.dom((sel) => {
  const p = document.querySelector('[data-testid="bow-chat-threads-error"]');
  if (!p) return null;
  const panel = document.querySelector(sel);
  const r = p.getBoundingClientRect();
  const inPanel = !!panel && panel.contains(p);
  const visible = r.width > 0 && r.height > 0 && r.top >= 0 && r.bottom <= window.innerHeight;
  return [p.getAttribute('role'), p.getAttribute('dir'), p.textContent, inPanel, visible, !!document.querySelector('[data-testid="bow-chat-error"]')];
}, DIALOG);

scenario('TH11 in a Drawer, failed rename and archive show an alert inside the Drawer, not in ChatMessages', async (env) => {
  const s = await start(env, {code: liveApp('drawer'), auto: {list: LIST3}, viewport: {width: 1000, height: 700}});
  assert.equal(await s.dom(() => window.__componentType), 'function', 'window.ChatThreads is a component');
  await s.frame.click('[data-testid="bow-chat-threads-toggle"]');
  await s.waitFor((sel) => document.querySelector(sel + ' [data-testid="bow-chat-threads"]'), DIALOG, 'the Drawer');
  await s.frame.click(item('c2', 'bow-chat-thread-rename'));
  await s.frame.fill(INPUT, 'New');
  await s.frame.press(INPUT, 'Enter');
  const ren = await s.take('rename');
  await s.fail(ren.id, {code: 'FORBIDDEN', message: 'You cannot rename this'});
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-threads-error"]'), null, 'the alert');
  assert.deepEqual(await threadsAlert(s), ['alert', 'auto', 'You cannot rename this', true, true, false], 'in the Drawer, visible, once');
  await openConfirm(s, 'c3');
  await s.frame.click('[data-testid="bow-chat-archive-confirm"]');
  const ar = await s.take('archive');
  await s.fail(ar.id, {code: 'UNAVAILABLE', message: 'Archive failed'});
  await s.waitFor(() => (document.querySelector('[data-testid="bow-chat-threads-error"]') || {}).textContent === 'Archive failed', null, 'the archive alert');
  assert.deepEqual(await threadsAlert(s), ['alert', 'auto', 'Archive failed', true, true, false]);
  await s.done();
});

scenario('TH12 without ChatThreads, a failed rename still shows in ChatMessages', async (env) => {
  const s = await start(env, {code: messagesOnlyApp, auto: {list: LIST3}, viewport: {width: 1000, height: 700}});
  await s.call('rename', 'c2', 'New');
  const ren = await s.take('rename');
  await s.fail(ren.id, {code: 'FORBIDDEN', message: 'You cannot rename this'});
  await s.waitFor(() => (document.querySelector('[data-testid="bow-chat-error"]') || {}).textContent === 'You cannot rename this', null, 'the error in ChatMessages');
  await s.done();
});

await run('artifactChatThreads', scenarios);

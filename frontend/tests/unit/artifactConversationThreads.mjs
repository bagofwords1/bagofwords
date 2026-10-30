// Real Chromium: useConversation's conversation list rules against the fake
// live host: rename(id, title) and archive(id), both optimistic and reverted
// on failure; the open conversation's in_progress flag (derived), the flag of
// a conversation left while its answer runs (set by the single 'switch'), the
// bounded list poll while another conversation is in progress, and reload()
// with the 'listed' rules.
//
//   cd frontend && PW_CHROMIUM_PATH=/path/to/chrome node tests/unit/artifactConversationThreads.mjs
import assert from 'node:assert/strict';
import {start, run, u, a, pageOf, sendStarted, finish, sleep} from './support/chatHarness.mjs';

// The hook's object as window.__chat; every render appends the conversations'
// flags ("id:true,id:false") to window.__flags.
const threadsApp = (opts = null) => `<script>
(function () {
  var h = React.createElement;
  window.__flags = []; window.__msgsVersion = 0;
  var last = null;
  function Chat(props) {
    var chat = useConversation(props.opts);
    window.__chat = chat;
    if (chat.messages !== last) { last = chat.messages; window.__msgsVersion++; }
    window.__flags.push(chat.conversations.map(function (c) { return c.id + ':' + c.in_progress; }).join(','));
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

const conv = (id, extra = {}) => ({id, title: 'T' + id, created_at: '2026-10-30T10:00:00Z', updated_at: '2026-10-30T10:00:00Z', in_progress: false, ...extra});
const LIST = {items: [conv('c3'), conv('c2'), conv('c1')], next_before: null};
const scenarios = [];
const scenario = (name, fn, opt = {}) => scenarios.push({name, fn, ...opt});
const titleOf = (c, id) => (c.conversations.find((x) => x.id === id) || {}).title;
const flagOf = (c, id) => (c.conversations.find((x) => x.id === id) || {}).in_progress;
const convIds = (c) => c.conversations.map((x) => x.id);
const setAuto = (s, op, value) => s.page.evaluate(([op, value]) => { window.__host.auto[op] = value; }, [op, value]);
const mark = (s) => s.dom(() => window.__flags.length);
const lastSeq = async (s) => (await s.log()).length - 1;
// A list request issued after log entry `after` (the initial one is already answered).
const newList = async (s, after) => s.take('list', (e) => e.seq > after, 'a new list read');
const flagsSince = (s, from, id) => s.dom(([from, id]) => window.__flags.slice(from).map((f) => {
  const hit = f.split(',').find((x) => x.startsWith(id + ':'));
  return hit ? hit.split(':')[1] : 'absent';
}), [from, id]);
async function threads(env, opts = {}) {
  const s = await start(env, {code: threadsApp(opts.hook || null), auto: {list: opts.list || LIST, ...(opts.auto || {})}});
  assert.equal(await s.dom(() => typeof window.__chat.rename), 'function', 'useConversation returns rename()');
  return s;
}
async function open(s, id, items = []) {
  await s.call('select', id);
  const load = await s.take('messages', (e) => e.args.id === id);
  await s.reply(load.id, pageOf(items));
  return s.until((c) => !c.loading && c.conversationId === id, id + ' loaded');
}

// ── T1-T3 rename ────────────────────────────────────────────────────────────
scenario('T1 rename retitles at once with the trimmed title, applies the server\'s answer; blank is a no-op; 250 characters are cut to 200', async (env) => {
  const s = await threads(env);
  await s.call('rename', 'c1', '  New  ');
  let c = await s.chat();
  assert.equal(titleOf(c, 'c1'), 'New', 'optimistic');
  const req = await s.take('rename');
  assert.deepEqual([req.args.id, req.args.title], ['c1', 'New']);
  await s.reply(req.id, {id: 'c1', title: 'New', created_at: '2026-10-30T10:00:00Z', updated_at: '2026-10-30T11:00:00Z', agents: []});
  c = await s.until((c) => (c.conversations.find((x) => x.id === 'c1') || {}).updated_at === '2026-10-30T11:00:00Z', 'the server answer');
  assert.equal(titleOf(c, 'c1'), 'New');
  await s.call('rename', 'c1', '   ');
  await sleep(120);
  assert.equal(await s.count('rename'), 1, 'a blank title sends nothing');
  await s.call('rename', 'c2', 'x'.repeat(250));
  const long = await s.take('rename');
  assert.equal(long.args.title.length, 200, 'the route\'s limit');
  await s.reply(long.id, {id: 'c2', title: 'x'.repeat(120)});
  await s.until((c) => titleOf(c, 'c2') === 'x'.repeat(120), 'the server\'s (cut) title');
  await s.done();
});

scenario('T2 a failed rename reverts and sets error; of two renames the later one wins whatever the answer order', async (env) => {
  const s = await threads(env);
  await s.call('rename', 'c2', 'Fail');
  const bad = await s.take('rename');
  await s.fail(bad.id, {code: 'FORBIDDEN', message: 'no'});
  let c = await s.until((c) => c.error, 'the error');
  assert.deepEqual([titleOf(c, 'c2'), c.error.code], ['Tc2', 'FORBIDDEN']);
  await s.call('rename', 'c3', 'A');
  const ra = await s.take('rename', (e) => e.args.title === 'A');
  await s.call('rename', 'c3', 'B');
  const rb = await s.take('rename', (e) => e.args.title === 'B');
  await s.reply(rb.id, {id: 'c3', title: 'B'});
  await s.reply(ra.id, {id: 'c3', title: 'A'});
  await sleep(150);
  c = await s.chat();
  assert.equal(titleOf(c, 'c3'), 'B');
  await s.done();
});

scenario('T3 a list answered during a pending rename keeps the pending title', async (env) => {
  const s = await threads(env);
  await setAuto(s, 'list', 'hold');
  const seq = await lastSeq(s);
  await s.call('reload');
  const list = await newList(s, seq);
  await s.call('rename', 'c2', 'New');
  const ren = await s.take('rename');
  await s.reply(list.id, LIST);
  await sleep(120);
  assert.equal(titleOf(await s.chat(), 'c2'), 'New', 'the pending title wins');
  await s.reply(ren.id, {id: 'c2', title: 'New'});
  await s.done();
});

// ── T4-T6 archive ───────────────────────────────────────────────────────────
scenario('T4 archiving another conversation removes it at once; a list issued before never brings it back', async (env) => {
  const s = await threads(env);
  await setAuto(s, 'list', 'hold');
  const seq = await lastSeq(s);
  await s.call('reload');
  const list = await newList(s, seq);
  await s.call('archive', 'c2');
  assert.deepEqual(convIds(await s.chat()), ['c3', 'c1']);
  const req = await s.take('archive');
  assert.equal(req.args.id, 'c2');
  await s.reply(req.id, null);
  await s.reply(list.id, LIST);
  await sleep(150);
  assert.deepEqual(convIds(await s.chat()), ['c3', 'c1'], 'the stale list is filtered');
  assert.equal(await s.count('archive'), 1);
  await s.done();
});

scenario('T5 archiving the open conversation while it streams: a new conversation at once, the cancel frame, no stop from the hook', async (env) => {
  const s = await threads(env);
  await open(s, 'c1');
  const req = await sendStarted(s, 'q', 'm1');
  await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'part'});
  await s.until((c) => c.messages.length === 2 && c.messages[1].text === 'part', 'streaming');
  await s.call('archive', 'c1');
  const c = await s.chat();
  assert.deepEqual([c.conversationId, c.messages.length, c.sending], [null, 0, false]);
  assert.ok(!convIds(c).includes('c1'));
  await s.take('cancel', (e) => e.id === req.id);
  const ar = await s.take('archive');
  assert.equal(ar.args.id, 'c1');
  await sleep(120);
  assert.equal(await s.count('stop'), 0, 'the server stops it on archive, not the hook');
  await s.done();
});

scenario('T6 a failed archive restores the item at its place (at the end when the list shrank); archiving twice sends once', async (env) => {
  const s = await threads(env);
  await s.call('archive', 'c2');
  await s.call('archive', 'c2');
  const req = await s.take('archive');
  await s.fail(req.id, {code: 'UNAVAILABLE', message: 'down'});
  let c = await s.until((c) => c.error && convIds(c).includes('c2'), 'restored');
  assert.deepEqual(convIds(c), ['c3', 'c2', 'c1']);
  assert.equal(c.error.code, 'UNAVAILABLE');
  assert.equal(await s.count('archive'), 1, 'one request for two calls');
  await s.call('archive', 'c1');
  const r1 = await s.take('archive', (e) => e.args.id === 'c1');
  await s.call('archive', 'c3');
  const r3 = await s.take('archive', (e) => e.args.id === 'c3');
  await s.reply(r3.id, null);
  await s.fail(r1.id, {code: 'UNAVAILABLE', message: 'down'});
  c = await s.until((c) => convIds(c).includes('c1'), 'c1 restored');
  assert.deepEqual(convIds(c), ['c2', 'c1'], 'index 2 of a one-item list: at the end');
  await s.done();
});

// ── T7, T8 the flags ────────────────────────────────────────────────────────
scenario('T7 the open conversation is in progress while its answer streams, whatever a list says, and not after', async (env) => {
  const s = await threads(env);
  await open(s, 'c1');
  assert.equal(flagOf(await s.chat(), 'c1'), false);
  const req = await sendStarted(s, 'q', 'm1');
  await s.until((c) => flagOf(c, 'c1') === true, 'in progress');
  const seq = await lastSeq(s);
  await s.call('reload');
  await newList(s, seq);
  await sleep(150);
  assert.equal(flagOf(await s.chat(), 'c1'), true, 'a list saying false does not turn it off');
  await finish(s, req, pageOf([u('u1', 'q'), a('m1', 'done')]));
  assert.equal(flagOf(await s.chat(), 'c1'), false);
  await s.done();
});

scenario('T8a leaving while streaming keeps the left conversation in progress', async (env) => {
  const s = await threads(env);
  await open(s, 'c1');
  const req = await sendStarted(s, 'q', 'm1');
  await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'part'});
  await s.until((c) => c.messages[1] && c.messages[1].text === 'part', 'streaming');
  const from = await mark(s);
  await open(s, 'c2');
  assert.equal(flagOf(await s.chat(), 'c1'), true);
  assert.ok((await flagsSince(s, from, 'c1')).every((f) => f === 'true'), 'no render showed it off');
  await s.done();
});

scenario('T8b leaving during queued (a send waiting for the load) leaves it off', async (env) => {
  const s = await threads(env);
  await s.call('select', 'c1');
  await s.take('messages', (e) => e.args.id === 'c1');
  await s.call('send', 'q');
  await s.until((c) => c.sending, 'queued');
  const from = await mark(s);
  await open(s, 'c2');
  assert.equal(flagOf(await s.chat(), 'c1'), false);
  assert.ok((await flagsSince(s, from, 'c1')).every((f) => f === 'false'), 'no render showed it on');
  await s.done();
});

scenario('T8c leaving after completed (during the re-read) leaves it off', async (env) => {
  const s = await threads(env);
  await open(s, 'c1');
  const req = await sendStarted(s, 'q', 'm1');
  await s.event(req.id, {type: 'completed', status: 'success'});
  await s.reply(req.id, null);
  await s.take('messages', (e) => e.args.id === 'c1');
  const from = await mark(s);
  await open(s, 'c2');
  assert.equal(flagOf(await s.chat(), 'c1'), false);
  assert.ok((await flagsSince(s, from, 'c1')).every((f) => f === 'false'));
  await s.done();
});

for (const away of ['select', 'newConversation']) {
  scenario(`T8d re-opened while its answer runs on the server, then left (${away}): still in progress`, async (env) => {
    const s = await threads(env);
    await open(s, 'c1', [u('u1', 'q1'), a('m7', 'Working', {status: 'in_progress'})]);
    assert.equal(flagOf(await s.chat(), 'c1'), true, 'the open conversation is answering');
    const from = await mark(s);
    if (away === 'select') await open(s, 'c2'); else await s.call('newConversation');
    await sleep(60);
    assert.equal(flagOf(await s.chat(), 'c1'), true);
    assert.ok((await flagsSince(s, from, 'c1')).every((f) => f === 'true'));
    await s.done();
  });
}

scenario('T8e re-opened, stopped (rule 26), then left: off', async (env) => {
  const s = await threads(env);
  await open(s, 'c1', [u('u1', 'q1'), a('m7', 'Working', {status: 'in_progress'})]);
  await s.call('stop');
  await s.take('stop');
  await s.until((c) => flagOf(c, 'c1') === false, 'stopped');
  const from = await mark(s);
  await open(s, 'c2');
  assert.equal(flagOf(await s.chat(), 'c1'), false);
  assert.ok((await flagsSince(s, from, 'c1')).every((f) => f === 'false'));
  await s.done();
});

// ── T9, T10 the list poll (clock) ───────────────────────────────────────────
async function step(s) { await s.page.clock.runFor(5000); await sleep(60); }
const withFlag = (flag) => ({items: [conv('c3'), conv('c2'), conv('c1', {in_progress: flag})], next_before: null});

scenario('T9 another conversation in progress: one list read per 5 s until it is done, at most 24 per reset, never after unmount', async (env) => {
  const s = await threads(env);
  await open(s, 'c1');
  const req = await sendStarted(s, 'q', 'm1');
  await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'part'});
  await s.until((c) => c.messages[1] && c.messages[1].text === 'part', 'streaming');
  await setAuto(s, 'list', 'hold');
  await open(s, 'c2');
  await sleep(60);
  let n = await s.count('list');
  let seq = await lastSeq(s);
  await step(s);
  assert.equal(await s.count('list'), n + 1, 'one list read after 5 s');
  let list = await newList(s, seq);
  await s.reply(list.id, withFlag(false));
  await s.until((c) => flagOf(c, 'c1') === false, 'c1 done');
  await step(s);
  assert.equal(await s.count('list'), n + 1, 'no read once nothing else is in progress');

  // The cap: c1 runs again (a list says so) and stays in progress.
  await s.call('select', 'c3');
  const l3 = await s.take('messages', (e) => e.args.id === 'c3');
  await s.reply(l3.id, pageOf([]));
  seq = await lastSeq(s);
  await s.call('reload');
  const r = await newList(s, seq);
  await s.reply(r.id, withFlag(true));
  await s.until((c) => flagOf(c, 'c1') === true, 'c1 running');
  await sleep(60);
  n = await s.count('list');
  for (let i = 0; i < 25; i++) {
    await step(s);
    const pending = (await s.log()).filter((e) => e.op === 'list').length;
    if (pending > n + i) {
      list = await newList(s, seq);
      seq = list.seq;
      await s.reply(list.id, withFlag(true));
      await sleep(40);
    }
  }
  assert.equal(await s.count('list') - n, 24, 'at most 24 reads per reset');
  await s.call('select', 'c2');
  const l2 = await s.take('messages', (e) => e.args.id === 'c2');
  await s.reply(l2.id, pageOf([]));
  await sleep(60);
  await step(s);
  assert.equal(await s.count('list') - n, 25, 'select resets the count');
  list = await newList(s, seq);
  await s.reply(list.id, withFlag(true));
  await sleep(60);
  await s.dom(() => window.__setApp({mounted: false}));
  await sleep(60);
  const after = await s.count('list');
  await step(s);
  assert.equal(await s.count('list'), after, 'nothing after unmount');
  await s.done();
}, {clock: true});

scenario('T10 an initial list with another conversation in progress starts the poll without any leave', async (env) => {
  const s = await threads(env, {list: {items: [conv('c3'), conv('c2', {in_progress: true})], next_before: null}});
  await setAuto(s, 'list', 'hold');
  const n = await s.count('list');
  const seq = await lastSeq(s);
  await step(s);
  assert.equal(await s.count('list'), n + 1);
  const list = await newList(s, seq);
  await s.reply(list.id, {items: [conv('c3'), conv('c2')], next_before: null});
  await s.until((c) => flagOf(c, 'c2') === false, 'c2 done');
  await step(s);
  assert.equal(await s.count('list'), n + 1);
  await s.done();
}, {clock: true});

// ── T11-T13 ─────────────────────────────────────────────────────────────────
scenario('T11 reload() keeps a just-created conversation the page omits and filters an archived one', async (env) => {
  const s = await threads(env, {list: {items: [conv('c2')], next_before: null}});
  const req = await sendStarted(s, 'q1', 'm1');
  await finish(s, req, pageOf([u('u1', 'q1'), a('m1', 'r')]));
  await s.call('reload');
  await s.take('messages', (e) => e.args.id === 'c1').then((r) => s.reply(r.id, pageOf([u('u1', 'q1'), a('m1', 'r')])));
  let c = await s.until((c) => convIds(c).length === 2, 'reloaded');
  assert.deepEqual(convIds(c).sort(), ['c1', 'c2'], 'the created conversation stays');
  await setAuto(s, 'archive', null);
  await s.call('archive', 'c2');
  await s.take('archive');
  await s.call('reload');
  await s.take('messages', (e) => e.args.id === 'c1').then((r) => s.reply(r.id, pageOf([u('u1', 'q1'), a('m1', 'r')])));
  await sleep(150);
  c = await s.chat();
  assert.deepEqual(convIds(c), ['c1'], 'the archived one stays out');
  await s.done();
});

scenario('T12 fixture mode: rename and archive round-trip', async (env) => {
  const s = await start(env, {code: threadsApp(), fixtureMode: true});
  await s.until((c) => !c.loading, 'loaded');
  await s.call('send', 'Revenue?');
  let c = await s.until((c) => !c.sending && c.conversationId && c.messages.length === 2, 'the fixture answer');
  const id = c.conversationId;
  assert.equal(flagOf(c, id), false);
  await s.call('rename', id, 'Renamed');
  c = await s.until((c) => titleOf(c, id) === 'Renamed', 'renamed');
  await s.call('archive', id);
  c = await s.until((c) => c.conversationId === null && !convIds(c).includes(id), 'archived');
  await s.call('reload');
  await sleep(200);
  assert.ok(!convIds(await s.chat()).includes(id));
  await s.done();
});

scenario('T13 a delta that does not change the open flag keeps the same conversations array', async (env) => {
  const s = await threads(env);
  await open(s, 'c1');
  const req = await sendStarted(s, 'q', 'm1');
  await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'a'});
  await s.until((c) => c.messages[1] && c.messages[1].text === 'a', 'the first delta');
  await s.dom(() => { window.__convs = window.__chat.conversations; window.__msgs0 = window.__chat.messages; });
  await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'b'});
  await s.until((c) => c.messages[1].text === 'ab', 'the second delta');
  assert.equal(await s.dom(() => window.__chat.messages !== window.__msgs0 && window.__chat.conversations === window.__convs), true);
  await finish(s, req, pageOf([u('u1', 'q'), a('m1', 'ab')]));
  await s.done();
});

await run('artifactConversationThreads', scenarios);

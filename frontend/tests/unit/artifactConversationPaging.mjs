// Real Chromium: useConversation's older pages (hasOlder, loadingOlder,
// loadOlder) against the fake live host, plus the SDK fixture mode.
//
//   cd frontend && PW_CHROMIUM_PATH=/path/to/chrome node tests/unit/artifactConversationPaging.mjs
import assert from 'node:assert/strict';
import {start, run, u, a, pageOf, ids, history, sendStarted, finish, hookApp, sleep} from './support/chatHarness.mjs';

const scenarios = [];
const scenario = (name, fn, opt = {}) => scenarios.push({name, fn, ...opt});
const older = (e) => e.args.before != null;

// select(A) answered with 30 newest items and the cursor `cursor`.
async function openA(s, cursor = 'k1', n = 30) {
  await s.call('select', 'A');
  const load = await s.take('messages', (e) => e.args.id === 'A' && !older(e));
  await s.reply(load.id, pageOf(history('n', n), cursor));
  return s.until((c) => !c.loading && c.messages.length === n, 'A loaded');
}

scenario('O1 loadOlder asks for the page before the cursor and puts it first', async (env) => {
  const s = await start(env, {opts: {historyLimit: 25}});
  let c = await openA(s);
  assert.deepEqual([c.hasOlder, c.loadingOlder], [true, false], 'a load with next_before has older pages');
  await s.call('loadOlder');
  const req = await s.take('messages', older);
  assert.deepEqual([req.args.id, req.args.before, req.args.limit], ['A', 'k1', 25], 'the cursor and the history limit');
  c = await s.until((c) => c.loadingOlder, 'loadingOlder');
  assert.equal(c.hasOlder, true);
  await s.reply(req.id, pageOf(history('o', 10), null));
  c = await s.until((c) => !c.loadingOlder, 'the older page applied');
  assert.deepEqual(ids(c), [...history('o', 10), ...history('n', 30)].map((m) => m.id), 'the older page first, in page order');
  assert.deepEqual([c.hasOlder, c.loadingOlder, c.error], [false, false, null]);
  await s.done();
});

scenario('O2 one older load at a time; none without older pages', async (env) => {
  const s = await start(env);
  await openA(s);
  await s.call('loadOlder');
  await s.call('loadOlder');
  await s.take('messages', older);
  await sleep(150);
  assert.equal(await s.count('messages', older), 1, 'a second loadOlder while one is in flight sends nothing');
  const req = (await s.log()).find((e) => e.op === 'messages' && older(e));
  await s.reply(req.id, pageOf(history('o', 4), null));
  await s.until((c) => !c.loadingOlder && !c.hasOlder, 'the end reached');
  await s.call('loadOlder');
  await sleep(150);
  assert.equal(await s.count('messages', older), 1, 'loadOlder with hasOlder false sends nothing');
  await s.done();
});

scenario('O3 an older page that overlaps keeps each id once', async (env) => {
  const s = await start(env);
  await openA(s);
  await s.call('loadOlder');
  const req = await s.take('messages', older);
  await s.reply(req.id, pageOf([...history('o', 8), ...history('n', 2)], 'k2'));
  const c = await s.until((c) => !c.loadingOlder && c.messages.length > 30, 'the older page applied');
  const list = ids(c);
  assert.equal(new Set(list).size, list.length, 'no id twice');
  assert.deepEqual(list, [...history('o', 8), ...history('n', 30)].map((m) => m.id));
  assert.equal(c.hasOlder, true, 'k2 is the next cursor');
  await s.done();
});

for (const away of ['select', 'newConversation']) {
  scenario(`O4 ${away}() while an older load is in flight drops its answer`, async (env) => {
    const s = await start(env);
    await openA(s);
    await s.call('loadOlder');
    const req = await s.take('messages', older);
    await s.until((c) => c.loadingOlder, 'loadingOlder');
    let c;
    if (away === 'select') {
      await s.call('select', 'B');
      c = await s.chat();
      assert.deepEqual([c.loadingOlder, c.hasOlder], [false, false], 'reset at once');
      const load = await s.take('messages', (e) => e.args.id === 'B');
      await s.reply(load.id, pageOf([u('b0', 'q'), a('b1', 'r')], null));
      await s.until((c) => !c.loading && c.messages.length === 2, 'B loaded');
    } else {
      await s.call('newConversation');
      c = await s.chat();
      assert.deepEqual([c.loadingOlder, c.hasOlder, c.messages.length], [false, false, 0], 'reset at once');
    }
    const before = await s.chat();
    await s.reply(req.id, pageOf(history('o', 10), null));
    await sleep(200);
    c = await s.chat();
    assert.deepEqual(ids(c), ids(before), 'the dropped answer changed nothing');
    assert.equal(c.version, before.version, 'no new messages array');
    assert.deepEqual([c.loadingOlder, c.hasOlder], [false, false]);
    await s.done();
  });
}

scenario('O5 a failed older load sets error and keeps the cursor for a retry', async (env) => {
  const s = await start(env);
  await openA(s);
  await s.call('loadOlder');
  const req = await s.take('messages', older);
  await s.fail(req.id, {code: 'RATE_LIMITED', message: 'Too many reads'});
  let c = await s.until((c) => !c.loadingOlder && c.error, 'the error');
  assert.equal(c.error.code, 'RATE_LIMITED');
  assert.deepEqual([c.hasOlder, c.messages.length], [true, 30], 'cursor and messages unchanged');
  await s.call('loadOlder');
  const retry = await s.take('messages', older);
  assert.equal(retry.args.before, 'k1', 'the retry asks for the same page');
  await s.reply(retry.id, pageOf(history('o', 2), null));
  c = await s.until((c) => !c.loadingOlder && c.messages.length === 32, 'the retry applied');
  await s.done();
});

scenario('O6 a send after a prepend: the re-read keeps the older items, in order, as the same objects; the cursor stays', async (env) => {
  const s = await start(env);
  await openA(s);
  await s.call('loadOlder');
  const req = await s.take('messages', older);
  await s.reply(req.id, pageOf(history('o', 10), null));
  await s.until((c) => c.messages.length === 40 && !c.hasOlder, 'the older page applied');
  await s.dom(() => { window.__kept = window.__chat.messages.slice(0, 10); });
  const send = await sendStarted(s, 'q', 'm100');
  await s.event(send.id, {type: 'text_delta', block: 'b1', text: 'answer'});
  await finish(s, send, pageOf([...history('n', 30), u('uq', 'q'), a('m100', 'answer')], 'k9'));
  const c = await s.chat();
  assert.deepEqual(ids(c), [...history('o', 10), ...history('n', 30)].map((m) => m.id).concat(['uq', 'm100']), 'oldest to newest, no duplicates');
  assert.equal(await s.dom(() => window.__kept.every((m, i) => window.__chat.messages[i] === m)), true, 'the prepended objects are the same objects');
  assert.equal(c.hasOlder, false, "the re-read's next_before never moves the cursor");
  await s.call('loadOlder');
  await sleep(150);
  assert.equal(await s.count('messages', older), 1, 'still no older page to load');
  await s.done();
});

scenario('O7 an older load during a streaming answer is applied and the run keeps it', async (env) => {
  const s = await start(env);
  await openA(s);
  const send = await sendStarted(s, 'q', 'm100');
  await s.event(send.id, {type: 'text_delta', block: 'b1', text: 'part'});
  await s.until((c) => c.messages[c.messages.length - 1].text === 'part', 'the first delta');
  await s.call('loadOlder');
  const req = await s.take('messages', older);
  await s.reply(req.id, pageOf(history('o', 10), null));
  let c = await s.until((c) => c.messages.length === 42, 'the older page applied during the run');
  assert.deepEqual(ids(c).slice(0, 10), history('o', 10).map((m) => m.id));
  assert.equal(c.sending, true);
  await s.event(send.id, {type: 'text_delta', block: 'b1', text: ' more'});
  await s.until((c) => c.messages[41].text === 'part more', 'the next delta');
  await finish(s, send, pageOf([...history('n', 30), u('uq', 'q'), a('m100', 'part more')], 'k9'));
  c = await s.chat();
  assert.deepEqual(ids(c), [...history('o', 10), ...history('n', 30)].map((m) => m.id).concat(['uq', 'm100']));
  await s.done();
});

scenario('O8 the initial conversationId load sets hasOlder; a conversation created by send has none', async (env) => {
  const s = await start(env, {opts: {conversationId: 'A'}, waitLoad: false});
  const load = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(load.id, pageOf(history('n', 30), 'k1'));
  let c = await s.until((c) => !c.loading && c.messages.length === 30, 'A loaded');
  assert.equal(c.hasOlder, true);
  await s.call('newConversation');
  c = await s.chat();
  assert.deepEqual([c.hasOlder, c.conversationId], [false, null]);
  const send = await sendStarted(s, 'q1', 'm1');
  await finish(s, send, pageOf([u('u1', 'q1'), a('m1', 'r')], 'k5'));
  c = await s.chat();
  assert.deepEqual([c.conversationId, c.hasOlder], ['c1', false], 'a created conversation starts without older pages');
  await s.done();
});

scenario('O9 fixture mode: no older pages, loadOlder makes no request', async (env) => {
  const code = `<script>window.__mcalls = 0; var _m = bow.conversations.messages;
    bow.conversations.messages = function () { window.__mcalls++; return _m.apply(this, arguments); };</script>` + hookApp(null);
  const s = await start(env, {code, fixtureMode: true});
  await s.until((c) => !c.loading, 'the initial load');
  await s.call('send', 'Revenue?');
  let c = await s.until((c) => !c.sending && c.messages.length === 2 && !String(c.messages[0].id).startsWith('local-'), 'the fixture answer');
  assert.equal(c.hasOlder, false);
  const calls = await s.dom(() => window.__mcalls);
  await s.call('loadOlder');
  await sleep(150);
  c = await s.chat();
  assert.equal(await s.dom(() => window.__mcalls), calls, 'no messages() call');
  assert.deepEqual([c.hasOlder, c.loadingOlder], [false, false]);
  await s.done();
});

scenario('O10 a successful older load clears the error its failed attempt set, and only that one', async (env) => {
  const s = await start(env);
  await openA(s);
  await s.call('loadOlder');
  let req = await s.take('messages', older);
  await s.fail(req.id, {code: 'RATE_LIMITED', message: 'Too many reads'});
  await s.until((c) => c.error && !c.loadingOlder, 'the older error');
  await s.call('loadOlder');
  req = await s.take('messages', older);
  await s.reply(req.id, pageOf(history('o', 2), 'k2'));
  let c = await s.until((c) => c.messages.length === 32, 'the retry');
  assert.equal(c.error, null, 'the retry clears its own failure');

  await s.call('loadOlder');
  req = await s.take('messages', older);
  await s.fail(req.id, {code: 'RATE_LIMITED', message: 'Too many reads'});
  await s.until((c) => c.error && c.error.code === 'RATE_LIMITED', 'the older error again');
  await s.call('rename', 'A', 'Other');
  const ren = await s.take('rename');
  await s.fail(ren.id, {code: 'FORBIDDEN', message: 'no'});
  await s.until((c) => c.error && c.error.code === 'FORBIDDEN', 'a later, unrelated error');
  await s.call('loadOlder');
  req = await s.take('messages', older);
  await s.reply(req.id, pageOf(history('p', 2), null));
  c = await s.until((c) => c.messages.length === 34, 'the second retry');
  assert.equal(c.error && c.error.code, 'FORBIDDEN', 'an error set by something else stays');
  await s.done();
});

await run('artifactConversationPaging', scenarios);

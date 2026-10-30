// Real Chromium: useConversation's `answering` and stop() for an answer the
// hook did not start (rule 26): a conversation re-opened while its answer still
// runs on the server can be stopped with that message's id; the item shows
// 'stopped' at once and a tagged re-read brings the server's final status.
//
//   cd frontend && PW_CHROMIUM_PATH=/path/to/chrome node tests/unit/artifactConversationAnswering.mjs
import assert from 'node:assert/strict';
import {start, run, u, a, pageOf, sendStarted, sleep} from './support/chatHarness.mjs';

const scenarios = [];
const scenario = (name, fn, opt = {}) => scenarios.push({name, fn, ...opt});
const running = (id, text = 'Working') => a(id, text, {status: 'in_progress'});
const statusOf = (c, id) => (c.messages.find((m) => m.id === id) || {}).status;
const marker = (s) => s.dom(() => { const e = document.querySelector('[data-testid="bow-chat-ended"]'); return e && e.getAttribute('data-status'); });

async function openRunning(s, id = 'A') {
  await s.call('select', id);
  const load = await s.take('messages', (e) => e.args.id === id);
  await s.reply(load.id, pageOf([u('u1', 'q1'), running('m7')]));
  return s.until((c) => !c.loading && c.messages.length === 2, id + ' loaded');
}
// stop() and the checks before its re-read is answered; returns the re-read request.
async function stopReturned(s, id = 'A') {
  await s.call('stop');
  const stop = await s.take('stop');
  assert.deepEqual([stop.args.id, stop.args.messageId], [id, 'm7'], 'the stop request names the running message');
  const c = await s.until((c) => statusOf(c, 'm7') === 'stopped', 'stopped locally');
  assert.deepEqual([c.answering, c.sending], [false, false], 'no longer answering');
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-ended"][data-status="stopped"]'), null, 'the stopped marker');
  return s.take('messages', (e) => e.args.id === id);
}

scenario('RS1 back in a conversation whose answer still runs: answering, and stop() stops it with its id', async (env) => {
  const s = await start(env);
  let c = await openRunning(s);
  assert.deepEqual([c.answering, c.sending], [true, false], 'answering without a run of its own');
  const read = await stopReturned(s);
  await s.reply(read.id, pageOf([u('u1', 'q1'), a('m7', 'Partial', {status: 'stopped'})]));
  c = await s.until((c) => (c.messages[1] || {}).text === 'Partial', 'the re-read');
  assert.deepEqual([statusOf(c, 'm7'), c.answering], ['stopped', false]);
  assert.equal(await s.count('stop'), 1, 'one stop request');
  assert.equal(await marker(s), 'stopped');
  await s.done();
});

scenario('RS2 the same after a remount with conversationId (the initial load)', async (env) => {
  const s = await start(env, {opts: {conversationId: 'A'}, waitLoad: false});
  const load = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(load.id, pageOf([u('u1', 'q1'), running('m7')]));
  let c = await s.until((c) => !c.loading && c.messages.length === 2, 'A loaded');
  assert.deepEqual([c.answering, c.sending], [true, false]);
  const read = await stopReturned(s);
  await s.reply(read.id, pageOf([u('u1', 'q1'), a('m7', '', {status: 'stopped'})]));
  c = await s.until((c) => statusOf(c, 'm7') === 'stopped' && (c.messages[1] || {}).text === '', 'the re-read');
  assert.equal(await s.count('stop'), 1);
  await s.done();
});

scenario('RS3 the server finished first: the re-read shows success', async (env) => {
  const s = await start(env);
  await openRunning(s);
  const read = await stopReturned(s);
  await s.reply(read.id, pageOf([u('u1', 'q1'), a('m7', 'The full answer')]));
  const c = await s.until((c) => statusOf(c, 'm7') === 'success', 'server truth');
  assert.deepEqual([c.messages[1].text, c.answering], ['The full answer', false]);
  assert.equal(await marker(s), null, 'no stopped marker on a finished answer');
  await s.done();
});

scenario('RS4 a lagging re-read that still says in_progress keeps it stopped', async (env) => {
  const s = await start(env);
  await openRunning(s);
  const read = await stopReturned(s);
  const before = (await s.chat()).version;
  await s.reply(read.id, pageOf([u('u1', 'q1'), running('m7', 'Working more')]));
  const c = await s.until((c) => c.version > before, 'the re-read applied');
  assert.deepEqual([statusOf(c, 'm7'), c.answering], ['stopped', false], 'held stopped');
  assert.equal(await marker(s), 'stopped');
  await s.done();
});

for (const order of ['refresh first', 'stop re-read first']) {
  scenario(`RS4 a refresh re-read in flight at the stop answers in_progress afterwards (${order}): still stopped`, async (env) => {
    const s = await start(env);
    await openRunning(s);
    await sleep(60);
    await s.page.clock.runFor(5000);
    const refresh = await s.take('messages', (e) => e.args.id === 'A');
    const read = await stopReturned(s);
    const later = pageOf([u('u1', 'q1'), running('m7', 'Working more')]);
    const v0 = (await s.chat()).version;
    if (order === 'refresh first') { await s.reply(refresh.id, later); await sleep(100); await s.reply(read.id, later); }
    else { await s.reply(read.id, later); await sleep(100); await s.reply(refresh.id, later); }
    const c = await s.until((c) => c.version > v0, 'a re-read applied');
    await sleep(100);
    assert.deepEqual([statusOf(await s.chat(), 'm7'), (await s.chat()).answering], ['stopped', false]);
    assert.equal(c.sending, false);
    await s.done();
  }, {clock: true});
}

scenario('RS5 no-ops: nothing running, a second stop(), a detached stopped run waiting for started', async (env) => {
  let s = await start(env);
  await s.call('select', 'A');
  const load = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(load.id, pageOf([u('u1', 'q1'), a('m1', 'done')]));
  let c = await s.until((c) => !c.loading && c.messages.length === 2, 'A loaded');
  assert.equal(c.answering, false);
  await s.call('stop');
  await sleep(150);
  assert.equal(await s.count('stop'), 0, 'nothing in progress: no request');

  s = await start(env);
  await openRunning(s);
  const read = await stopReturned(s);
  await s.call('stop');
  await sleep(150);
  assert.equal(await s.count('stop'), 1, 'a second stop() finds nothing running');
  await s.reply(read.id, pageOf([u('u1', 'q1'), a('m7', '', {status: 'stopped'})]));

  s = await start(env);
  await s.call('send', 'q');
  const send = await s.take('send');
  await s.call('stop');
  c = await s.until((c) => !c.sending && c.messages[1].status === 'stopped', 'stopped before started');
  assert.ok(String(c.messages[1].id).startsWith('local-'));
  await s.call('stop');
  await sleep(150);
  assert.equal(await s.count('stop'), 0, 'a local stopped answer sends nothing (the deferred stop waits for started)');
  await s.event(send.id, {type: 'started', message_id: 'm9'});
  await s.take('stop', (e) => e.args.messageId === 'm9', 'the deferred stop');
  await sleep(100);
  assert.equal(await s.count('stop'), 1);
  await s.done();
});

scenario('RS6 an active run: stop() is the Phase-1 stop (the started id, then the cancel frame)', async (env) => {
  const s = await start(env);
  const req = await sendStarted(s, 'q1', 'm1');
  let c = await s.until((c) => c.messages[1] && c.messages[1].id === 'm1', 'started');
  assert.deepEqual([c.answering, c.sending], [true, true]);
  await s.call('stop');
  const stop = await s.take('stop');
  assert.deepEqual([stop.args.id, stop.args.messageId], ['c1', 'm1']);
  await s.take('cancel', (e) => e.id === req.id);
  c = await s.until((c) => !c.sending, 'stopped');
  assert.equal(c.answering, false);
  const log = await s.log();
  assert.ok(log.findIndex((e) => e.op === 'stop') < log.findIndex((e) => e.op === 'cancel'), 'the stop goes out before the cancel frame');
  await s.done();
});

scenario('RS7 left while streaming, selected back, stopped: the pending refresh is cancelled', async (env) => {
  const s = await start(env);
  const req = await sendStarted(s, 'q1', 'm1');
  await s.event(req.id, {type: 'text_delta', block: 'b1', text: 'Part'});
  await s.until((c) => c.messages[1].text === 'Part', 'the first delta');
  await s.call('newConversation');
  await s.take('cancel', (e) => e.id === req.id);
  await s.call('select', 'c1');
  const load = await s.take('messages', (e) => e.args.id === 'c1');
  await s.reply(load.id, pageOf([u('u1', 'q1'), a('m1', 'Part', {status: 'in_progress'})]));
  let c = await s.until((c) => !c.loading && c.conversationId === 'c1', 'c1 loaded');
  assert.deepEqual([c.answering, c.sending], [true, false]);
  await sleep(60);
  const reads = await s.count('messages');
  await s.call('stop');
  const stop = await s.take('stop');
  assert.deepEqual([stop.args.id, stop.args.messageId], ['c1', 'm1']);
  const read = await s.take('messages', (e) => e.args.id === 'c1');
  await s.reply(read.id, pageOf([u('u1', 'q1'), a('m1', 'Part', {status: 'stopped'})]));
  c = await s.until((c) => statusOf(c, 'm1') === 'stopped' && !c.answering, 'stopped');
  await s.page.clock.runFor(5000);
  await sleep(100);
  await s.page.clock.runFor(5000);
  await sleep(100);
  assert.equal(await s.count('messages'), reads + 1, 'only the stop\'s own re-read, no refresh');
  await s.done();
}, {clock: true});

scenario('RS8 select(B) while A\'s stop re-read is in flight drops its answer', async (env) => {
  const s = await start(env);
  await openRunning(s);
  const read = await stopReturned(s);
  await s.call('select', 'B');
  const load = await s.take('messages', (e) => e.args.id === 'B');
  await s.reply(load.id, pageOf([u('b1', 'other'), a('b2', 'answer')]));
  const b = await s.until((c) => !c.loading && c.conversationId === 'B' && c.messages.length === 2, 'B loaded');
  await s.reply(read.id, pageOf([u('u1', 'q1'), a('m7', '', {status: 'stopped'}), u('u2', 'late')]));
  await sleep(200);
  const c = await s.chat();
  assert.equal(c.version, b.version, 'nothing applied');
  assert.deepEqual(c.messages.map((m) => m.id), ['b1', 'b2']);
  assert.equal(c.answering, false);
  await s.done();
});

await run('artifactConversationAnswering', scenarios);

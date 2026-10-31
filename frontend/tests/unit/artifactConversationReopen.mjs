// Real Chromium: useConversation reopens, on mount without a conversationId,
// the newest listed conversation whose answer is still running (rule 27,
// OD-10), unless the user acted first; it never flashes an empty state.
//
//   cd frontend && PW_CHROMIUM_PATH=/path/to/chrome node tests/unit/artifactConversationReopen.mjs
import assert from 'node:assert/strict';
import {start, run, u, a, pageOf, sleep, CONV} from './support/chatHarness.mjs';

// window.__log gets [loading, messages.length, conversationId] per render.
const reopenApp = (opts = null) => `<script>
(function () {
  var h = React.createElement;
  window.__log = []; window.__msgsVersion = 0;
  function App() {
    var chat = useConversation(${JSON.stringify(opts)});
    window.__chat = chat;
    window.__log.push([chat.loading, chat.messages.length, chat.conversationId]);
    return h('div', {id: 'chat'}, chat.messages.map(function (m) { return h(ChatMessage, {key: m.id, message: m}); }));
  }
  ReactDOM.createRoot(document.getElementById('root')).render(h(App));
})();
</script>`;

const conv = (id, extra = {}) => ({id, title: 'T' + id, created_at: '2026-10-30T10:00:00Z', updated_at: '2026-10-30T10:00:00Z', in_progress: false, ...extra});
const RUNNING = {items: [conv('c3'), conv('c2', {in_progress: true}), conv('c1', {in_progress: true})], next_before: null};
const IDLE = {items: [conv('c3'), conv('c2'), conv('c1')], next_before: null};
const scenarios = [];
const scenario = (name, fn, opt = {}) => scenarios.push({name, fn, ...opt});
const history = () => pageOf([u('u1', 'q1'), a('m7', 'Working', {status: 'in_progress'})]);

async function reopened(s) {
  const load = await s.take('messages', (e) => e.args.id === 'c2', 'the reopen');
  await s.reply(load.id, history());
  return s.until((c) => !c.loading && c.conversationId === 'c2' && c.messages.length === 2, 'c2 loaded');
}

scenario('RO1 a mount without conversationId opens the newest running conversation (c2, not the idle c3)', async (env) => {
  const s = await start(env, {code: reopenApp(), auto: {list: RUNNING}, waitLoad: false});
  const c = await reopened(s);
  assert.equal(c.conversationId, 'c2');
  await sleep(150);
  assert.deepEqual((await s.log()).filter((e) => e.op === 'messages').map((e) => e.args.id), ['c2'], 'one history read, for c2');
  await s.done();
});

scenario('RO2 loading stays true until the reopened history applies: no empty state in between', async (env) => {
  const s = await start(env, {code: reopenApp(), auto: {list: RUNNING}, waitLoad: false});
  await reopened(s);
  const log = await s.dom(() => window.__log.slice());
  assert.deepEqual(log.filter(([loading, n]) => !loading && n === 0), [], 'no render with loading false and no messages: ' + JSON.stringify(log));
  await s.done();
});

scenario('RO3 nothing running: no history read, a new conversation (MR-5)', async (env) => {
  const s = await start(env, {code: reopenApp(), auto: {list: IDLE}});
  await sleep(150);
  const c = await s.chat();
  assert.deepEqual([c.conversationId, c.messages.length, await s.count('messages')], [null, 0, 0]);
  await s.done();
});

scenario('RO4 a mount with conversationId opens that one, never another', async (env) => {
  const s = await start(env, {code: reopenApp({conversationId: 'c1'}), auto: {list: RUNNING}, waitLoad: false});
  const load = await s.take('messages', (e) => e.args.id === 'c1');
  await s.reply(load.id, pageOf([u('u1', 'q'), a('m1', 'r')]));
  await s.until((c) => !c.loading && c.messages.length === 2, 'c1 loaded');
  await sleep(150);
  const c = await s.chat();
  assert.equal(c.conversationId, 'c1');
  assert.deepEqual((await s.log()).filter((e) => e.op === 'messages').map((e) => e.args.id), ['c1']);
  await s.done();
});

for (const act of ['newConversation', 'select', 'send', 'archive']) {
  scenario(`RO5 ${act} before the list answers: no reopen`, async (env) => {
    const s = await start(env, {code: reopenApp(), auto: {list: 'hold'}, waitLoad: false});
    const list = await s.take('list');
    if (act === 'newConversation') await s.call('newConversation');
    if (act === 'select') await s.call('select', 'c3');
    if (act === 'send') await s.call('send', 'q');
    // Archive of another conversation: no generation, run or conversation
    // changes, only the user's action marks the mount as touched.
    if (act === 'archive') await s.call('archive', 'c3');
    await s.reply(list.id, RUNNING);
    if (act === 'select') {
      const load = await s.take('messages', (e) => e.args.id === 'c3');
      await s.reply(load.id, pageOf([]));
    }
    if (act === 'send') {
      const send = await s.take('send');
      assert.equal(send.args.id, CONV.id, 'the send created its own conversation');
    }
    await sleep(200);
    const c = await s.chat();
    assert.notEqual(c.conversationId, 'c2');
    assert.equal(await s.count('messages', (e) => e.args.id === 'c2'), 0, 'c2 is never read');
    await s.done();
  });
}

scenario('RO6 after the reopen, answering is true and stop() stops the running message', async (env) => {
  const s = await start(env, {code: reopenApp(), auto: {list: RUNNING}, waitLoad: false});
  const c = await reopened(s);
  assert.deepEqual([c.answering, c.sending], [true, false]);
  await s.call('stop');
  const stop = await s.take('stop');
  assert.deepEqual([stop.args.id, stop.args.messageId], ['c2', 'm7']);
  await s.until((c) => !c.answering, 'stopped');
  await s.done();
});

scenario('RO7 no reopen in fixture mode, nor when the policy is unavailable', async (env) => {
  let s = await start(env, {code: reopenApp(), fixtureMode: true});
  await s.until((c) => !c.loading, 'loaded');
  assert.equal((await s.chat()).conversationId, null);
  await s.done();
  s = await start(env, {code: reopenApp(), auto: {policy: {available: false, reason: 'not_member'}, list: RUNNING}});
  await sleep(150);
  assert.deepEqual([(await s.chat()).conversationId, await s.count('list'), await s.count('messages')], [null, 0, 0]);
  await s.done();
});

await run('artifactConversationReopen', scenarios);

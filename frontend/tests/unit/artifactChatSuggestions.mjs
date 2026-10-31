// Real Chromium: <ChatSuggestions chat={chat} items={[...]}/>
// (artifact-globals.js): starter questions that send on a click only, and
// show only while the conversation is empty, loaded and available.
//
//   cd frontend && PW_CHROMIUM_PATH=/path/to/chrome node tests/unit/artifactChatSuggestions.mjs
import assert from 'node:assert/strict';
import {start, run, u, a, pageOf, finish, sleep} from './support/chatHarness.mjs';

const ITEMS = "['Revenue by month?', {label: 'Top genres', text: 'Which genres sell best?'}, {label: 'Only a label'}, '   ', {text: ''}, null, 'הכנסות לפי חודש?']";

const stubApp = (items = ITEMS) => `<script>
(function () {
  var h = React.createElement;
  window.__componentType = typeof window.ChatSuggestions;
  window.__calls = [];
  var base = {messages: [], conversations: [], policy: {available: true}, loading: false, sending: false, answering: false, error: null,
    send: function (t) { window.__calls.push(['send', t]); }};
  function App() {
    var s = React.useState(base);
    window.__setChat = function (p) { s[1](Object.assign({}, base, p)); };
    if (typeof window.ChatSuggestions !== 'function') return h('p', {id: 'missing'}, 'ChatSuggestions is not defined');
    return h('div', {style: {width: '600px'}}, h(window.ChatSuggestions, {chat: s[0], items: ${items}}));
  }
  ReactDOM.createRoot(document.getElementById('root')).render(h(App));
})();
</script>`;

// The hook with ChatMessages, the suggestions as its empty slot.
const liveApp = `<script>
(function () {
  var h = React.createElement;
  window.__componentType = typeof window.ChatSuggestions;
  function App() {
    var chat = useConversation();
    window.__chat = chat;
    if (typeof window.ChatSuggestions !== 'function') return h('p', {id: 'missing'}, 'ChatSuggestions is not defined');
    return h('div', {style: {height: '500px', display: 'flex', flexDirection: 'column'}},
      h(ChatMessages, {chat: chat, empty: h(ChatSuggestions, {chat: chat, items: ['Revenue per month?', 'Top genres?']})}));
  }
  ReactDOM.createRoot(document.getElementById('root')).render(h(App));
})();
</script>`;

const scenarios = [];
const scenario = (name, fn, opt = {}) => scenarios.push({name, fn, ...opt});
const calls = (s) => s.dom(() => window.__calls.slice());
const chips = (s) => s.dom(() => Array.from(document.querySelectorAll('[data-testid="bow-chat-suggestion"]')).map((b) => b.textContent));
const shown = (s) => s.dom(() => !!document.querySelector('[data-testid="bow-chat-suggestions"]'));
async function stub(env) {
  const s = await start(env, {code: stubApp(), waitLoad: false});
  assert.equal(await s.dom(() => window.__componentType), 'function', 'window.ChatSuggestions is a component');
  return s;
}
const setChat = (s, patch) => s.dom((p) => window.__setChat(p), patch);

scenario('S1 a click sends exactly the item\'s text once (strings and {label, text}); blank items are skipped', async (env) => {
  const s = await stub(env);
  assert.deepEqual(await chips(s), ['Revenue by month?', 'Top genres', 'Only a label', 'הכנסות לפי חודש?']);
  const click = (i) => s.dom((i) => document.querySelectorAll('[data-testid="bow-chat-suggestion"]')[i].click(), i);
  await click(0);
  await click(1);
  await click(2);
  assert.deepEqual(await calls(s), [['send', 'Revenue by month?'], ['send', 'Which genres sell best?'], ['send', 'Only a label']]);
  assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-suggestion"]').type), 'button');
  await s.done();
});

scenario('S2 hidden while loading, with messages and when unavailable', async (env) => {
  const s = await stub(env);
  assert.equal(await shown(s), true);
  await setChat(s, {loading: true});
  assert.equal(await shown(s), false, 'loading');
  await setChat(s, {messages: [{id: 'u1', role: 'user', text: 'q'}]});
  assert.equal(await shown(s), false, 'messages');
  await setChat(s, {policy: {available: false, reason: 'auth_required'}});
  assert.equal(await shown(s), false, 'unavailable');
  await setChat(s, {policy: null});
  assert.equal(await shown(s), true, 'the policy still loading does not hide them');
  await s.done();
});

scenario('S2 with useConversation: a click sends, they hide with the first message and come back after newConversation()', async (env) => {
  const s = await start(env, {code: liveApp});
  assert.equal(await s.dom(() => window.__componentType), 'function', 'window.ChatSuggestions is a component');
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-empty"] [data-testid="bow-chat-suggestions"]'), null, 'the suggestions in the empty slot');
  await s.frame.click('[data-testid="bow-chat-suggestion"]');
  const req = await s.take('send');
  assert.equal(req.args.text, 'Revenue per month?');
  await s.event(req.id, {type: 'started', message_id: 'm1'});
  assert.equal(await shown(s), false, 'hidden with the first message');
  await finish(s, req, pageOf([u('u1', 'Revenue per month?'), a('m1', 'r')]));
  await s.call('newConversation');
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-suggestions"]'), null, 'shown again');
  assert.equal(await s.count('send'), 1);
  await s.done();
});

scenario('S3 no send without a click', async (env) => {
  const s = await start(env, {code: liveApp});
  assert.equal(await s.dom(() => window.__componentType), 'function', 'window.ChatSuggestions is a component');
  await sleep(500);
  assert.equal(await shown(s), true);
  assert.deepEqual([await s.count('send'), await s.count('create')], [0, 0]);
  await s.done();
});

scenario('S4 each suggestion is dir="auto": a Hebrew one is right-to-left', async (env) => {
  const s = await stub(env);
  const dirs = await s.dom(() => Array.from(document.querySelectorAll('[data-testid="bow-chat-suggestion"]')).map((b) => [b.getAttribute('dir'), getComputedStyle(b).direction]));
  assert.deepEqual(dirs, [['auto', 'ltr'], ['auto', 'ltr'], ['auto', 'ltr'], ['auto', 'rtl']]);
  await s.done();
});

scenario('S5 disabled while sending', async (env) => {
  const s = await stub(env);
  await setChat(s, {sending: true});
  assert.deepEqual(await s.dom(() => Array.from(document.querySelectorAll('[data-testid="bow-chat-suggestion"]')).map((b) => b.disabled)), [true, true, true, true]);
  await s.dom(() => document.querySelector('[data-testid="bow-chat-suggestion"]').click());
  assert.deepEqual(await calls(s), []);
  await s.done();
});

scenario('S6 fixture mode: ChatMessages empty={<ChatSuggestions/>}: a click shows the first message and the suggestions go', async (env) => {
  const s = await start(env, {code: liveApp, fixtureMode: true});
  await s.until((c) => !c.loading, 'loaded');
  await s.frame.click('[data-testid="bow-chat-suggestion"]');
  await s.until((c) => !c.sending && c.messages.length === 2, 'the fixture answer');
  await s.waitFor(() => document.querySelectorAll('[data-testid="bow-chat-row"]').length === 2, null, 'two rows');
  assert.equal(await shown(s), false);
  assert.equal((await s.chat()).messages[0].text, 'Revenue per month?');
  await s.done();
});

await run('artifactChatSuggestions', scenarios);

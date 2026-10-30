// Real Chromium: <ChatComposer chat={chat}/> (artifact-globals.js). Enter
// sends and Shift+Enter adds a line; an IME composition never sends; Stop
// replaces Send while chat.answering; it is disabled with the reason when the
// viewer may not chat; its direction is right while empty. Stub `chat`
// objects with spies for the component alone, the fake live host for the
// integration with useConversation and ChatMessages.
//
//   cd frontend && PW_CHROMIUM_PATH=/path/to/chrome node tests/unit/artifactChatComposer.mjs
import assert from 'node:assert/strict';
import {start, run, u, a, pageOf, sleep} from './support/chatHarness.mjs';

// A stub chat: send/stop record their calls in window.__calls (no guard of
// their own); window.__setChat(patch) re-renders with base + patch.
const stubApp = ({props = '{}', wrapDir = null, pre = '', noBow = false} = {}) => `${pre}<script>
(function () {
  var h = React.createElement;
  ${noBow ? 'delete window.bow;' : ''}
  window.__componentType = typeof window.ChatComposer;
  window.__calls = [];
  var base = {messages: [], conversations: [], policy: {available: true}, loading: false, sending: false, answering: false, error: null,
    send: function (t) { window.__calls.push(['send', t]); }, stop: function () { window.__calls.push(['stop']); }};
  function App() {
    var s = React.useState(base);
    window.__setChat = function (p) { s[1](Object.assign({}, base, p)); };
    if (typeof window.ChatComposer !== 'function') return h('p', {id: 'missing'}, 'ChatComposer is not defined');
    return h('div', {dir: ${JSON.stringify(wrapDir)} || undefined, style: {width: '600px'}}, h(window.ChatComposer, Object.assign({chat: s[0]}, ${props})));
  }
  ReactDOM.createRoot(document.getElementById('root')).render(h(App));
})();
</script>`;

// The hook with ChatMessages and ChatComposer.
const liveApp = (pre = '') => `${pre}<script>
(function () {
  var h = React.createElement;
  window.__componentType = typeof window.ChatComposer;
  function App() {
    var chat = useConversation();
    window.__chat = chat;
    if (typeof window.ChatComposer !== 'function') return h('p', {id: 'missing'}, 'ChatComposer is not defined');
    return h('div', {style: {height: '500px', display: 'flex', flexDirection: 'column'}}, [
      h(ChatMessages, {key: 'm', chat: chat}), h(ChatComposer, {key: 'c', chat: chat, placeholder: 'Ask'})]);
  }
  ReactDOM.createRoot(document.getElementById('root')).render(h(App));
})();
</script>`;

const scenarios = [];
const scenario = (name, fn, opt = {}) => scenarios.push({name, fn, ...opt});
const IN = '[data-testid="bow-chat-input"]';
const calls = (s) => s.dom(() => window.__calls.slice());
const value = (s) => s.dom((sel) => document.querySelector(sel).value, IN);
async function stub(env, opts) {
  const s = await start(env, {code: stubApp(opts), waitLoad: false});
  assert.equal(await s.dom(() => window.__componentType), 'function', 'window.ChatComposer is a component');
  await s.waitFor((sel) => document.querySelector(sel), IN, 'the textarea');
  return s;
}
const setChat = (s, patch) => s.dom((p) => window.__setChat(p), patch);

scenario('K1 Enter sends once and clears the draft; Shift+Enter adds a line; a Send click sends once', async (env) => {
  const s = await stub(env, {props: "{placeholder: 'Ask'}"});
  await s.frame.fill(IN, 'q1');
  await s.frame.press(IN, 'Enter');
  assert.deepEqual(await calls(s), [['send', 'q1']]);
  assert.equal(await value(s), '', 'the draft is cleared');
  assert.equal(await s.dom((sel) => document.activeElement === document.querySelector(sel), IN), true, 'the focus stays');
  await s.frame.fill(IN, 'line one');
  await s.frame.press(IN, 'Shift+Enter');
  assert.equal(await value(s), 'line one\n', 'a new line');
  assert.deepEqual(await calls(s), [['send', 'q1']], 'nothing sent');
  await s.frame.fill(IN, 'q2');
  await s.frame.click('[data-testid="bow-chat-send"]');
  assert.deepEqual(await calls(s), [['send', 'q1'], ['send', 'q2']]);
  const send = await s.dom(() => { const b = document.querySelector('[data-testid="bow-chat-send"]'); return [b.type, b.getAttribute('aria-label'), !!b.querySelector('svg')]; });
  assert.deepEqual(send, ['submit', 'Send', true]);
  assert.equal(await s.dom((sel) => document.querySelector(sel).getAttribute('aria-label'), IN), 'Ask');
  await s.done();
});

scenario('K2 a blank draft never sends and Send is disabled', async (env) => {
  const s = await stub(env);
  assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-send"]').disabled), true, 'disabled while empty');
  await s.frame.fill(IN, '   ');
  await s.frame.press(IN, 'Enter');
  assert.deepEqual(await calls(s), []);
  assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-send"]').disabled), true, 'disabled for whitespace');
  assert.equal(await s.dom((sel) => document.querySelector(sel).getAttribute('aria-label'), IN), 'Send', 'no placeholder: the Send label names it');
  await s.done();
});

scenario('K3 Enter during an IME composition never sends', async (env) => {
  const s = await stub(env);
  await s.frame.fill(IN, 'שלום');
  const key = (extra) => s.dom(([sel, extra]) => {
    const el = document.querySelector(sel);
    const e = new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true, cancelable: true, isComposing: !!extra.isComposing});
    if (extra.keyCode) Object.defineProperty(e, 'keyCode', {get: () => extra.keyCode});
    el.dispatchEvent(e);
  }, [IN, extra]);
  await key({keyCode: 229});
  assert.deepEqual(await calls(s), [], 'keyCode 229 does not send');
  await s.dom((sel) => document.querySelector(sel).dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true, data: ''})), IN);
  await key({isComposing: true});
  assert.deepEqual(await calls(s), [], 'isComposing does not send');
  await key({});
  assert.deepEqual(await calls(s), [], 'a plain Enter while a composition is open does not send');
  await s.dom((sel) => document.querySelector(sel).dispatchEvent(new CompositionEvent('compositionend', {bubbles: true, data: 'ם'})), IN);
  await key({});
  assert.deepEqual(await calls(s), [['send', 'שלום']], 'after compositionend, Enter sends once');
  // Best effort: a real composition through CDP (recorded, not failed, when the shell refuses it).
  try {
    const cdp = await s.page.context().newCDPSession(s.page);
    await s.frame.fill(IN, 'abc');
    await s.frame.focus(IN);
    await cdp.send('Input.imeSetComposition', {text: 'に', selectionStart: 1, selectionEnd: 1});
    await s.page.keyboard.press('Enter');
    await sleep(50);
    const n = (await calls(s)).length;
    await cdp.send('Input.insertText', {text: 'に'});
    console.log('  K3 CDP composition: ' + (n === 1 ? 'Enter during the composition did not send' : 'sent during the composition (calls=' + n + ')'));
    assert.equal(n, 1, 'a real composition does not send on Enter');
  } catch (e) {
    if (e && e.code === 'ERR_ASSERTION') throw e;
    console.log('  K3 CDP composition: not run (' + String(e && e.message || e).split('\n')[0] + ')');
  }
  await s.done();
});

for (const sending of [true, false]) {
  scenario(`K4 answering (sending ${sending}): Stop replaces Send; Enter neither sends nor stops; the textarea stays enabled`, async (env) => {
    const s = await stub(env);
    await setChat(s, {answering: true, sending});
    await s.waitFor(() => document.querySelector('[data-testid="bow-chat-stop"]'), null, 'Stop');
    const state = await s.dom((sel) => {
      const b = document.querySelector('[data-testid="bow-chat-stop"]');
      return [!!document.querySelector('[data-testid="bow-chat-send"]'), b.type, b.getAttribute('aria-label'), !!b.querySelector('svg'), document.querySelector(sel).disabled];
    }, IN);
    assert.deepEqual(state, [false, 'button', 'Stop', true, false]);
    await s.frame.fill(IN, 'next question');
    await s.frame.press(IN, 'Enter');
    assert.deepEqual(await calls(s), [], 'Enter sends nothing while answering and never stops');
    assert.equal(await value(s), 'next question', 'the draft is kept');
    await s.frame.click('[data-testid="bow-chat-stop"]');
    assert.deepEqual(await calls(s), [['stop']]);
    await setChat(s, {});
    await s.waitFor(() => document.querySelector('[data-testid="bow-chat-send"]'), null, 'Send back');
    await s.done();
  });
}

scenario('K5 unavailable: disabled with the reason (auth_required, other, Hebrew labels); enabled while the policy loads', async (env) => {
  const s = await stub(env);
  const read = () => s.dom((sel) => {
    const p = document.querySelector('[data-testid="bow-chat-unavailable"]');
    return [document.querySelector(sel).disabled, p && p.textContent, p && p.getAttribute('dir'), document.querySelector('[data-testid="bow-chat-send"]').disabled];
  }, IN);
  await setChat(s, {policy: {available: false, reason: 'auth_required'}});
  assert.deepEqual(await read(), [true, 'Sign in to chat', 'auto', true]);
  await setChat(s, {policy: {available: false, reason: 'not_member'}});
  assert.deepEqual(await read(), [true, 'Chat is not available here', 'auto', true]);
  await setChat(s, {policy: null});
  assert.deepEqual(await read(), [false, null, null, true], 'policy null while it loads: enabled');
  await s.frame.fill(IN, 'q');
  await setChat(s, {policy: {available: false, reason: 'auth_required'}});
  await s.frame.press(IN, 'Enter').catch(() => {});
  assert.deepEqual(await calls(s), [], 'nothing is sent when unavailable');
  await s.done();

  const he = await stub(env, {props: "{labels: {authRequired: 'יש להתחבר', unavailable: 'הצ׳אט אינו זמין', send: 'שלח'}}"});
  await setChat(he, {policy: {available: false, reason: 'auth_required'}});
  assert.equal(await he.dom(() => document.querySelector('[data-testid="bow-chat-unavailable"]').textContent), 'יש להתחבר');
  await setChat(he, {policy: {available: false}});
  assert.equal(await he.dom(() => document.querySelector('[data-testid="bow-chat-unavailable"]').textContent), 'הצ׳אט אינו זמין');
  assert.equal(await he.dom(() => document.querySelector('[data-testid="bow-chat-send"]').getAttribute('aria-label')), 'שלח');
  await he.done();

  const none = await stub(env, {noBow: true});
  await setChat(none, {policy: null, error: {code: 'UNAVAILABLE', message: 'Conversations are unavailable here'}});
  assert.deepEqual(await none.dom((sel) => [document.querySelector(sel).disabled, document.querySelector('[data-testid="bow-chat-unavailable"]').textContent], IN), [true, 'Chat is not available here'], 'no window.bow: disabled');
  await none.done();
});

scenario('K6 direction: an empty composer takes the placeholder\'s direction; typed text is dir="auto"; an explicit dir wins', async (env) => {
  const s = await stub(env, {props: "{placeholder: 'שאלו שאלה'}"});
  const dir = () => s.dom((sel) => { const el = document.querySelector(sel); return [el.getAttribute('dir'), getComputedStyle(el).direction]; }, IN);
  assert.deepEqual(await dir(), ['rtl', 'rtl'], 'empty, Hebrew placeholder, LTR frame: rtl, no dir="auto"');
  await s.frame.fill(IN, 'Revenue by month');
  assert.deepEqual(await dir(), ['auto', 'ltr'], 'English text');
  await s.frame.fill(IN, 'הכנסות לפי חודש');
  assert.deepEqual(await dir(), ['auto', 'rtl'], 'Hebrew text in an LTR frame');
  await s.frame.fill(IN, '');
  assert.deepEqual(await dir(), ['rtl', 'rtl'], 'empty again');
  await s.done();

  const plain = await stub(env, {props: "{placeholder: '...'}", wrapDir: 'rtl'});
  assert.deepEqual(await plain.dom((sel) => { const el = document.querySelector(sel); return [el.getAttribute('dir'), getComputedStyle(el).direction]; }, IN), [null, 'rtl'], 'a placeholder with no strong character inherits');
  await plain.done();

  const fixed = await stub(env, {props: "{dir: 'rtl', placeholder: 'Ask'}"});
  const fdir = () => fixed.dom((sel) => { const el = document.querySelector(sel); return [el.getAttribute('dir'), getComputedStyle(el).direction]; }, IN);
  assert.deepEqual(await fdir(), ['rtl', 'rtl'], 'explicit dir while empty');
  await fixed.frame.fill(IN, 'English');
  assert.deepEqual(await fdir(), ['rtl', 'rtl'], 'explicit dir with English text');
  await fixed.done();
});

scenario('K7 the textarea grows to maxRows lines, then scrolls; clearing returns to one row', async (env) => {
  const s = await stub(env);
  await sleep(200);
  const box = () => s.dom((sel) => { const el = document.querySelector(sel); const cs = getComputedStyle(el); return {h: el.getBoundingClientRect().height, lh: parseFloat(cs.lineHeight), sh: el.scrollHeight, ch: el.clientHeight}; }, IN);
  const one = await box();
  await s.frame.fill(IN, Array.from({length: 10}, (_, i) => 'line ' + i).join('\n'));
  await sleep(50);
  const ten = await box();
  assert.ok(Math.abs((ten.h - one.h) - 5 * ten.lh) <= 2, `six lines tall: one row ${one.h}, ten lines ${ten.h}, line ${ten.lh}`);
  assert.ok(ten.sh > ten.ch + ten.lh, 'the rest scrolls inside');
  await s.frame.fill(IN, '');
  await sleep(50);
  assert.ok(Math.abs((await box()).h - one.h) <= 1, 'back to one row');
  await s.done();
});

scenario('K8 two Enter keydowns in one task send once; two Enter presses send once', async (env) => {
  const s = await stub(env);
  await s.frame.fill(IN, 'q');
  await s.dom((sel) => {
    const el = document.querySelector(sel);
    for (let i = 0; i < 2; i++) el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true, cancelable: true}));
  }, IN);
  assert.deepEqual(await calls(s), [['send', 'q']], 'one send for two keydowns in the same task');
  await s.frame.fill(IN, 'r');
  await s.frame.press(IN, 'Enter');
  await s.frame.press(IN, 'Enter');
  assert.deepEqual(await calls(s), [['send', 'q'], ['send', 'r']]);
  await s.done();
});

scenario('K9 with useConversation: Enter sends one request; Stop while streaming stops it with the started id', async (env) => {
  const s = await start(env, {code: liveApp()});
  assert.equal(await s.dom(() => window.__componentType), 'function', 'window.ChatComposer is a component');
  await s.frame.fill(IN, 'Revenue?');
  await s.frame.press(IN, 'Enter');
  const req = await s.take('send');
  assert.equal(req.args.text, 'Revenue?');
  await s.event(req.id, {type: 'started', message_id: 'm1'});
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-stop"]'), null, 'Stop');
  await s.frame.press(IN, 'Enter');
  // A pointer click on Stop within 500 ms of the send is taken as the rest of the Send click (K12).
  await sleep(550);
  await s.frame.click('[data-testid="bow-chat-stop"]');
  const stop = await s.take('stop');
  assert.deepEqual([stop.args.id, stop.args.messageId], ['c1', 'm1']);
  await s.take('cancel', (e) => e.id === req.id);
  assert.equal(await s.count('send'), 1, 'one send request');
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-send"]'), null, 'Send back');
  await s.done();
});

scenario('K10 an app\'s own Icon and LoadingSpinner do not change the composer\'s icons', async (env) => {
  const s = await stub(env, {pre: '<script>function Icon() { return null; } function LoadingSpinner() { return null; }</script>'});
  assert.equal(await s.dom(() => window.Icon.length === 0 && window.Icon() === null), true, 'the app replaced window.Icon');
  assert.equal(await s.dom(() => !!document.querySelector('[data-testid="bow-chat-send"] svg')), true);
  await setChat(s, {answering: true, sending: true});
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-stop"] svg'), null, 'the Stop icon');
  await s.done();
});

scenario('K11 back in a conversation whose answer still runs: Stop shows, stops it by id, and Send returns', async (env) => {
  const s = await start(env, {code: liveApp()});
  assert.equal(await s.dom(() => window.__componentType), 'function', 'window.ChatComposer is a component');
  await s.call('select', 'A');
  const load = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(load.id, pageOf([u('u1', 'q1'), a('m7', 'Working', {status: 'in_progress'})]));
  await s.until((c) => !c.loading && c.messages.length === 2, 'A loaded');
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-stop"]') && !document.querySelector('[data-testid="bow-chat-send"]'), null, 'Stop instead of Send');
  await s.frame.click('[data-testid="bow-chat-stop"]');
  const stop = await s.take('stop');
  assert.deepEqual([stop.args.id, stop.args.messageId], ['A', 'm7']);
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-ended"][data-status="stopped"]'), null, 'the stopped marker');
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-send"]'), null, 'Send back');
  const read = await s.take('messages', (e) => e.args.id === 'A');
  await s.reply(read.id, pageOf([u('u1', 'q1'), a('m7', 'Working', {status: 'stopped'})]));
  await s.done();
});

scenario('K12 a double click on Send sends once and never stops the question', async (env) => {
  const s = await start(env, {code: liveApp()});
  assert.equal(await s.dom(() => window.__componentType), 'function', 'window.ChatComposer is a component');
  await s.frame.fill(IN, 'Revenue?');
  await s.frame.dblclick('[data-testid="bow-chat-send"]');
  const req = await s.take('send');
  assert.equal(req.args.text, 'Revenue?');
  await sleep(200);
  const c = await s.chat();
  assert.deepEqual([await s.count('send'), await s.count('stop')], [1, 0]);
  assert.ok(c.messages.length === 2 && c.messages.every((m) => m.status !== 'stopped'), 'nothing stopped: ' + JSON.stringify(c.messages.map((m) => m.status)));
  assert.equal(c.sending, true, 'the answer runs');
  await s.event(req.id, {type: 'started', message_id: 'm1'});
  await sleep(550);
  await s.frame.click('[data-testid="bow-chat-stop"]');
  await s.take('stop');
  await s.done();
});

scenario('K13 a keyboard Stop right after an Enter send still stops', async (env) => {
  const s = await stub(env);
  await s.frame.fill(IN, 'q');
  await s.frame.press(IN, 'Enter');
  await setChat(s, {answering: true, sending: true});
  await s.waitFor(() => document.querySelector('[data-testid="bow-chat-stop"]'), null, 'Stop');
  await s.frame.focus('[data-testid="bow-chat-stop"]');
  await s.page.keyboard.press('Enter');
  assert.deepEqual(await calls(s), [['send', 'q'], ['stop']]);
  await s.done();
});

await run('artifactChatComposer', scenarios);

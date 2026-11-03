import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

// Run: node --test tests/unit/embedSdk.mjs
// The host's SDK (backend/app/static/embed/bow-embed.js), loaded as a host page
// loads it, against a stub window and document. It tells the host apart:
// an artifact that is not available (`unavailable`, with its reason) from a
// session that needs a sign-in (`authRequired`). Only the frame it mounted,
// from BOW's origin, with the init nonce, is listened to.

const src = readFileSync(new URL('../../../backend/app/static/embed/bow-embed.js', import.meta.url), 'utf8')
const BOW = 'https://bow.example.com'

function host() {
  let onMessage = null
  const sent = []
  const contentWindow = { postMessage: (msg, origin) => sent.push({ msg, origin }) }
  const node = () => ({
    style: {}, setAttribute() {}, appendChild() {}, addEventListener() {}, parentNode: null,
  })
  const iframe = { ...node(), contentWindow }
  const document = {
    createElement: (tag) => (tag === 'iframe' ? iframe : node()),
    querySelector: () => node(),
    body: node(),
  }
  const window = {
    crypto: { getRandomValues: (bytes) => { bytes.forEach((_, i) => { bytes[i] = i + 1 }); return bytes } },
    location: { href: 'https://erp.example.com/assistant' },
    console,
    setTimeout: () => null,
    clearTimeout: () => {},
    addEventListener: (type, fn) => { if (type === 'message') onMessage = fn },
    removeEventListener: () => {},
  }
  new Function('window', 'document', src)(window, document)
  const embed = window.BOWEmbed.mount({ baseUrl: BOW, hostApp: 'host_1', artifact: 'a1', container: '#bow' })
  // The frame says hello; the SDK answers with init, which carries the nonce.
  onMessage({ source: contentWindow, origin: BOW, data: { type: 'bow:hello' } })
  const nonce = sent.find((s) => s.msg.type === 'bow:init').msg.nonce
  const events = []
  for (const name of ['unavailable', 'authRequired', 'authenticated']) embed.on(name, (detail) => events.push({ name, detail }))
  const frame = (data) => onMessage({ source: contentWindow, origin: BOW, data })
  return { embed, nonce, events, frame }
}

{
  const { embed, nonce, events, frame } = host()
  assert.ok(nonce, 'the init carries a nonce')
  frame({ type: 'bow:unavailable', nonce, reason: 'not_found', message: 'm' })
  assert.deepEqual(events, [{ name: 'unavailable', detail: { reason: 'not_found', message: 'm' } }])
  assert.equal(embed.state.status, 'unavailable')

  // A later sign-in (a renewal that finds the artifact) is reported as today.
  frame({ type: 'bow:auth', nonce, status: 'authenticated', user: { name: 'Alice' } })
  assert.equal(embed.state.status, 'authenticated')
}
{
  const { embed, events, frame } = host()
  frame({ type: 'bow:unavailable', nonce: 'not-the-nonce', reason: 'not_found', message: 'm' })
  assert.deepEqual(events, [], 'a message without the nonce is ignored')
  assert.equal(embed.state.status, 'starting')
}
{
  const { events, frame, nonce } = host()
  frame({ type: 'bow:auth', nonce, status: 'failed', error: 'Your session ended.' })
  assert.deepEqual(events.map((e) => e.name), ['authRequired'], 'a dead session asks for a sign-in, it is not "unavailable"')
}
console.log('PASS: the SDK tells the host an artifact is unavailable apart from a sign-in it needs')

// ── start timeout and teardown: timers, DOM removal and listener removal are
// modelled, and the frame only says what each test makes it say ───────────
function mountWith(options = {}) {
  let onMessage = null
  let now = 0
  const timers = []
  const sent = []
  const contentWindow = { postMessage: (msg, origin) => sent.push({ msg, origin }) }
  const removed = []
  const node = (name) => {
    const n = { name, style: {}, setAttribute() {}, addEventListener() {}, parentNode: null, children: [] }
    n.appendChild = (child) => { child.parentNode = n; n.children.push(child) }
    n.removeChild = (child) => { child.parentNode = null; removed.push(child.name) }
    return n
  }
  const iframe = { ...node('iframe'), contentWindow }
  let made = 0
  const document = {
    createElement: (tag) => (tag === 'iframe' ? iframe : node(made++ === 0 ? 'wrapper' : 'launcher')),
    querySelector: () => node('container'),
    body: node('body'),
  }
  const window = {
    crypto: { getRandomValues: (bytes) => { bytes.forEach((_, i) => { bytes[i] = i + 1 }); return bytes } },
    location: { href: 'https://erp.example.com/assistant' },
    console,
    setTimeout: (fn, ms) => { const t = { fn, at: now + ms, done: false }; timers.push(t); return t },
    clearTimeout: (t) => { if (t) t.done = true },
    addEventListener: (type, fn) => { if (type === 'message') onMessage = fn },
    removeEventListener: (type, fn) => { if (type === 'message' && onMessage === fn) onMessage = null },
  }
  const tick = (ms) => {
    now += ms
    for (const t of timers) if (!t.done && t.at <= now) { t.done = true; t.fn() }
  }
  new Function('window', 'document', src)(window, document)
  const embed = window.BOWEmbed.mount({ baseUrl: BOW, hostApp: 'host_1', artifact: 'a1', container: '#bow', ...options })
  const events = []
  for (const name of ['unavailable', 'authRequired', 'authenticated', 'error']) embed.on(name, (detail) => events.push({ name, detail }))
  const deliver = (data, from = {}) => onMessage && onMessage({ source: contentWindow, origin: BOW, data, ...from })
  const hello = () => { deliver({ type: 'bow:hello' }); return sent.filter((s) => s.msg.type === 'bow:init').at(-1)?.msg.nonce }
  return { embed, events, deliver, hello, tick, sent, removed, listening: () => onMessage !== null }
}

{
  // The browser refused the frame: it never runs, so nothing ever arrives.
  const { embed, events, tick } = mountWith()
  tick(15000 - 1)
  assert.deepEqual(events, [])
  tick(1)
  assert.deepEqual(events.map((e) => [e.name, e.detail.reason]), [['unavailable', 'no_response']])
  assert.equal(embed.state.status, 'unavailable')
  assert.match(events[0].detail.message, /origin/)
}
{
  // A bare hello is not enough: a parent the frame refuses (not in allowed
  // origins) gets hello, but never a message with the nonce.
  const { events, hello, tick } = mountWith()
  hello()
  tick(15000)
  assert.deepEqual(events.map((e) => e.detail.reason), ['no_response'])
}
{
  // The frame answers with the nonce (here, asking for a credential): no timeout,
  // however long the sign-in itself takes.
  const { events, hello, deliver, tick } = mountWith({ identity: { getAssertion: () => new Promise(() => {}) } })
  const nonce = hello()
  deliver({ type: 'bow:credential-request', nonce, requestId: 'r1', kind: 'assertion' })
  tick(60000)
  assert.deepEqual(events, [])
}
{
  // A late answer after the timeout still continues normally.
  const { embed, events, hello, deliver, tick } = mountWith()
  tick(15000)
  const nonce = hello()
  deliver({ type: 'bow:auth', nonce, status: 'authenticated', user: { name: 'Alice' } })
  assert.deepEqual(events.map((e) => e.name), ['unavailable', 'authenticated'])
  assert.equal(embed.state.status, 'authenticated')
}
{
  // startTimeout: 0 turns it off; a destroyed embed never reports it.
  const off = mountWith({ startTimeout: 0 })
  off.tick(600000)
  assert.deepEqual(off.events, [])
  const gone = mountWith()
  gone.embed.destroy()
  gone.tick(15000)
  assert.deepEqual(gone.events, [])
}
console.log('PASS: a frame that never answers with the nonce is reported as unavailable/no_response, and only that')

{
  // logout(); destroy(); back to back: the frame is hidden at once and removed
  // only when it says signed_out, so its sign-out (revoking the session) runs.
  const { embed, events, hello, deliver, sent, removed, listening } = mountWith()
  const nonce = hello()
  deliver({ type: 'bow:auth', nonce, status: 'authenticated', user: {} })
  events.length = 0
  embed.logout()
  embed.destroy()
  assert.ok(sent.some((s) => s.msg.type === 'bow:logout'), 'the logout was sent')
  assert.deepEqual(removed, [], 'the frame is not removed yet')
  assert.equal(embed.iframe.parentNode.style.display, 'none', 'but it is hidden')
  // Only the exact frame, from BOW, with the nonce, saying signed_out, counts.
  deliver({ type: 'bow:auth', nonce: 'wrong', status: 'signed_out' })
  deliver({ type: 'bow:auth', nonce, status: 'signed_out' }, { origin: 'https://evil.example' })
  deliver({ type: 'bow:auth', nonce, status: 'signed_out' }, { source: {} })
  deliver({ type: 'bow:auth', nonce, status: 'authenticated' })
  assert.deepEqual(removed, [])
  deliver({ type: 'bow:auth', nonce, status: 'signed_out' })
  assert.deepEqual(removed, ['wrapper'])
  assert.equal(listening(), false, 'the message listener is gone')
  assert.deepEqual(events, [], 'the host hears nothing after destroy')
  embed.destroy()
  assert.deepEqual(removed, ['wrapper'], 'destroy twice is harmless')
}
{
  // A frame that never says signed_out is removed after 6 s at most.
  const { embed, hello, deliver, tick, removed, listening } = mountWith()
  const nonce = hello()
  deliver({ type: 'bow:auth', nonce, status: 'authenticated', user: {} })
  embed.logout().destroy()
  tick(6000 - 1)
  assert.deepEqual(removed, [])
  tick(1)
  assert.deepEqual(removed, ['wrapper'])
  assert.equal(listening(), false)
}
{
  // No logout, or a frame that never answered (no session): removed at once.
  const a = mountWith()
  a.hello()
  a.embed.destroy()
  assert.deepEqual(a.removed, ['wrapper'])
  const b = mountWith()
  b.embed.logout().destroy()
  assert.deepEqual(b.removed, ['wrapper'])
}
{
  // After destroy the embed is inert: a listener that destroys it stops the
  // rest of that dispatch, late credential failures and new listeners are ignored.
  const { embed, hello, deliver, events } = mountWith({ identity: { getAssertion: () => Promise.reject(new Error('no')) } })
  const nonce = hello()
  let second = 0
  embed.on('authenticated', () => embed.destroy())
  embed.on('authenticated', () => { second++ })
  deliver({ type: 'bow:credential-request', nonce, requestId: 'r1', kind: 'assertion' })
  deliver({ type: 'bow:auth', nonce, status: 'authenticated', user: {} })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(second, 0, 'the second listener did not run')
  assert.deepEqual(events.map((e) => e.name), ['authenticated'], 'the late credential failure emitted nothing')
  let late = 0
  embed.on('authenticated', () => { late++ })
  assert.equal(late, 0)
}
console.log('PASS: destroy after logout waits for the frame to sign out (6 s at most), and the embed is inert after destroy')

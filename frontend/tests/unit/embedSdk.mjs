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
// `styleFor(name)` may give a node a style object of its own, such as one that
// refuses a value the way a browser does; by default every style is a plain object.
// `computed(color)` stands in for getComputedStyle's background-color.
function mountWith(options = {}, { styleFor, computed } = {}) {
  let onMessage = null
  let now = 0
  const timers = []
  const sent = []
  const contentWindow = { postMessage: (msg, origin) => sent.push({ msg, origin }) }
  const removed = []
  const node = (name) => {
    const n = { name, style: (styleFor && styleFor(name)) || {}, setAttribute() {}, addEventListener() {}, parentNode: null, children: [] }
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
    getComputedStyle: computed ? (el) => ({ backgroundColor: computed(el.style.backgroundColor) }) : undefined,
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
  return { embed, events, deliver, hello, tick, sent, removed, body: document.body, listening: () => onMessage !== null }
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

// ── sizes, z-index and launcher colour ──────────────────────────────────────
{
  // Spy on console.warn while fn runs, and always put it back.
  const withWarnings = (fn) => {
    const original = console.warn
    const warnings = []
    console.warn = (...args) => { warnings.push(args.map(String).join(' ')) }
    try {
      return { result: fn(), warnings }
    } finally {
      console.warn = original
    }
  }
  // A wrapper style that acts as a browser's for width and height: a value it
  // can't parse is dropped and the old one stays. `dvh: false` is an older
  // browser without the dynamic viewport units. (A real browser is checked in
  // embedSdkBrowser.mjs.)
  const browserStyle = ({ dvh = true } = {}) => (name) => {
    if (name !== 'wrapper') return null
    const kept = { width: '', height: '' }
    const valid = (v) => v === '' || !(/;|nonsense|url\(/.test(v) || /(^|min\()[1-9][\d.]*(,|$)/.test(v) || (!dvh && /[dsl]vh/i.test(v)))
    const style = { cssText: '' }
    for (const prop of ['width', 'height']) {
      Object.defineProperty(style, prop, { get: () => kept[prop], set: (v) => { if (valid(String(v))) kept[prop] = String(v) } })
    }
    return style
  }
  const panelOf = (options, stub = { styleFor: browserStyle() }) =>
    mountWith({ container: undefined, mode: 'floating', ...options }, stub).embed.iframe.parentNode.style
  const panel = (options, stub) => withWarnings(() => panelOf(options, stub))

  // Floating default: 600 x 720, capped by the viewport (dvh where known).
  {
    const { result, warnings } = panel({})
    assert.equal(result.width, 'min(600px, calc(100vw - 32px))')
    assert.equal(result.height, 'min(720px, calc(100dvh - 120px))')
    assert.deepEqual(warnings, [])
  }
  // A browser without dvh keeps the vh line.
  assert.equal(panel({ height: '80dvh' }, { styleFor: browserStyle({ dvh: false }) }).result.height, 'min(80vh, calc(100vh - 120px))')
  // A number is pixels; any CSS length the browser keeps is used, in both modes, including calc() and small values.
  for (const [given, shown] of [[480, '480px'], ['40rem', '40rem'], ['0.9em', '0.9em'], ['calc(100vh - 80px)', 'calc(100vh - 80px)'], [' 50% ', '50%']]) {
    const { result, warnings } = panel({ width: given, height: given })
    assert.equal(result.width, `min(${shown}, calc(100vw - 32px))`, `floating width ${given}`)
    assert.equal(result.height, `min(${shown}, calc(100dvh - 120px))`, `floating height ${given}`)
    assert.deepEqual(warnings, [])
    assert.equal(mountWith({ height: given }, { styleFor: browserStyle() }).embed.iframe.parentNode.style.height, shown, `inline height ${given}`)
  }
  // A value the browser refuses, a number below 1, or a non-string warns once per option and is the default.
  for (const bad of ['600px;background:url(x)', 'nonsense', '10', '', '  ', -5, 0, 0.5, NaN, Infinity, {}, true, Object.create(null)]) {
    const { result, warnings } = panel({ width: bad, height: bad })
    assert.equal(result.width, 'min(600px, calc(100vw - 32px))', `refuses width ${typeof bad === 'object' ? 'object' : String(bad)}`)
    assert.equal(result.height, 'min(720px, calc(100dvh - 120px))')
    assert.equal(warnings.length, 2)
    assert.match(warnings[0], /^\[bow-embed\] ignoring width /)
    const inline = withWarnings(() => mountWith({ height: bad }, { styleFor: browserStyle() }).embed.iframe.parentNode.style.height)
    assert.equal(inline.result, '100%')
    assert.equal(inline.warnings.length, 1)
  }
  // Absent: the default, without a word. Inline mode fills its container.
  assert.deepEqual(panel({ width: null, height: undefined }).warnings, [])
  assert.equal(mountWith({}).embed.iframe.parentNode.style.height, '100%')
  assert.equal(mountWith({ width: 300 }).embed.iframe.parentNode.style.width, '100%')
  // A host console.warn that throws does not break the mount.
  {
    const original = console.warn
    console.warn = () => { throw new Error('the host console is broken') }
    try {
      assert.equal(panelOf({ width: 'nonsense', zIndex: 'bad', accentColor: 12 }).width, 'min(600px, calc(100vw - 32px))')
    } finally {
      console.warn = original
    }
  }

  // z-index: 'auto' or an integer in CSS's range, as a number or a digit string; the launcher gets the same.
  const floating = (options, stub) => {
    const { body, embed } = mountWith({ container: undefined, mode: 'floating', ...options }, stub)
    return { css: embed.iframe.parentNode.style.cssText, launcher: body.children.find((n) => n.name === 'launcher').style }
  }
  for (const [given, shown] of [[0, '0'], ['1000', '1000'], [' 7 ', '7'], [-3, '-3'], ['auto', 'auto'], ['AUTO', 'auto'],
    [-2147483648, '-2147483648'], [2147483647, '2147483647']]) {
    const { result, warnings } = withWarnings(() => floating({ zIndex: given }))
    assert.match(result.css, new RegExp(`z-index:${shown}(;|$)`), `keeps zIndex ${JSON.stringify(given)}`)
    assert.match(result.launcher.cssText, new RegExp(`z-index:${shown}(;|$)`))
    assert.deepEqual(warnings, [])
  }
  for (const bad of [1.5, 1e21, 2147483648, '-2147483649', '1.5', '1;color:red', '', 'inherit', NaN, {}]) {
    const { result, warnings } = withWarnings(() => floating({ zIndex: bad }))
    assert.match(result.css, /z-index:2147483000(;|$)/, `refuses zIndex ${String(bad)}`)
    assert.doesNotMatch(result.css + result.launcher.cssText, /color:red/)
    assert.equal(warnings.length, 1)
    assert.match(warnings[0], /^\[bow-embed\] ignoring zIndex /)
  }
  assert.match(floating({}).css, /z-index:2147483000(;|$)/)

  // accentColor: any colour the browser keeps, set through background-color only.
  const INVALID = ['blu', 'rgb()', 'red;x:y', 'url(x)']
  const browserLauncher = (name) => {
    if (name !== 'launcher') return null
    let color = ''
    return {
      cssText: '',
      get backgroundColor() { return color },
      set backgroundColor(v) { if (!INVALID.includes(v)) color = String(v) },
    }
  }
  // What getComputedStyle gives: see-through for transparent or an undefined var(), white for currentcolor (the icon is white).
  const computed = (color) => ({ 'transparent': 'rgba(0, 0, 0, 0)', 'var(--missing)': 'rgba(0, 0, 0, 0)', 'currentcolor': 'rgb(255, 255, 255)',
    '#fff': 'rgb(255, 255, 255)', '#2563eb': 'rgb(37, 99, 235)' })[color] || 'rgb(1, 2, 3)'
  const accent = (accentColor) => withWarnings(() => floating({ accentColor }, { styleFor: browserLauncher, computed }).launcher)
  for (const good of ['#abc', '#abcd', 'hsl(220deg 80% 50%)', 'oklch(60% 0.2 250)', 'var(--brand)', 'rgba(37, 99, 235, 0.5)']) {
    const { result, warnings } = accent(good)
    assert.equal(result.backgroundColor, good, `keeps accentColor ${good}`)
    assert.deepEqual(warnings, [])
  }
  assert.equal(accent('  #abc ').result.backgroundColor, '#abc')
  for (const bad of [...INVALID, '', 12, {}]) {
    const { result, warnings } = accent(bad)
    assert.equal(result.backgroundColor, '#2563eb', `refuses accentColor ${String(bad)}`)
    assert.doesNotMatch(result.cssText, /x:y|url\(|blu/)
    assert.equal(warnings.length, 1)
  }
  // A colour that would hide the launcher (see-through, or white like the icon) warns and is the default.
  for (const hidden of ['transparent', 'var(--missing)', 'currentcolor', '#fff']) {
    const { result, warnings } = accent(hidden)
    assert.equal(result.backgroundColor, '#2563eb', `refuses hidden accentColor ${hidden}`)
    assert.equal(warnings.length, 1)
    assert.match(warnings[0], /hide the launcher/)
  }
  for (const absent of [undefined, null]) {
    const { result, warnings } = accent(absent)
    assert.equal(result.backgroundColor, '#2563eb')
    assert.deepEqual(warnings, [])
  }
  // The launcher's style text resets the background longhands but never sets the colour or the shorthand.
  {
    const css = accent('#abc').result.cssText
    assert.doesNotMatch(css, /background-color|background:/)
    for (const reset of ['background-image:none;', 'background-clip:border-box;', 'background-origin:padding-box;']) assert.ok(css.includes(reset))
  }
}
console.log('PASS: sizes take pixels or any CSS length the browser keeps (else a warning and the default), z-index takes integers or auto, the launcher colour stays visible')

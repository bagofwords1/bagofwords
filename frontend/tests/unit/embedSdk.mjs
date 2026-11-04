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
function mountWith(options = {}, { styleFor } = {}) {
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

// ── floating panel size ──────────────────────────────────────────────────────
{
  const panel = (options) => mountWith({ container: undefined, mode: 'floating', ...options }).embed.iframe.parentNode.style.cssText
  // The default is wide enough to show a chat as BOW's artifact preview does.
  assert.match(panel(), /width:min\(600px,calc\(100vw - 32px\)\);height:min\(720px,calc\(100vh - 120px\)\)/)
  // A number is pixels, a string is one CSS length; the viewport still caps both.
  assert.match(panel({ width: 480, height: 560 }), /width:min\(480px,calc\(100vw - 32px\)\);height:min\(560px,calc\(100vh - 120px\)\)/)
  assert.match(panel({ width: '40rem', height: '80vh' }), /width:min\(40rem,calc\(100vw - 32px\)\);height:min\(80vh,calc\(100vh - 120px\)\)/)
  // Anything else falls back to the default; it never reaches the style text.
  // (Each refusal also warns; the warning is tested below, and kept quiet here.)
  const warn = console.warn
  console.warn = () => {}
  try {
    for (const bad of ['600px;background:url(x)', 'calc(1px)', '', -5, 0, NaN, Infinity, {}, '10']) {
      const css = panel({ width: bad, height: bad })
      assert.match(css, /width:min\(600px,.*height:min\(720px,/, `rejects ${String(bad)}`)
      assert.doesNotMatch(css, /url\(/)
    }
  } finally {
    console.warn = warn
  }
  // Inline mode is sized by its container, as before.
  const inline = mountWith({ width: 300 }).embed.iframe.parentNode.style
  assert.equal(inline.width, '100%')
}
console.log('PASS: the floating panel is 600 x 720 by default, sized by width/height when given, capped by the viewport')

// ── size options: warnings, tiny sizes, units, inline height, z-index ───────
{
  // Spy on console.warn for one mount, and always put it back.
  const warnsFor = (options) => {
    const original = console.warn
    const warnings = []
    console.warn = (...args) => { warnings.push(args.map(String).join(' ')) }
    try {
      const css = mountWith({ container: undefined, mode: 'floating', ...options }).embed.iframe.parentNode.style.cssText
      return { css, warnings }
    } finally {
      console.warn = original
    }
  }
  // A value that was given but refused is reported, naming the option and the value.
  {
    const { css, warnings } = warnsFor({ width: '600px;background:url(x)' })
    assert.match(css, /width:min\(600px,/)
    assert.equal(warnings.length, 1)
    assert.match(warnings[0], /^\[bow-embed\] .*width.*600px;background:url\(x\)/)
  }
  {
    const { warnings } = warnsFor({ height: 'calc(1px)' })
    assert.equal(warnings.length, 1)
    assert.match(warnings[0], /^\[bow-embed\] .*height.*calc\(1px\)/)
  }
  // Valid or absent values say nothing.
  assert.deepEqual(warnsFor({}).warnings, [])
  assert.deepEqual(warnsFor({ width: 480, height: '80vh', zIndex: 5 }).warnings, [])
  assert.deepEqual(warnsFor({ width: null, height: undefined }).warnings, [])
  // Sizes below 1, in any unit, would give an invisible panel: refused, with a warning.
  for (const tiny of ['0px', '0%', '0.0rem', '0.5px', 1e-7, 0.5]) {
    const { css, warnings } = warnsFor({ width: tiny, height: tiny })
    assert.match(css, /width:min\(600px,.*height:min\(720px,/, `rejects ${String(tiny)}`)
    assert.equal(warnings.length, 2, `warns for ${String(tiny)}`)
  }
  // Units match in any case, and the dynamic viewport units are accepted.
  {
    const { css, warnings } = warnsFor({ width: '40PX', height: '90dvh' })
    // A dynamic unit gets a plain vh line first, so a browser without dvh still sizes the panel.
    assert.match(css, /width:min\(40PX,calc\(100vw - 32px\)\);height:min\(90vh,calc\(100vh - 120px\)\);height:min\(90dvh,calc\(100dvh - 120px\)\)/)
    assert.deepEqual(warnings, [])
  }
  assert.match(warnsFor({ height: '50svh' }).css, /height:min\(50svh,/)
  assert.match(warnsFor({ height: '50lvh' }).css, /height:min\(50lvh,/)
  // The height cap follows the mobile toolbar where the browser knows dvh; the vh one stays first for older browsers.
  assert.match(warnsFor({}).css, /height:min\(720px,calc\(100vh - 120px\)\);height:min\(720px,calc\(100dvh - 120px\)\);/)
  // z-index 0 is a real value; anything that is not a finite number keeps the default.
  assert.match(warnsFor({ zIndex: 0 }).css, /z-index:0(;|$)/)
  assert.match(warnsFor({}).css, /z-index:2147483000(;|$)/)
  assert.match(warnsFor({ zIndex: '1;color:red' }).css, /z-index:2147483000(;|$)/)
  const launcher = mountWith({ container: undefined, mode: 'floating', zIndex: 0 }).body.children.find((n) => n.name === 'launcher')
  assert.match(launcher.style.cssText, /z-index:0(;|$)/)
}
{
  // Inline height: a number is pixels; any other value is used as given, default 100%.
  const inlineHeight = (height) => mountWith({ height }).embed.iframe.parentNode.style.height
  assert.equal(inlineHeight(720), '720px')
  assert.equal(inlineHeight('calc(100vh - 80px)'), 'calc(100vh - 80px)')
  assert.equal(inlineHeight('clamp(300px, 50vh, 800px)'), 'clamp(300px, 50vh, 800px)')
  assert.equal(inlineHeight(undefined), '100%')
}
console.log('PASS: refused sizes warn, sizes below 1 are refused, units match in any case, inline height takes pixels, z-index 0 is kept')

// ── z-index strings, inline height refusals, a fallback for dynamic units,
// warnings that cannot break a mount, accent colour ──────────────────────────
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
  const floating = (options) => {
    const { body, embed } = mountWith({ container: undefined, mode: 'floating', ...options })
    return { css: embed.iframe.parentNode.style.cssText, launcher: body.children.find((n) => n.name === 'launcher').style.cssText }
  }

  // z-index: an integer, as a number or as a string of digits (spaces around are fine).
  for (const [given, shown] of [['1000', '1000'], [' 7 ', '7'], [-3, '-3'], ['-3', '-3'], [2147483647, '2147483647']]) {
    const { result, warnings } = withWarnings(() => floating({ zIndex: given }))
    assert.match(result.css, new RegExp(`z-index:${shown}(;|$)`), `keeps zIndex ${JSON.stringify(given)}`)
    assert.match(result.launcher, new RegExp(`z-index:${shown}(;|$)`))
    assert.deepEqual(warnings, [])
  }
  // Anything else that was given is refused once, with a warning, and never reaches the style text.
  for (const bad of [1.5, 1e21, 2147483648, '2147483648', '1.5', '1;color:red', '', 'auto', NaN, {}]) {
    const { result, warnings } = withWarnings(() => floating({ zIndex: bad }))
    assert.match(result.css, /z-index:2147483000(;|$)/, `refuses zIndex ${String(bad)}`)
    assert.match(result.launcher, /z-index:2147483000(;|$)/)
    assert.doesNotMatch(result.css + result.launcher, /color:red/)
    assert.equal(warnings.length, 1, `warns once for zIndex ${String(bad)}`)
    assert.match(warnings[0], /^\[bow-embed\] ignoring zIndex /)
  }
  assert.deepEqual(withWarnings(() => floating({ zIndex: null })).warnings, [])

  // Inline height: a number follows the floating rule (finite, at least 1).
  const inline = (height, stub) => mountWith({ height }, stub).embed.iframe.parentNode.style.height
  assert.equal(inline(1.5), '1.5px')
  for (const bad of [0.5, -5, 0, Infinity, NaN, Object.create(null), true]) {
    const { result, warnings } = withWarnings(() => inline(bad))
    assert.equal(result, '100%', `refuses inline height ${typeof bad === 'number' ? bad : typeof bad}`)
    assert.equal(warnings.length, 1)
    assert.match(warnings[0], /^\[bow-embed\] ignoring height /)
  }
  // A string the browser refuses (it reads back empty) warns and falls back to 100%.
  const refusing = (bad) => (name) => {
    if (name !== 'wrapper') return null
    let height = ''
    return { get height() { return height }, set height(v) { height = bad.includes(v) ? '' : String(v) } }
  }
  {
    const { result, warnings } = withWarnings(() => inline('nonsense', { styleFor: refusing(['nonsense']) }))
    assert.equal(result, '100%')
    assert.equal(warnings.length, 1)
    assert.match(warnings[0], /^\[bow-embed\] ignoring height "nonsense"/)
  }
  {
    const { result, warnings } = withWarnings(() => inline('calc(100vh - 80px)', { styleFor: refusing(['nonsense']) }))
    assert.equal(result, 'calc(100vh - 80px)')
    assert.deepEqual(warnings, [])
  }

  // Dynamic viewport units: the first line uses plain vh, the second the value as written.
  assert.match(floating({ height: '80dvh' }).css, /height:min\(80vh,calc\(100vh - 120px\)\);height:min\(80dvh,calc\(100dvh - 120px\)\);/)
  assert.match(floating({ height: '50SVH' }).css, /height:min\(50vh,calc\(100vh - 120px\)\);height:min\(50SVH,calc\(100dvh - 120px\)\);/)
  assert.match(floating({ width: '50lvh' }).css, /width:min\(50vh,calc\(100vw - 32px\)\);width:min\(50lvh,calc\(100vw - 32px\)\);height:/)
  assert.doesNotMatch(floating({ width: '40rem' }).css, /width:.*width:/, 'one width line when there is nothing to fall back from')

  // A value that cannot be turned into text still warns, and the mount goes on.
  {
    const { result, warnings } = withWarnings(() => floating({ width: Object.create(null) }))
    assert.match(result.css, /width:min\(600px,/)
    assert.equal(warnings.length, 1)
    assert.match(warnings[0], /^\[bow-embed\] ignoring width /)
  }
  // A host console.warn that throws does not break the mount.
  {
    const original = console.warn
    console.warn = () => { throw new Error('the host console is broken') }
    try {
      assert.match(floating({ width: 'bad', zIndex: 'bad', accentColor: 'bad;' }).css, /width:min\(600px,/)
      assert.equal(inline(-5), '100%')
    } finally {
      console.warn = original
    }
  }

}
console.log('PASS: z-index takes integer strings, inline height refusals warn, dynamic units fall back to vh, warnings never break a mount')

// ── accentColor is whatever colour the browser accepts, inline heights below
// 1px are refused, and z-index takes CSS's whole range ─────────────────────
{
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
  // The launcher's style acts as a browser's does for background-color: a value
  // it cannot read as a colour is dropped and the old value stays.
  const INVALID = ['blu', 'rgb()', 'rgb(,,,)', 'red;x:y', 'url(x)', 'rgb(1,2,3);x:y']
  const browserLauncher = (name) => {
    if (name !== 'launcher') return null
    let color = ''
    return {
      cssText: '',
      get backgroundColor() { return color },
      set backgroundColor(v) { if (!INVALID.includes(v)) color = String(v) },
    }
  }
  const launcherOf = (options) => {
    const { body } = mountWith({ container: undefined, mode: 'floating', ...options }, { styleFor: browserLauncher })
    return body.children.find((n) => n.name === 'launcher').style
  }

  // Any colour the browser accepts is kept, including the forms a shape check missed.
  for (const good of ['#abc', '#abcd', '#A1b2C3', '#11223344', 'rgb(1, 2, 3)', 'hsl(220deg 80% 50%)',
    'oklch(60% 0.2 250)', 'var(--brand)', 'rebeccapurple', 'transparent']) {
    const { result, warnings } = withWarnings(() => launcherOf({ accentColor: good }))
    assert.equal(result.backgroundColor, good, `keeps accentColor ${good}`)
    assert.deepEqual(warnings, [], `no warning for accentColor ${good}`)
  }
  assert.equal(withWarnings(() => launcherOf({ accentColor: '  #abc ' })).result.backgroundColor, '#abc', 'spaces around are trimmed')
  // A colour the browser drops, an empty string, or a value that is not a string warns once and is the default.
  for (const bad of [...INVALID, '', 12, {}]) {
    const { result, warnings } = withWarnings(() => launcherOf({ accentColor: bad }))
    assert.equal(result.backgroundColor, '#2563eb', `refuses accentColor ${String(bad)}`)
    assert.doesNotMatch(result.cssText, /x:y|url\(|blu|rgb\(/, 'the value never reaches the style text')
    assert.equal(warnings.length, 1, `warns once for accentColor ${String(bad)}`)
    assert.match(warnings[0], /^\[bow-embed\] ignoring accentColor /)
  }
  // Absent: the default, without a word.
  for (const absent of [undefined, null]) {
    const { result, warnings } = withWarnings(() => launcherOf({ accentColor: absent }))
    assert.equal(result.backgroundColor, '#2563eb')
    assert.deepEqual(warnings, [])
  }
  // The style text sets no background, so it can never override the colour set above.
  assert.doesNotMatch(launcherOf({ accentColor: '#abc' }).cssText, /background/)

  // Inline height: a string the browser reads back as less than 1px gives an
  // invisible frame, so it is refused like the number 0.
  const inline = (height) => mountWith({ height }).embed.iframe.parentNode.style.height
  for (const tiny of ['0px', '0.5px', '0PX', ' 0px ']) {
    const { result, warnings } = withWarnings(() => inline(tiny))
    assert.equal(result, '100%', `refuses inline height ${JSON.stringify(tiny)}`)
    assert.equal(warnings.length, 1)
    assert.match(warnings[0], /^\[bow-embed\] ignoring height /)
  }
  // A browser reads '0' back as '0px': that is refused too.
  {
    const zeroAsPx = (name) => {
      if (name !== 'wrapper') return null
      let height = ''
      return { get height() { return height }, set height(v) { height = v === '0' ? '0px' : String(v) } }
    }
    const { result, warnings } = withWarnings(() => mountWith({ height: '0' }, { styleFor: zeroAsPx }).embed.iframe.parentNode.style.height)
    assert.equal(result, '100%')
    assert.equal(warnings.length, 1)
  }
  for (const kept of ['1px', '240px', 'calc(100vh - 80px)', 'clamp(0px, 50vh, 800px)', 'var(--h)', '50%']) {
    const { result, warnings } = withWarnings(() => inline(kept))
    assert.equal(result, kept, `keeps inline height ${kept}`)
    assert.deepEqual(warnings, [])
  }

  // z-index: CSS's whole integer range, from -2147483648 to 2147483647.
  const zOf = (zIndex) => mountWith({ container: undefined, mode: 'floating', zIndex }).embed.iframe.parentNode.style.cssText
  for (const edge of [-2147483648, '-2147483648', 2147483647]) {
    const { result, warnings } = withWarnings(() => zOf(edge))
    assert.match(result, new RegExp(`z-index:${String(edge).trim()}(;|$)`), `keeps zIndex ${edge}`)
    assert.deepEqual(warnings, [])
  }
  for (const out of [-2147483649, '-2147483649']) {
    const { result, warnings } = withWarnings(() => zOf(out))
    assert.match(result, /z-index:2147483000(;|$)/, `refuses zIndex ${out}`)
    assert.equal(warnings.length, 1)
  }
}
console.log('PASS: accentColor is any colour the browser accepts (else a warning and the default), inline heights below 1px are refused, z-index takes its full range')

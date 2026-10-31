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

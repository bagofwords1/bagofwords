// The popup SSO handoff for a framed BOW (utils/ssoPopup.ts): what the frame
// opens, what the popup accepts as its ask, which signals the frame takes, and
// the hashing that ties the frame's verifier to the server's stored challenge.
import assert from 'node:assert/strict'
import { createHash, randomBytes } from 'node:crypto'
import { readFileSync } from 'node:fs'

import * as ssoPopup from '../../utils/ssoPopup.ts'

const {
  ssoPopupUrl, readPopupRequest, resultFromQuery, resultUrl, isValidPopupState, isValidChallenge,
  randomHex, sha256Hex, signalMessage, parseSignalMessage, ackMessage, isAckMessage, frameTookHandoff,
} = ssoPopup

// ── the verifier/challenge pair is what the backend will compare ───────────
// login_exchange_service._hash is sha256(code).hexdigest(): the frame's
// challenge must be exactly that of its verifier, for every input length that
// crosses a SHA-256 block boundary.
const nodeSha = (text) => createHash('sha256').update(text, 'utf8').digest('hex')
for (const length of [0, 1, 3, 55, 56, 57, 63, 64, 65, 119, 120, 128, 1000]) {
  const text = randomBytes(length).toString('hex').slice(0, length)
  assert.equal(sha256Hex(text), nodeSha(text), `sha256 of ${length} chars`)
}
assert.equal(sha256Hex('abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
assert.equal(sha256Hex('héllo ✓ שלום'), nodeSha('héllo ✓ שלום'))
console.log('PASS: the challenge is the SHA-256 the server stores')

// ── state and verifier need no secure context ──────────────────────────────
// crypto.randomUUID and crypto.subtle are missing on a plain-HTTP origin; the
// popup must still open there.
const source = readFileSync(new URL('../../utils/ssoPopup.ts', import.meta.url), 'utf8')
const code = source.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, '')
assert.equal(/randomUUID|crypto\.subtle/.test(code), false, 'no secure-context-only crypto')
const realCrypto = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
const secure = globalThis.crypto
const insecure = { getRandomValues: (array) => secure.getRandomValues(array) }
Object.defineProperty(globalThis, 'crypto', { value: insecure, configurable: true })
try {
  const state = randomHex(16)
  const verifier = randomHex(32)
  assert.equal(isValidPopupState(state), true)
  assert.match(verifier, /^[a-f0-9]{64}$/)
  assert.notEqual(randomHex(16), state)
  assert.equal(isValidChallenge(sha256Hex(verifier)), true)
  // The verifier is what the frame presents as its login code.
  assert.deepEqual(resultFromQuery({ login_code: verifier }), { login_code: verifier })
} finally {
  Object.defineProperty(globalThis, 'crypto', realCrypto)
}
console.log('PASS: state, verifier and challenge are made without a secure context')

const state = randomHex(16)
const challenge = sha256Hex(randomHex(32))

// ── the popup's URL round-trips through its own reader ─────────────────────
const url = new URL(ssoPopupUrl('entra', state, challenge, 'ada@example.com'), 'https://bow.test')
assert.equal(url.pathname, '/users/sign-in')
const query = Object.fromEntries(url.searchParams)
assert.deepEqual(readPopupRequest(query), { state, challenge, provider: 'entra' })
assert.equal(query.login_hint, 'ada@example.com')
assert.equal(new URL(ssoPopupUrl('google', state, challenge), 'https://bow.test').searchParams.has('login_hint'), false)
// The frame's redirect rides along, so the popup's sign-in reads the same
// target (an embed sign-in window's host application binds the new account's role).
const withRedirect = new URL(ssoPopupUrl('entra', state, challenge, '', '/embed-signin?host=host_0123abcd&state=x'), 'https://bow.test')
assert.equal(withRedirect.searchParams.get('redirect'), '/embed-signin?host=host_0123abcd&state=x')
assert.deepEqual(readPopupRequest(Object.fromEntries(withRedirect.searchParams)), { state, challenge, provider: 'entra' })
assert.equal(new URL(ssoPopupUrl('entra', state, challenge), 'https://bow.test').searchParams.has('redirect'), false)
// Not an ask: a missing or malformed state, challenge or provider.
assert.equal(readPopupRequest({ challenge, provider: 'entra' }), null)
assert.equal(readPopupRequest({ sso_popup: 'short', challenge, provider: 'entra' }), null)
assert.equal(readPopupRequest({ sso_popup: state, provider: 'entra' }), null)
assert.equal(readPopupRequest({ sso_popup: state, challenge: challenge.toUpperCase(), provider: 'entra' }), null)
assert.equal(readPopupRequest({ sso_popup: state, challenge: challenge.slice(1), provider: 'entra' }), null)
assert.equal(readPopupRequest({ sso_popup: state, challenge, provider: '../x' }), null)
assert.equal(readPopupRequest({ sso_popup: [state], challenge, provider: 'entra' }), null)
assert.equal(isValidPopupState(null), false)
console.log('PASS: the popup URL carries the provider, the state and the challenge')

// ── what the provider round trip leaves behind ─────────────────────────────
assert.deepEqual(resultFromQuery({ login_code: 'abcDEF123_-xyz' }), { login_code: 'abcDEF123_-xyz' })
assert.deepEqual(resultFromQuery({ error_code: 'oauth_login_failed' }), { error_code: 'oauth_login_failed' })
// A code wins over an error; free text and markup are never passed on.
assert.deepEqual(resultFromQuery({ login_code: 'abcDEF123_-xyz', error_code: 'x' }), { login_code: 'abcDEF123_-xyz' })
assert.equal(resultFromQuery({ error_code: 'Something <b>bad</b>' }), null)
assert.equal(resultFromQuery({ login_code: 'a b' }), null)
assert.equal(resultFromQuery({}), null)
console.log('PASS: only a login code or a fixed error code is a result')

// ── the frame only accepts its own attempt's signals ───────────────────────
const other = randomHex(16)
assert.deepEqual(parseSignalMessage(signalMessage(state, { ready: true }), state), { ready: true })
assert.deepEqual(parseSignalMessage(signalMessage(state, { error_code: 'invitation_required' }), state),
  { error_code: 'invitation_required' })
assert.equal(parseSignalMessage(signalMessage(state, { ready: true }), other), null)
assert.equal(parseSignalMessage({ ...signalMessage(state, { ready: true }), type: 'bow:sso-code' }, state), null)
assert.equal(parseSignalMessage(signalMessage(state, { error_code: '<script>' }), state), null)
assert.equal(parseSignalMessage({ type: 'bow:sso-popup-result', state, ready: 'yes' }, state), null)
assert.equal(parseSignalMessage(null, state), null)
assert.equal(parseSignalMessage('bow:sso-popup-result', state), null)
// A signal never carries a credential: there is nothing in it to steal.
assert.deepEqual(Object.keys(signalMessage(state, { ready: true })).sort(), ['ready', 'state', 'type'])
// The acknowledgement is a different message, and bound to the attempt too.
assert.equal(isAckMessage(ackMessage(state), state), true)
assert.equal(isAckMessage(ackMessage(state), other), false)
assert.equal(isAckMessage(signalMessage(state, { ready: true }), state), false)
assert.equal(parseSignalMessage(ackMessage(state), state), null)
console.log('PASS: signals and acknowledgements are bound to the waiting attempt')

// ── when the popup may close ───────────────────────────────────────────────
assert.equal(frameTookHandoff(null, 'consumed'), true)
assert.equal(frameTookHandoff('ready', 'consumed'), true)
// The server sweeps consumed handoffs: ready, then gone, was taken.
assert.equal(frameTookHandoff('ready', 'pending'), true)
// Not yet: still waiting, never seen, or the server could not be asked.
assert.equal(frameTookHandoff(null, 'ready'), false)
assert.equal(frameTookHandoff('ready', 'ready'), false)
assert.equal(frameTookHandoff(null, 'pending'), false)
assert.equal(frameTookHandoff('ready', null), false)
console.log('PASS: the popup closes only after the frame took the handoff')

// ── where the frame goes next ──────────────────────────────────────────────
assert.equal(resultUrl({ login_code: 'abcDEF123_-xyz' }, '/reports/1'), '/users/sign-in?login_code=abcDEF123_-xyz')
assert.equal(resultUrl({ error_code: 'invitation_required' }, '/reports/1'),
  '/users/sign-in?error_code=invitation_required&redirect=%2Freports%2F1')
assert.equal(resultUrl({ error_code: 'invitation_required' }), '/users/sign-in?error_code=invitation_required')
console.log('PASS: the sign-in page finishes a code and reports an error')

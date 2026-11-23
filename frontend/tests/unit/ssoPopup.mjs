// The popup SSO handoff for a framed BOW (utils/ssoPopup.ts): what the frame
// opens, what the popup accepts as its ask, and which results the frame takes.
import assert from 'node:assert/strict'

import * as ssoPopup from '../../utils/ssoPopup.ts'

const { ssoPopupUrl, readPopupRequest, resultFromQuery, resultMessage, parseResultMessage, resultUrl, isValidPopupState } = ssoPopup

const state = '0b0e6a52-6a3f-4a43-9d0c-2f0f8f1c9a11'

// ── the popup's URL round-trips through its own reader ─────────────────────
const url = new URL(ssoPopupUrl('entra', state, 'ada@example.com'), 'https://bow.test')
assert.equal(url.pathname, '/users/sign-in')
const query = Object.fromEntries(url.searchParams)
assert.deepEqual(readPopupRequest(query), { state, provider: 'entra' })
assert.equal(query.login_hint, 'ada@example.com')
assert.equal(new URL(ssoPopupUrl('google', state), 'https://bow.test').searchParams.has('login_hint'), false)
// Not an ask: no state, a state that is not ours, a provider that is not a slug.
assert.equal(readPopupRequest({ provider: 'entra' }), null)
assert.equal(readPopupRequest({ sso_popup: 'short', provider: 'entra' }), null)
assert.equal(readPopupRequest({ sso_popup: state, provider: '../x' }), null)
assert.equal(readPopupRequest({ sso_popup: [state], provider: 'entra' }), null)
assert.equal(isValidPopupState(null), false)
console.log('PASS: the popup URL carries the provider and the attempt state')

// ── what the provider round trip leaves behind ─────────────────────────────
assert.deepEqual(resultFromQuery({ login_code: 'abcDEF123_-xyz' }), { login_code: 'abcDEF123_-xyz' })
assert.deepEqual(resultFromQuery({ error_code: 'oauth_login_failed' }), { error_code: 'oauth_login_failed' })
// A code wins over an error; free text and markup are never passed on.
assert.deepEqual(resultFromQuery({ login_code: 'abcDEF123_-xyz', error_code: 'x' }), { login_code: 'abcDEF123_-xyz' })
assert.equal(resultFromQuery({ error_code: 'Something <b>bad</b>' }), null)
assert.equal(resultFromQuery({ login_code: 'a b' }), null)
assert.equal(resultFromQuery({}), null)
console.log('PASS: only a login code or a fixed error code is a result')

// ── the frame only accepts its own attempt's message ───────────────────────
const message = resultMessage(state, { login_code: 'abcDEF123_-xyz' })
assert.deepEqual(parseResultMessage(message, state), { login_code: 'abcDEF123_-xyz' })
assert.equal(parseResultMessage(message, 'another-attempt-state-0000'), null)
assert.equal(parseResultMessage({ ...message, type: 'bow:sso-code' }, state), null)
assert.equal(parseResultMessage({ ...message, login_code: '<script>' }, state), null)
assert.equal(parseResultMessage(null, state), null)
assert.equal(parseResultMessage('bow:sso-popup-result', state), null)
console.log('PASS: a result is accepted only with the waiting attempt\'s state')

// ── where the frame goes next ──────────────────────────────────────────────
assert.equal(resultUrl({ login_code: 'abcDEF123_-xyz' }, '/reports/1'), '/users/sign-in?login_code=abcDEF123_-xyz')
assert.equal(resultUrl({ error_code: 'invitation_required' }, '/reports/1'),
  '/users/sign-in?error_code=invitation_required&redirect=%2Freports%2F1')
assert.equal(resultUrl({ error_code: 'invitation_required' }), '/users/sign-in?error_code=invitation_required')
console.log('PASS: the sign-in page finishes a code and reports an error')

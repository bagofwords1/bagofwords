// A sign-up or sign-in inside a host application's sign-in window is sent
// with that application's intent, or not at all (utils/embedSignupIntent.ts).
// The regression this pins: when the intent request failed (rate limit,
// network) the form used to submit the registration bare, creating an
// ordinary workspace member where the host application says app user.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { EmbedIntentError, embedHostOfRedirect, embedSignupHeaders, sendWithEmbedIntent } from '../../utils/embedSignupIntent.ts'

const HOST = 'host_0939cf2cddc6f8481b2e'
const IN_WINDOW = `/embed-signin?host=${HOST}&challenge=abc&state=xyz`

test('the host application is read from the redirect back to the sign-in window', () => {
  assert.equal(embedHostOfRedirect(IN_WINDOW), HOST)
  assert.equal(embedHostOfRedirect(`/embed-signin?state=1&host=${HOST}`), HOST)
  for (const outside of [undefined, null, '', '/', '/reports/1', '/embed-signin', '/embed-signin?host=nope',
    `/other?host=${HOST}`, `https://evil.example/embed-signin?host=${HOST}`, ['/embed-signin?host=' + HOST]]) {
    assert.equal(embedHostOfRedirect(outside), null, String(outside))
  }
})

test('outside a window the request is sent as it always was, and nothing is asked', async () => {
  let asked = 0
  const sent = await sendWithEmbedIntent('/reports/1', async () => { asked++; return { token: 't' } }, async h => h)
  assert.deepEqual(sent, {})
  assert.equal(asked, 0)
})

test('inside a window the request carries the intent it just asked for', async () => {
  const urls = []
  const sent = await sendWithEmbedIntent(IN_WINDOW, async (url) => { urls.push(url); return { token: 'tok-1' } }, async h => h)
  assert.deepEqual(sent, { 'X-Embed-Signup': 'tok-1' })
  assert.deepEqual(urls, [`/api/embed/${HOST}/sso/intent`])
})

for (const [name, post] of [
  ['rate limited (429)', async () => { throw Object.assign(new Error('429'), { status: 429 }) }],
  ['a network error', async () => { throw new TypeError('Failed to fetch') }],
  ['a disabled application (403)', async () => { throw Object.assign(new Error('403'), { status: 403 }) }],
  ['an answer without a token', async () => ({})],
  ['an empty answer', async () => null],
]) {
  test(`a failed intent request prevents the registration: ${name}`, async () => {
    let registrations = 0
    await assert.rejects(
      sendWithEmbedIntent(IN_WINDOW, post, async () => { registrations++; return 'registered' }),
      EmbedIntentError,
    )
    assert.equal(registrations, 0, 'the registration request must not be sent')
    await assert.rejects(embedSignupHeaders(IN_WINDOW, post), EmbedIntentError)
  })
}

test('trying again after a failure sends it once the intent is had', async () => {
  let attempts = 0, registrations = 0
  const post = async () => { if (++attempts === 1) throw new Error('429'); return { token: 'tok-2' } }
  const send = async (headers) => { registrations++; return headers }
  await assert.rejects(sendWithEmbedIntent(IN_WINDOW, post, send), EmbedIntentError)
  assert.deepEqual(await sendWithEmbedIntent(IN_WINDOW, post, send), { 'X-Embed-Signup': 'tok-2' })
  assert.equal(registrations, 1)
})

test('an error from the registration itself is not mistaken for a missing intent', async () => {
  const failure = new Error('REGISTER_USER_ALREADY_EXISTS')
  await assert.rejects(sendWithEmbedIntent(IN_WINDOW, async () => ({ token: 't' }), async () => { throw failure }),
    error => error === failure)
})

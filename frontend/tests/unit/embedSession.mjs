// Embedded-artifact session lifecycle (utils/embedSession.ts): expiry does not
// depend on the browser's timezone, a failed renewal keeps a live session, and
// a renewed token for the same person keeps the runtime connected.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import ts from 'typescript'
import { computed, ref } from 'vue'

import * as embedSession from '../../utils/embedSession.ts'

const { runtimeIdentity, renewalDelayMs, renewalFailure, renewalRetry, sessionLifetimeMs } = embedSession

// East of UTC, where a bare (offset-less) expiry read as local time lies in the past.
process.env.TZ = 'Asia/Jerusalem'
const now = Date.parse('2026-10-29T15:00:00Z')
const HOUR = 3_600_000

// ── how long the session lasts ────────────────────────────────────────────
// expires_in is relative: neither the browser's clock nor its timezone matters.
assert.equal(sessionLifetimeMs({ expires_in: 3600, expires_at: '2020-01-01T00:00:00Z' }, now), HOUR)
assert.equal(sessionLifetimeMs({ expires_in: 90 }, now), 90_000)
// Without it, the instant: with its Z, and a bare one is the server's UTC too.
assert.equal(sessionLifetimeMs({ expires_at: '2026-10-29T16:00:00Z' }, now), HOUR)
assert.equal(sessionLifetimeMs({ expires_at: '2026-10-29T18:00:00+02:00' }, now), HOUR)
assert.equal(sessionLifetimeMs({ expires_at: '2026-10-29T16:00:00.123456' }, now), HOUR + 123)
// Unreadable or past: nothing left.
assert.equal(sessionLifetimeMs({ expires_at: 'soon' }, now), 0)
assert.equal(sessionLifetimeMs({ expires_in: -5 }, now), 0)
console.log('PASS: the session lifetime does not depend on the browser timezone')

// ── when to renew ─────────────────────────────────────────────────────────
// Five minutes ahead of an hour's session; halfway through a short one; never a hot loop.
assert.equal(renewalDelayMs(HOUR), 55 * 60_000)
assert.equal(renewalDelayMs(6 * 60_000), 3 * 60_000)
assert.equal(renewalDelayMs(60_000), 30_000)
assert.equal(renewalDelayMs(0), 15_000)
assert.equal(renewalDelayMs(Number.NaN), 15_000)
console.log('PASS: renewal is scheduled ahead of the real expiry')

// ── a failed renewal ──────────────────────────────────────────────────────
// Quiet retries with backoff while the session is still good...
assert.deepEqual([0, 1, 2, 3, 4, 9].map((n) => renewalRetry(n, HOUR)), [15_000, 30_000, 60_000, 120_000, 120_000, 120_000].map((delay) => ({ expire: false, delay })))
// ...the last one early enough to finish before the end...
assert.deepEqual(renewalRetry(3, 100_000), { expire: false, delay: 70_000 })
// ...then the session ends when it really expires, not before.
assert.deepEqual(renewalRetry(0, 20_000), { expire: true, delay: 20_000 })
assert.deepEqual(renewalRetry(0, -1), { expire: true, delay: 0 })
console.log('PASS: a failed renewal retries with backoff until the session really expires')

// What the gate does about it (EmbeddedArtifactGate.vue, signIn's catch).
const failed = (over) => renewalFailure({ hasCredential: true, errorCode: 'embed.host_unreachable', failures: 0, remainingMs: HOUR, ...over })
// No session to keep (the first sign-in): the gate fails, with a retry button.
assert.deepEqual(failed({ hasCredential: false }), { kind: 'fail', retryable: true })
// Someone BOW does not know: a retry cannot help, even with a live session.
for (const errorCode of ['embed.identity_unmapped', 'embed.identity_ambiguous', 'embed.user_inactive']) {
  assert.deepEqual(failed({ errorCode }), { kind: 'fail', retryable: false })
  assert.deepEqual(failed({ errorCode, hasCredential: false }), { kind: 'fail', retryable: false })
}
// A transient error (a 503, the host not answering, no error code at all)
// keeps the live session and retries with backoff...
assert.deepEqual([0, 1, 2, 3, 4].map((failures) => failed({ failures })), [15_000, 30_000, 60_000, 120_000, 120_000].map((delay) => ({ kind: 'retry', delay })))
assert.deepEqual(failed({ errorCode: undefined }), { kind: 'retry', delay: 15_000 })
// ...the last retry early enough to finish, then the session ends when it really expires.
assert.deepEqual(failed({ failures: 3, remainingMs: 100_000 }), { kind: 'retry', delay: 70_000 })
assert.deepEqual(failed({ failures: 4, remainingMs: 25_000 }), { kind: 'expire', delay: 25_000 })
console.log('PASS: a failed renewal keeps a live session unless the error is permanent')

// ── whose runtime ports these are ─────────────────────────────────────────
const alice = { token: 'bow_embed_a1', organizationId: 'org', hostApplication: 'host_1', viewer: { id: 'u-alice' } }
const renewed = { ...alice, token: 'bow_embed_a2' }
const bob = { ...alice, token: 'bow_embed_b1', viewer: { id: 'u-bob' } }
assert.equal(runtimeIdentity(renewed, 'app-token'), runtimeIdentity(alice, 'app-token'))
assert.notEqual(runtimeIdentity(bob, ''), runtimeIdentity(alice, ''))
assert.notEqual(runtimeIdentity({ ...alice, hostApplication: 'host_2' }, ''), runtimeIdentity(alice, ''))
assert.notEqual(runtimeIdentity(null, ''), runtimeIdentity(alice, ''))
// The credential's userId names the person too (renewals keep it).
const byId = { ...alice, viewer: null, userId: 'u-alice' }
assert.equal(runtimeIdentity({ ...byId, token: 'bow_embed_a2' }, null), runtimeIdentity(byId, null))
assert.notEqual(runtimeIdentity({ ...byId, userId: 'u-bob' }, null), runtimeIdentity(byId, null))
// Not knowing who it is never shares: each token is someone new.
const unknown = { ...alice, viewer: null }
assert.notEqual(runtimeIdentity({ ...unknown, token: 'bow_embed_x2' }, ''), runtimeIdentity(unknown, ''))
assert.equal(runtimeIdentity(unknown, ''), 'embed-token:bow_embed_a1')
// The app session (no embed credential) is keyed by being signed in at all:
// its token renewals keep the ports; signing out changes it.
assert.equal(runtimeIdentity(null, 'Bearer one'), runtimeIdentity(null, 'Bearer two'))
assert.equal(runtimeIdentity(null, 'Bearer one'), 'app:signed-in')
assert.notEqual(runtimeIdentity(null, 'Bearer one'), runtimeIdentity(null, ''))
assert.notEqual(runtimeIdentity(null, 'Bearer one'), runtimeIdentity(null, null))
console.log('PASS: a renewed token keeps the viewer; a different viewer is a different identity')

// The composable's own watch: it disposes the ports when its source changes.
// Run the real useArtifactRuntime with Nuxt's auto-imports stubbed, and read
// what it watches (a reverted `watch(token, dispose)` changes on every renewal).
const runtimeSource = readFileSync(new URL('../../composables/useArtifactRuntime.ts', import.meta.url), 'utf8')
const runtimeModule = ts.transpileModule(runtimeSource, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
function mountRuntime(embed, appToken) {
  const watched = []
  const stubs = {
    require: (id) => { assert.equal(id, '~/utils/embedSession'); return embedSession },
    useEmbedCredential: () => embed,
    useAuth: () => ({ token: appToken }),
    computed,
    useApiAuthorization: () => computed(() => (embed.value ? `Bearer ${embed.value.token}` : (appToken.value || ''))),
    useErrorMessage: () => ({ getErrorMessage: () => '' }),
    onMounted: () => {},
    onUnmounted: () => {},
    watch: (source, callback) => watched.push({ source, callback }),
  }
  const exports = {}
  new Function('exports', ...Object.keys(stubs), runtimeModule)(exports, ...Object.values(stubs))
  const { dispose } = exports.useArtifactRuntime(() => ({ frames: [] }))
  const disposing = watched.filter((w) => w.callback === dispose)
  assert.equal(disposing.length, 1, 'the runtime disposes its ports on one watched source')
  const { source } = disposing[0]
  return () => (typeof source === 'function' ? source() : source.value)
}
{
  const embed = ref(alice), appToken = ref('Bearer app-1')
  const key = mountRuntime(embed, appToken)
  const before = key()
  embed.value = renewed
  assert.equal(key(), before, 'a renewed token for the same viewer keeps the ports')
  embed.value = bob
  assert.notEqual(key(), before, 'another viewer disposes them')
  embed.value = null
  assert.notEqual(key(), before, 'signing out disposes them')
  const app = key()
  appToken.value = 'Bearer app-2'
  assert.equal(key(), app, 'a renewed app session token keeps them')
  appToken.value = ''
  assert.notEqual(key(), app, 'signing out of the app disposes them')
}
{
  const embed = ref(unknown), appToken = ref('')
  const key = mountRuntime(embed, appToken)
  const before = key()
  embed.value = { ...unknown, token: 'bow_embed_x2' }
  assert.notEqual(key(), before, 'a credential without a user id never keeps the ports')
}
console.log('PASS: useArtifactRuntime keeps its ports across a renewal for the same viewer only')

// ── an artifact the embed cannot open ─────────────────────────────────────
// What GET /api/d/{ref} answered, mapped to what the gate tells the viewer.
// Only editors and admins are told "not published" (the backend decides that);
// the host application missing from the artifact's embed settings is its own
// reason, not "ask its owner to share it"; any 401 means the session is dead.
{
  const { embedLoadFailure, EMBED_UNAVAILABLE_MESSAGE_KEYS } = embedSession
  assert.equal(typeof embedLoadFailure, 'function', 'embedLoadFailure is exported')
  assert.equal(embedLoadFailure(404, 'dashboard.not_published'), 'not_published')
  for (const args of [[404, 'artifact.not_found'], [404], [410], [400, 'validation']]) {
    assert.equal(embedLoadFailure(...args), 'not_found', JSON.stringify(args))
  }
  // The server broke, or no answer came (network, a thrown load, an empty
  // body): not "it does not exist" -- a retryable failure.
  for (const args of [[500], [502, 'internal_error'], [503], [undefined, null], [undefined], [0]]) {
    assert.equal(embedLoadFailure(...args), 'failed', JSON.stringify(args))
  }
  assert.equal(embedLoadFailure(403, 'embed.not_allowed'), 'not_allowed')
  assert.equal(embedLoadFailure(403, 'access_denied'), 'denied')
  assert.equal(embedLoadFailure(403), 'denied')
  assert.equal(embedLoadFailure(401, 'embed.session_invalid'), 'session_invalid')
  assert.equal(embedLoadFailure(401), 'session_invalid')
  assert.equal(embedLoadFailure(undefined, 'embed.session_invalid'), 'session_invalid')
  assert.equal(embedLoadFailure(500, 'embed.session_invalid'), 'session_invalid')

  // A later request of the view (content, queries) ending the same way.
  const { embedSessionEnded } = embedSession
  assert.equal(typeof embedSessionEnded, 'function', 'embedSessionEnded is exported')
  assert.equal(embedSessionEnded(401), true)
  assert.equal(embedSessionEnded(403, 'embed.session_invalid'), true)
  for (const args of [[403], [404], [500], [undefined, null]]) {
    assert.equal(embedSessionEnded(...args), false, JSON.stringify(args))
  }

  // Each reason the gate shows has a message in every catalog.
  assert.deepEqual(Object.keys(EMBED_UNAVAILABLE_MESSAGE_KEYS).sort(), ['denied', 'failed', 'not_allowed', 'not_found', 'not_published'])
  const localesDir = new URL('../../../locales/', import.meta.url)
  const catalogs = ['en', 'es', 'he', 'fr', 'sv', 'ar', 'ru', 'de', 'pt', 'it']
  for (const locale of catalogs) {
    const catalog = JSON.parse(readFileSync(new URL(`${locale}.json`, localesDir), 'utf8'))
    for (const key of Object.values(EMBED_UNAVAILABLE_MESSAGE_KEYS)) {
      const text = key.split('.').reduce((node, part) => (node == null ? undefined : node[part]), catalog)
      assert.ok(typeof text === 'string' && text.trim(), `${locale}: ${key}`)
    }
  }
}
console.log('PASS: an embed that cannot open its artifact says why, in every catalog')

// ── the unavailable message is never a raw key (finding 15) ───────────────
// The gate shows the catalog text and tells it to the host (bow:unavailable).
// When the catalog it was served lacks the key (a stale catalog: t() answers
// the key itself, or nothing), it says the built-in English text instead.
{
  const { embedUnavailableMessage, EMBED_UNAVAILABLE_FALLBACK_MESSAGES, EMBED_UNAVAILABLE_MESSAGE_KEYS } = embedSession
  assert.equal(typeof embedUnavailableMessage, 'function', 'embedUnavailableMessage is exported')
  const reasons = Object.keys(EMBED_UNAVAILABLE_MESSAGE_KEYS)
  assert.deepEqual(Object.keys(EMBED_UNAVAILABLE_FALLBACK_MESSAGES || {}).sort(), [...reasons].sort(), 'a fallback for every reason')

  const en = JSON.parse(readFileSync(new URL('../../../locales/en.json', import.meta.url), 'utf8'))
  for (const reason of reasons) {
    const key = EMBED_UNAVAILABLE_MESSAGE_KEYS[reason]
    const enText = key.split('.').reduce((node, part) => (node == null ? undefined : node[part]), en)
    // Parity: the built-in text is the English catalog's, word for word.
    assert.equal(EMBED_UNAVAILABLE_FALLBACK_MESSAGES[reason], enText, `fallback = en.json for ${reason}`)
    // The catalog answers: its text.
    assert.equal(embedUnavailableMessage(reason, (k) => (k === key ? `catalog:${k}` : k)), `catalog:${key}`)
    // The catalog lacks it (the key comes back, or nothing): the fallback.
    assert.equal(embedUnavailableMessage(reason, (k) => k), enText, `${reason}: raw key replaced`)
    assert.equal(embedUnavailableMessage(reason, () => ''), enText, `${reason}: empty text replaced`)
  }
}
console.log('PASS: an unavailable message is the catalog text, else the built-in English, never a raw key')

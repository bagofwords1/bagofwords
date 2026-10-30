import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { mock } from 'node:test'

import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import { ref } from 'vue'

import * as embedSession from '../../utils/embedSession.ts'

// Run: node --test tests/unit/embeddedArtifactGate.mjs
// The real EmbeddedArtifactGate.vue setup, with Nuxt's auto-imports and the
// host stubbed, on mock timers: a failed renewal keeps the live session and
// retries; a permanent identity error ends it. (The rule itself, renewalFailure,
// is tested on its own in embedSession.mjs; this is the gate acting on it.)

const file = new URL('../../components/dashboard/EmbeddedArtifactGate.vue', import.meta.url)
const { descriptor } = parse(readFileSync(file, 'utf8'), { filename: 'EmbeddedArtifactGate.vue' })
const script = compileScript(descriptor, { id: 'embedded-artifact-gate', inlineTemplate: false }).content
const compiled = ts.transpileModule(script, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText

// The stub t() knows no key (it answers the key itself, as a stale catalog
// would): the viewer and the host get the built-in English text, never the key.
const NOT_PUBLISHED = embedSession.EMBED_UNAVAILABLE_FALLBACK_MESSAGES?.not_published ?? 'missing fallback: not_published'
const FAILED = embedSession.EMBED_UNAVAILABLE_FALLBACK_MESSAGES?.failed ?? 'missing fallback: failed'

const SESSION = { token: 'bow_embed_a1', organization_id: 'org', host_application: 'host_1', viewer: { id: 'u-alice' }, user: { name: 'Alice' }, expires_in: 3600 }
const MINUTE = 60_000

// `credentials` answers each host.requestCredential in turn; `sessions` each
// POST /session ({ ok, status, body }, or a promise of one: an exchange still
// in flight).
function mountGate({ credentials, sessions }) {
  const credential = ref(null)
  const requested = []
  const posted = []
  const deleted = []
  let hostHandlers = null
  let mounted = null
  const stubs = {
    require: (id) => {
      if (id === 'vue') return { defineComponent: (options) => options }
      if (id === '~/utils/embedSession') return embedSession
      if (id === '~/components/Spinner.vue') return { default: {} }
      throw new Error(`unexpected import ${id}`)
    },
    ref,
    nextTick: () => Promise.resolve(),
    useI18n: () => ({ t: (key) => key }),
    useErrorMessage: () => ({ getErrorMessage: (e) => `error:${e?.data?.error_code}` }),
    useRuntimeConfig: () => ({ public: { baseURL: '/api' } }),
    useEmbedCredential: () => credential,
    useNuxtApp: () => ({}),
    useEmbedHost: (_origins, handlers) => {
      hostHandlers = handlers
      return {
        embedded: true,
        start: () => {},
        stop: () => {},
        post: (type, payload) => posted.push({ type, ...payload }),
        requestCredential: (kind) => {
          requested.push(kind)
          const next = credentials.shift()
          return next instanceof Error ? Promise.reject(next) : Promise.resolve(next)
        },
      }
    },
    fetch: async (url, init = {}) => {
      if (url.endsWith('/bootstrap')) return { ok: true, json: async () => ({ enabled: true, identity_mode: 'assertion', allowed_origins: [] }) }
      if (init.method === 'DELETE') { deleted.push(init.headers?.Authorization); return { ok: true, status: 204, json: async () => ({}) } }
      assert.equal(init.method, 'POST')
      const { ok = true, status = 200, body } = await sessions.shift()
      return { ok, status, json: async () => body }
    },
    onMounted: (fn) => { mounted = fn },
    onBeforeUnmount: () => {},
  }
  const exports = {}
  new Function('exports', ...Object.keys(stubs), compiled)(exports, ...Object.values(stubs))
  let exposed = {}
  const bindings = exports.default.setup({ hostApplication: 'host_1' }, { expose: (o) => { exposed = o }, emit: () => {} })
  return { bindings, credential, requested, posted, deleted, exposed: () => exposed || {}, mounted: () => mounted(),
    init: () => hostHandlers.onInit({ context: null }), logout: () => hostHandlers.onLogout() }
}

// Let the gate's awaits (credential, exchange, json) run between timer ticks.
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve)) }

mock.timers.enable({ apis: ['setTimeout', 'Date'], now: Date.parse('2026-10-29T15:00:00Z') })
try {
  const gate = mountGate({
    credentials: ['assertion-1', new Error('the host did not answer'), 'assertion-3'],
    sessions: [
      { body: SESSION },
      { ok: false, status: 403, body: { error_code: 'embed.identity_unmapped' } },
    ],
  })
  await gate.mounted()
  gate.init()
  await settle()
  assert.equal(gate.bindings.status.value, 'ready')
  assert.equal(gate.credential.value?.token, 'bow_embed_a1')

  // The renewal, five minutes before the hour's session ends, fails transiently...
  mock.timers.tick(55 * MINUTE)
  await settle()
  assert.equal(gate.requested.length, 2, 'the renewal asked the host again')
  // ...and the gate keeps the session, and the app, and retries quietly.
  assert.equal(gate.credential.value?.token, 'bow_embed_a1', 'a transient renewal failure keeps the credential')
  assert.equal(gate.bindings.status.value, 'ready', 'a transient renewal failure keeps the app')
  assert.equal(gate.posted.filter((p) => p.type === 'bow:auth' && p.status === 'failed').length, 0, 'nothing failed for the host')
  mock.timers.tick(15_000 - 1)
  await settle()
  assert.equal(gate.requested.length, 2, 'not before the backoff')
  mock.timers.tick(1)
  await settle()
  assert.equal(gate.requested.length, 3, 'a retry is scheduled after the backoff')

  // That retry: BOW no longer knows this user. A retry cannot help: the session ends.
  assert.equal(gate.credential.value, null, 'a permanent identity error clears the credential')
  assert.equal(gate.bindings.status.value, 'failed')
  assert.equal(gate.bindings.retryable.value, false, 'with no retry offered')
  assert.equal(gate.bindings.message.value, 'error:embed.identity_unmapped')
} finally {
  mock.timers.reset()
}
console.log('PASS: the gate keeps a live session through a transient renewal failure, and ends it on a permanent one')

// ── the view could not open the artifact (artifactUnavailable) ────────────
// An unpublished / missing / refused artifact: the gate says why in place and
// tells the host (bow:unavailable), keeping the session. A dead session (401):
// it ends as an expired one does, so the host is asked to sign the user in.
async function readyGate() {
  const gate = mountGate({ credentials: ['assertion-1'], sessions: [{ body: SESSION }] })
  await gate.mounted()
  gate.init()
  await settle()
  assert.equal(gate.bindings.status.value, 'ready')
  return gate
}
// Mock timers: the session's renewal timer (55 min) must not keep the process alive.
mock.timers.enable({ apis: ['setTimeout', 'Date'], now: Date.parse('2026-10-29T15:00:00Z') })
try {
{
  const gate = await readyGate()
  assert.equal(typeof gate.exposed().artifactUnavailable, 'function', 'the gate exposes artifactUnavailable')
  gate.exposed().artifactUnavailable('not_published')
  assert.equal(gate.bindings.status.value, 'unavailable')
  assert.equal(gate.bindings.message.value, NOT_PUBLISHED)
  assert.deepEqual(gate.posted.filter((p) => p.type === 'bow:unavailable'),
    [{ type: 'bow:unavailable', reason: 'not_published', message: NOT_PUBLISHED }])
  assert.equal(gate.credential.value?.token, 'bow_embed_a1', 'the session is kept')
}
{
  const gate = await readyGate()
  gate.exposed().artifactUnavailable('session_invalid')
  assert.equal(gate.credential.value, null, 'a dead session is ended')
  assert.equal(gate.bindings.status.value, 'failed')
  assert.equal(gate.bindings.message.value, 'embed.gate.expired')
  assert.ok(gate.posted.some((p) => p.type === 'bow:auth' && p.status === 'failed'), 'the host is told to sign in again')
  assert.equal(gate.posted.filter((p) => p.type === 'bow:unavailable').length, 0)
}
} finally {
  mock.timers.reset()
}
console.log('PASS: the gate tells the viewer and the host why the artifact is not shown, and ends a dead session')

// ── a renewal while the artifact is unavailable ──────────────────────────
// The renewal is silent, as over a shown artifact: the message stays (never
// "Signing you in…"), and the host is not told "authenticated" then
// "unavailable" again every cycle. It still re-checks (the view remounts),
// and the host hears "authenticated" once the artifact opens.
mock.timers.enable({ apis: ['setTimeout', 'Date'], now: Date.parse('2026-10-29T15:00:00Z') })
try {
  const gate = mountGate({ credentials: ['assertion-1', 'assertion-2', 'assertion-3'],
    sessions: [{ body: SESSION }, { body: SESSION }, { body: SESSION }] })
  await gate.mounted()
  gate.init()
  await settle()
  const authenticated = () => gate.posted.filter((p) => p.type === 'bow:auth' && p.status === 'authenticated').length
  const unavailable = () => gate.posted.filter((p) => p.type === 'bow:unavailable').length
  assert.equal(authenticated(), 1)
  gate.exposed().artifactUnavailable('not_published')
  assert.equal(unavailable(), 1)

  mock.timers.tick(55 * MINUTE)  // the renewal starts
  assert.equal(gate.requested.length, 2, 'the session renews')
  assert.equal(gate.bindings.message.value, NOT_PUBLISHED, 'the renewal is silent')
  assert.equal(gate.bindings.status.value, 'unavailable')
  await settle()
  assert.equal(gate.bindings.status.value, 'ready', 'the renewed session re-checks the artifact (the view remounts)')
  assert.equal(authenticated(), 1, 'the host is not told "authenticated" over an unavailable artifact')
  gate.exposed().artifactUnavailable('not_published')  // still not published
  assert.equal(unavailable(), 1, 'nor "unavailable" again for the same reason')
  assert.equal(gate.bindings.message.value, NOT_PUBLISHED)

  mock.timers.tick(55 * MINUTE)
  await settle()
  assert.equal(typeof gate.exposed().artifactOpened, 'function', 'the gate exposes artifactOpened')
  gate.exposed().artifactOpened()  // published meanwhile: the view opened it
  assert.equal(authenticated(), 2, 'the host hears the artifact is shown')
  gate.exposed().artifactOpened()
  assert.equal(authenticated(), 2, 'once')
  gate.exposed().artifactUnavailable('not_published')  // unpublished again: told again
  assert.equal(unavailable(), 2)
} finally {
  mock.timers.reset()
}
console.log('PASS: a renewal over an unavailable artifact is silent and does not toggle the host')

// ── the server broke: a retryable failure ────────────────────────────────
mock.timers.enable({ apis: ['setTimeout', 'Date'], now: Date.parse('2026-10-29T15:00:00Z') })
try {
  const gate = await readyGate()
  gate.exposed().artifactUnavailable('failed')
  assert.equal(gate.bindings.status.value, 'unavailable')
  assert.equal(gate.bindings.message.value, FAILED)
  assert.deepEqual(gate.posted.filter((p) => p.type === 'bow:unavailable'),
    [{ type: 'bow:unavailable', reason: 'failed', message: FAILED }])
  assert.equal(gate.bindings.unavailableReason.value, 'failed', 'the Try again button shows for it')
  gate.bindings.retryOpen()
  assert.equal(gate.bindings.status.value, 'ready', 'Try again opens the artifact again')
  assert.equal(gate.credential.value?.token, 'bow_embed_a1', 'with the same session')
  gate.exposed().artifactUnavailable('not_found')
  assert.equal(gate.bindings.unavailableReason.value, 'not_found', 'no retry for an artifact that is not there')
} finally {
  mock.timers.reset()
}
console.log('PASS: a load that failed on the server is shown with a retry, and the host hears `failed`')

// ── the host signs the user out while a sign-in is in flight ─────────────
// A renewal (or the first sign-in) is still waiting on its exchange when the
// host says bow:logout. Whatever that exchange brings back afterwards is
// dropped: no session, no "authenticated", no "failed", no renewal. A new
// sign-in the host asks for afterwards (bow:init) works as usual.
function deferred() {
  let resolve
  const promise = new Promise((r) => { resolve = r })
  return { promise, resolve }
}
mock.timers.enable({ apis: ['setTimeout', 'Date'], now: Date.parse('2026-10-29T15:00:00Z') })
try {
  // The first sign-in: the exchange resolves after the logout.
  const pending = deferred()
  const gate = mountGate({ credentials: ['assertion-1', 'assertion-2'], sessions: [pending.promise, { body: SESSION }] })
  await gate.mounted()
  gate.init()
  await settle()
  assert.equal(gate.bindings.status.value, 'signing_in', 'the exchange is still in flight')
  gate.logout()
  assert.equal(gate.bindings.status.value, 'signed_out')
  const postsAtLogout = gate.posted.length
  pending.resolve({ body: SESSION })
  await settle()
  assert.equal(gate.bindings.status.value, 'signed_out', 'a sign-in that resolves after the logout does not sign the user in again')
  assert.equal(gate.credential.value, null, 'and leaves no credential')
  assert.deepEqual(gate.posted.slice(postsAtLogout).filter((p) => p.type === 'bow:auth' && p.status === 'authenticated'), [],
    'and the host hears no "authenticated" after its logout')
  mock.timers.tick(60 * MINUTE)
  await settle()
  assert.equal(gate.requested.length, 1, 'nor is a renewal scheduled for it')

  // The host signs the user in again (bow:init): that sign-in works.
  gate.init()
  await settle()
  assert.equal(gate.bindings.status.value, 'ready', 'a new sign-in after the logout succeeds')
  assert.equal(gate.credential.value?.token, 'bow_embed_a1')
  assert.equal(gate.posted.slice(postsAtLogout).filter((p) => p.type === 'bow:auth' && p.status === 'authenticated').length, 1)
} finally {
  mock.timers.reset()
}
mock.timers.enable({ apis: ['setTimeout', 'Date'], now: Date.parse('2026-10-29T15:00:00Z') })
try {
  // A renewal: its exchange fails (transiently) after the logout.
  const pending = deferred()
  const gate = mountGate({ credentials: ['assertion-1', 'assertion-2'], sessions: [{ body: SESSION }, pending.promise] })
  await gate.mounted()
  gate.init()
  await settle()
  assert.equal(gate.bindings.status.value, 'ready')
  mock.timers.tick(55 * MINUTE)  // the renewal starts
  await settle()
  assert.equal(gate.requested.length, 2, 'the renewal is in flight')
  gate.logout()
  assert.equal(gate.bindings.status.value, 'signed_out')
  assert.deepEqual(gate.deleted, ['Bearer bow_embed_a1'], 'the session is ended server-side')
  const postsAtLogout = gate.posted.length
  pending.resolve({ ok: false, status: 503, body: { error_code: 'server.unavailable' } })
  await settle()
  assert.equal(gate.bindings.status.value, 'signed_out', 'a renewal that fails after the logout does not turn it into a failure')
  assert.equal(gate.credential.value, null)
  assert.deepEqual(gate.posted.slice(postsAtLogout), [], 'the host hears nothing more')
  mock.timers.tick(60 * MINUTE)
  await settle()
  assert.equal(gate.requested.length, 2, 'and no renewal is scheduled')
} finally {
  mock.timers.reset()
}
mock.timers.enable({ apis: ['setTimeout', 'Date'], now: Date.parse('2026-10-29T15:00:00Z') })
try {
  // The host signs the user in again before the dropped exchange has answered:
  // that is a sign-in of its own, not a wait on the dropped one.
  const pending = deferred()
  const gate = mountGate({ credentials: ['assertion-1', 'assertion-2'],
    sessions: [pending.promise, { body: { ...SESSION, token: 'bow_embed_a2' } }] })
  await gate.mounted()
  gate.init()
  await settle()
  gate.logout()
  gate.init()
  await settle()
  assert.equal(gate.requested.length, 2, 'the new sign-in asks the host for a credential of its own')
  assert.equal(gate.bindings.status.value, 'ready')
  assert.equal(gate.credential.value?.token, 'bow_embed_a2')
  pending.resolve({ body: SESSION })
  await settle()
  assert.equal(gate.credential.value?.token, 'bow_embed_a2', 'the dropped exchange does not replace the new session')
} finally {
  mock.timers.reset()
}
console.log('PASS: a sign-in or renewal still in flight at the logout is dropped, and a new sign-in works')

// ── a renewal for the same viewer, with another role ─────────────────────
// The app stays (no remount: the credential is never cleared on the way) and
// the credential carries the renewed viewer, which the view hands to the
// running artifact as current_user (tests/unit/embedViewerRefresh.mjs).
mock.timers.enable({ apis: ['setTimeout', 'Date'], now: Date.parse('2026-10-29T15:00:00Z') })
try {
  const promoted = { ...SESSION, token: 'bow_embed_a2', viewer: { id: 'u-alice', role: 'manager', groups: ['emea'] } }
  const gate = mountGate({ credentials: ['assertion-1', 'assertion-2'], sessions: [{ body: SESSION }, { body: promoted }] })
  await gate.mounted()
  gate.init()
  await settle()
  const seen = []
  const stop = (await import('vue')).watch(gate.credential, (value) => seen.push(value?.token ?? null), { flush: 'sync' })
  mock.timers.tick(55 * MINUTE)
  await settle()
  stop()
  assert.deepEqual(seen, ['bow_embed_a2'], 'the credential is swapped in place, never cleared (no remount)')
  assert.equal(gate.bindings.status.value, 'ready')
  assert.deepEqual(gate.credential.value.viewer, { id: 'u-alice', role: 'manager', groups: ['emea'] })
} finally {
  mock.timers.reset()
}
console.log('PASS: a same-viewer renewal swaps the credential and its viewer in place')

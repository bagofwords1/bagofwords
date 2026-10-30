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

const SESSION = { token: 'bow_embed_a1', organization_id: 'org', host_application: 'host_1', viewer: { id: 'u-alice' }, user: { name: 'Alice' }, expires_in: 3600 }
const MINUTE = 60_000

// `credentials` answers each host.requestCredential in turn; `sessions` each
// POST /session ({ ok, status, body }).
function mountGate({ credentials, sessions }) {
  const credential = ref(null)
  const requested = []
  const posted = []
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
      assert.equal(init.method, 'POST')
      const { ok = true, status = 200, body } = sessions.shift()
      return { ok, status, json: async () => body }
    },
    onMounted: (fn) => { mounted = fn },
    onBeforeUnmount: () => {},
  }
  const exports = {}
  new Function('exports', ...Object.keys(stubs), compiled)(exports, ...Object.values(stubs))
  let exposed = {}
  const bindings = exports.default.setup({ hostApplication: 'host_1' }, { expose: (o) => { exposed = o }, emit: () => {} })
  return { bindings, credential, requested, posted, exposed: () => exposed || {}, mounted: () => mounted(), init: () => hostHandlers.onInit({ context: null }) }
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
  assert.equal(gate.bindings.message.value, 'embed.unavailable.notPublished')
  assert.deepEqual(gate.posted.filter((p) => p.type === 'bow:unavailable'),
    [{ type: 'bow:unavailable', reason: 'not_published', message: 'embed.unavailable.notPublished' }])
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
  assert.equal(gate.bindings.message.value, 'embed.unavailable.notPublished', 'the renewal is silent')
  assert.equal(gate.bindings.status.value, 'unavailable')
  await settle()
  assert.equal(gate.bindings.status.value, 'ready', 'the renewed session re-checks the artifact (the view remounts)')
  assert.equal(authenticated(), 1, 'the host is not told "authenticated" over an unavailable artifact')
  gate.exposed().artifactUnavailable('not_published')  // still not published
  assert.equal(unavailable(), 1, 'nor "unavailable" again for the same reason')
  assert.equal(gate.bindings.message.value, 'embed.unavailable.notPublished')

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
  assert.equal(gate.bindings.message.value, 'embed.unavailable.failed')
  assert.deepEqual(gate.posted.filter((p) => p.type === 'bow:unavailable'),
    [{ type: 'bow:unavailable', reason: 'failed', message: 'embed.unavailable.failed' }])
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

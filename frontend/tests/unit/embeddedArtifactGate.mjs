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
  const bindings = exports.default.setup({ hostApplication: 'host_1' }, { expose: () => {}, emit: () => {} })
  return { bindings, credential, requested, posted, mounted: () => mounted(), init: () => hostHandlers.onInit({ context: null }) }
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

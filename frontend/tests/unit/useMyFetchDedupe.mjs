import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import ts from 'typescript'
import { ref } from 'vue'

import { inflightGetKey } from '../../composables/useMyFetch.ts'

// Run: node --test tests/unit/useMyFetchDedupe.mjs
// Identical GETs in flight share one call (useMyFetch). The call carries the
// sender's Authorization, so only the same sender may share its answer: after
// an embedded artifact switches viewer, the new viewer must not receive the
// previous viewer's response to a GET still in flight.

const alice = 'Bearer bow_embed_alice'
const bob = 'Bearer bow_embed_bob'

// Another sender (viewer, or the app session) never shares.
assert.notEqual(inflightGetKey(bob, 'org', '/reports', null), inflightGetKey(alice, 'org', '/reports', null))
assert.notEqual(inflightGetKey('app-jwt', 'org', '/reports', null), inflightGetKey(alice, 'org', '/reports', null))
// The same sender's identical GETs still share one call...
assert.equal(inflightGetKey('app-jwt', 'org', '/reports', { page: 2 }), inflightGetKey('app-jwt', 'org', '/reports', { page: 2 }))
assert.equal(inflightGetKey(alice, 'org', '/reports', undefined), inflightGetKey(alice, 'org', '/reports', null))
// ...and different ones do not.
assert.notEqual(inflightGetKey(alice, 'org', '/reports', { page: 2 }), inflightGetKey(alice, 'org', '/reports', { page: 3 }))
assert.notEqual(inflightGetKey(alice, 'org', '/reports', null), inflightGetKey(alice, 'org', '/users', null))
assert.notEqual(inflightGetKey(alice, 'org-2', '/reports', null), inflightGetKey(alice, 'org', '/reports', null))
console.log('PASS: only the same sender shares an in-flight GET')

// The real useMyFetch, with Nuxt's auto-imports stubbed: the key it builds
// must hold the Authorization it sends (a key without it shares across viewers).
const source = readFileSync(new URL('../../composables/useMyFetch.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
function loadUseMyFetch(embed) {
  const sent = []
  const stubs = {
    process: { client: true },
    ref,
    useRuntimeConfig: () => ({ public: { baseURL: '/api' } }),
    useAuth: () => ({ token: ref('app-jwt') }),
    useOrganization: () => ({ organization: ref(null), ensureOrganization: async () => ({ id: 'org' }) }),
    useEmbedCredential: () => embed,
    useFetch: () => { throw new Error('useFetch is the server path; this is the client') },
    // Never answers: the first call stays in flight while the second is made.
    $fetch: (request, opts) => { sent.push({ request, authorization: opts.headers.Authorization }); return new Promise(() => {}) },
  }
  const exports = {}
  new Function('exports', ...Object.keys(stubs), compiled)(exports, ...Object.values(stubs))
  return { useMyFetch: exports.useMyFetch, sent }
}
const settle = () => new Promise((resolve) => setImmediate(resolve))
const viewer = (name) => ({ token: `bow_embed_${name}`, organizationId: 'org', hostApplication: 'host_1', viewer: { id: `u-${name}` } })
{
  const embed = ref(viewer('alice'))
  const { useMyFetch, sent } = loadUseMyFetch(embed)
  useMyFetch('/reports', { method: 'GET' })
  await settle()
  embed.value = viewer('bob')
  useMyFetch('/reports', { method: 'GET' })
  await settle()
  assert.deepEqual(sent.map((s) => s.authorization), ['Bearer bow_embed_alice', 'Bearer bow_embed_bob'], 'the next viewer sends its own GET')
}
{
  const embed = ref(viewer('alice'))
  const { useMyFetch, sent } = loadUseMyFetch(embed)
  useMyFetch('/reports', { method: 'GET' })
  useMyFetch('/reports', { method: 'GET' })
  await settle()
  assert.equal(sent.length, 1, 'the same viewer\'s identical GETs share one call')
}
console.log('PASS: useMyFetch shares an in-flight GET with the same sender only')

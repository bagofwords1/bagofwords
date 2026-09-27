// The host-side app data helper: request validation, route/method/body per op,
// and server error -> runtime error.code mapping (plan D-ERRORS). The HTTP
// boundary is a recording stub shaped like useMyFetch's { data, error } refs.
import assert from 'node:assert/strict'

import {
  appDataErrorFrom,
  appDataPath,
  isAppDataRequest,
  performAppDataRequest,
} from '../../utils/artifactAppData.ts'

const req = (fields) => ({ type: 'APP_DATA_REQUEST', rid: 'rid-1', collection: 'notes', ...fields })

// --- request shape -----------------------------------------------------------
assert.equal(isAppDataRequest(req({ op: 'list' })), true)
assert.equal(isAppDataRequest(req({ op: 'update', id: 'r1', data: { a: 1 }, version: 2 })), true)
assert.equal(isAppDataRequest(req({ op: 'drop' })), false, 'unknown op')
assert.equal(isAppDataRequest({ ...req({ op: 'list' }), type: 'ARTIFACT_SET_PARAMS' }), false)
assert.equal(isAppDataRequest({ ...req({ op: 'list' }), rid: '' }), false, 'rid is required')
assert.equal(isAppDataRequest({ ...req({ op: 'list' }), collection: 5 }), false)
assert.equal(isAppDataRequest(null), false)
assert.equal(isAppDataRequest('APP_DATA_REQUEST'), false)

// --- paths use the PARENT artifact id and encode segments --------------------
assert.equal(appDataPath('art-parent', req({ op: 'list' })), '/api/artifacts/art-parent/data/notes')
assert.equal(appDataPath('art-parent', req({ op: 'create', data: {} })), '/api/artifacts/art-parent/data/notes')
assert.equal(appDataPath('art-parent', req({ op: 'update', id: 'rec-1' })), '/api/artifacts/art-parent/data/notes/rec-1')
assert.equal(appDataPath('art-parent', req({ op: 'delete', id: 'rec-1' })), '/api/artifacts/art-parent/data/notes/rec-1')
assert.equal(appDataPath('a/b', req({ op: 'delete', id: 'rec-1' })), '/api/artifacts/a%2Fb/data/notes/rec-1', 'artifact id is encoded')
// URL parsing removes dot segments, so ".." as a segment would re-target the
// host's own session at other /api/artifacts/... endpoints. Segments are
// allowlisted, never merely encoded.
assert.throws(() => appDataPath('art-parent', req({ op: 'list', collection: '..' })))
assert.throws(() => appDataPath('art-parent', req({ op: 'delete', id: '..' })))

// --- segment allowlist: collection ^[a-z][a-z0-9_]{0,63}$, id ^[A-Za-z0-9-]{1,64}$
const BAD_COLLECTIONS = ['..', '.', 'a/b', 'Notes', 'NOTES', '', 'n' + 'x'.repeat(64), 'n?x', '1notes', '_notes', 'no-tes', 'no tes', '%2e%2e']
const BAD_IDS = ['..', '.', 'a/b', '', 'r'.repeat(65), 'r_1', 'r?x', 'r#1', '%2e%2e', 'a b', 5, null]
for (const collection of BAD_COLLECTIONS) {
  for (const fields of [{ op: 'list' }, { op: 'create', data: {} }, { op: 'update', id: 'rec-1', data: {}, version: 1 }, { op: 'delete', id: 'rec-1', version: 1 }]) {
    const calls = []
    const res = await performAppDataRequest(req({ ...fields, collection }), { artifactId: 'art-parent', fetch: async (p, i) => { calls.push(p); return { data: { value: { items: [] } }, error: { value: null } } } })
    assert.equal(calls.length, 0, `collection ${JSON.stringify(collection)} (${fields.op}) must not be fetched`)
    assert.equal(res.ok, false)
    assert.equal(res.rid, 'rid-1')
    assert.equal(res.error.code, 'validation', `collection ${JSON.stringify(collection)} (${fields.op})`)
  }
}
for (const id of BAD_IDS) {
  for (const fields of [{ op: 'update', data: {}, version: 1 }, { op: 'delete', version: 1 }]) {
    const calls = []
    const res = await performAppDataRequest(req({ ...fields, id }), { artifactId: 'art-parent', fetch: async (p) => { calls.push(p); return { data: { value: {} }, error: { value: null } } } })
    assert.equal(calls.length, 0, `id ${JSON.stringify(id)} (${fields.op}) must not be fetched`)
    assert.equal(res.error.code, 'validation', `id ${JSON.stringify(id)} (${fields.op})`)
  }
}
// Boundaries that ARE valid: 64-char names, uppercase/uuid ids.
for (const [collection, id] of [['n' + 'x'.repeat(63), 'r'.repeat(64)], ['a', 'ABC-def-123'], ['notes_2', '3f2b9c1e-0d4a-4c8e-9b7a-1e2d3c4b5a69']]) {
  const calls = []
  const res = await performAppDataRequest(req({ op: 'delete', collection, id, version: 1 }), { artifactId: 'art-parent', fetch: async (p) => { calls.push(p); return { data: { value: { id, deleted: true } }, error: { value: null } } } })
  assert.equal(res.ok, true, `${collection}/${id} is valid`)
  assert.deepEqual(calls, [`/api/artifacts/art-parent/data/${collection}/${id}`])
}

// --- performAppDataRequest: method/body per op, result shape ------------------
function recorder(reply) {
  const calls = []
  const fetch = async (path, init) => {
    calls.push({ path, init })
    return reply(path, init)
  }
  return { calls, fetch }
}
const ok = (value) => async () => ({ data: { value }, error: { value: null } })
const record = { id: 'rec-1', data: { text: 'hi' }, user: { id: 'u1', name: 'A' }, version: 1, created_at: 't', updated_at: 't', mine: true }

{
  const { calls, fetch } = recorder(ok({ items: [record] }))
  const res = await performAppDataRequest(req({ op: 'list' }), { artifactId: 'art-parent', fetch })
  assert.deepEqual(calls, [{ path: '/api/artifacts/art-parent/data/notes', init: { method: 'GET' } }])
  assert.deepEqual(res, { type: 'APP_DATA_RESULT', rid: 'rid-1', ok: true, items: [record] })
}
{
  const { calls, fetch } = recorder(ok(record))
  const res = await performAppDataRequest(req({ op: 'create', data: { text: 'hi' } }), { artifactId: 'art-parent', fetch })
  assert.deepEqual(calls, [{ path: '/api/artifacts/art-parent/data/notes', init: { method: 'POST', body: { data: { text: 'hi' } } } }])
  assert.deepEqual(res, { type: 'APP_DATA_RESULT', rid: 'rid-1', ok: true, record })
}
{
  const { calls, fetch } = recorder(ok({ ...record, version: 2 }))
  const res = await performAppDataRequest(req({ op: 'update', id: 'rec-1', data: { text: 'yo' }, version: 1 }), { artifactId: 'art-parent', fetch })
  assert.deepEqual(calls, [{ path: '/api/artifacts/art-parent/data/notes/rec-1', init: { method: 'PATCH', body: { data: { text: 'yo' }, version: 1 } } }])
  assert.equal(res.ok, true)
  assert.equal(res.record.version, 2)
}
{
  const { calls, fetch } = recorder(ok({ id: 'rec-1', deleted: true }))
  const res = await performAppDataRequest(req({ op: 'delete', id: 'rec-1', version: 3 }), { artifactId: 'art-parent', fetch })
  assert.deepEqual(calls, [{ path: '/api/artifacts/art-parent/data/notes/rec-1', init: { method: 'DELETE', body: { version: 3 } } }])
  assert.deepEqual(res, { type: 'APP_DATA_RESULT', rid: 'rid-1', ok: true, record: { id: 'rec-1', deleted: true } })
}

// --- missing artifact id -> unavailable, no request ---------------------------
for (const artifactId of [null, undefined, '']) {
  const { calls, fetch } = recorder(ok({ items: [] }))
  const res = await performAppDataRequest(req({ op: 'list' }), { artifactId, fetch })
  assert.equal(calls.length, 0)
  assert.equal(res.ok, false)
  assert.equal(res.rid, 'rid-1')
  assert.equal(res.error.code, 'unavailable')
}

// --- update/delete without id or version are refused locally ------------------
for (const bad of [req({ op: 'update', data: {}, version: 1 }), req({ op: 'update', id: 'r', data: {} }), req({ op: 'delete', id: 'r' })]) {
  const { calls, fetch } = recorder(ok({}))
  const res = await performAppDataRequest(bad, { artifactId: 'art-parent', fetch })
  assert.equal(calls.length, 0)
  assert.equal(res.error.code, 'validation')
}

// --- error mapping ----------------------------------------------------------
// Shapes as ofetch's FetchError delivers them through useMyFetch.
const httpError = (status, error_code, params) => Object.assign(new Error(`HTTP ${status}`), {
  statusCode: status, status, data: { detail: `server says ${status}`, error_code, params, status_code: status },
})
const cases = [
  [httpError(409, 'app_data.conflict', { current_version: 4 }), 'conflict'],
  [httpError(401, 'app_data.unauthenticated'), 'unauthenticated'],
  [httpError(403, 'app_data.forbidden'), 'forbidden'],
  [httpError(403, 'access.denied'), 'forbidden'],
  [httpError(404, 'app_data.collection_not_declared'), 'collection_not_declared'],
  [httpError(404, 'app_data.record_not_found'), 'not_found'],
  [httpError(404, 'artifact.not_found'), 'not_found'],
  [httpError(413, 'app_data.too_large'), 'too_large'],
  [httpError(422, 'app_data.validation', { field: 'text', reason: 'x' }), 'validation'],
  [httpError(422, 'app_data.limit_reached', { limit: 10 }), 'limit_reached'],
  // Status fallbacks when no error_code is present (e.g. FastAPI body validation).
  [httpError(401), 'unauthenticated'],
  [httpError(403), 'forbidden'],
  [httpError(404), 'not_found'],
  [httpError(409), 'conflict'],
  [httpError(413), 'too_large'],
  [httpError(422), 'validation'],
  [httpError(500), 'error'],
  // No response at all: the request never reached the server.
  [Object.assign(new TypeError('fetch failed'), { statusCode: undefined, data: undefined }), 'network'],
]
for (const [err, code] of cases) {
  assert.equal(appDataErrorFrom(err).code, code, `status ${err.statusCode} ${err.data?.error_code} -> ${code}`)
  const fetch = async () => ({ data: { value: null }, error: { value: err } })
  const res = await performAppDataRequest(req({ op: 'list' }), { artifactId: 'art-parent', fetch })
  assert.deepEqual(res.ok, false)
  assert.equal(res.error.code, code)
  assert.equal(res.rid, 'rid-1')
}

// Messages: the host's localizer wins; otherwise server detail; never empty.
assert.equal(appDataErrorFrom(httpError(403, 'app_data.forbidden'), () => 'Localized forbidden').message, 'Localized forbidden')
assert.equal(appDataErrorFrom(httpError(403, 'app_data.forbidden')).message, 'server says 403')
assert.ok(appDataErrorFrom(new TypeError('fetch failed')).message.length > 0)
assert.ok(appDataErrorFrom(undefined).message.length > 0)
assert.equal(appDataErrorFrom(undefined).code, 'error')

// The localizer receives the original error (so it can read error_code/params).
{
  const err = httpError(409, 'app_data.conflict', { current_version: 7 })
  let seen
  const fetch = async () => ({ data: { value: null }, error: { value: err } })
  const res = await performAppDataRequest(req({ op: 'update', id: 'rec-1', data: {}, version: 6 }), {
    artifactId: 'art-parent', fetch, message: (e) => { seen = e; return `now at ${e.data.params.current_version}` },
  })
  assert.equal(seen, err)
  assert.deepEqual(res.error, { code: 'conflict', message: 'now at 7' })
}

// A throwing transport becomes a result, never a rejection (the iframe must get an answer).
{
  const fetch = async () => { throw new TypeError('socket hang up') }
  const res = await performAppDataRequest(req({ op: 'list' }), { artifactId: 'art-parent', fetch })
  assert.equal(res.ok, false)
  assert.equal(res.error.code, 'network')
}

// A list reply without an items array is malformed.
{
  const res = await performAppDataRequest(req({ op: 'list' }), { artifactId: 'art-parent', fetch: ok({ nope: 1 }) })
  assert.equal(res.ok, false)
  assert.equal(res.error.code, 'error')
}

// Results are plain data (structured-clone safe for postMessage).
{
  const res = await performAppDataRequest(req({ op: 'list' }), { artifactId: 'art-parent', fetch: ok({ items: [record] }) })
  assert.deepEqual(structuredClone(res), res)
}

// useMyFetch wraps bodies in Vue refs (reactive Proxies) that postMessage
// cannot clone; results must be detached plain copies.
{
  const reactive = (o) => new Proxy(o, {})
  const res = await performAppDataRequest(req({ op: 'list' }), { artifactId: 'art-parent', fetch: ok(reactive({ items: reactive([reactive({ ...record })]) })) })
  assert.deepEqual(structuredClone(res), { type: 'APP_DATA_RESULT', rid: 'rid-1', ok: true, items: [record] })
  const created = await performAppDataRequest(req({ op: 'create', data: {} }), { artifactId: 'art-parent', fetch: ok(reactive({ ...record })) })
  assert.deepEqual(structuredClone(created).record, record)
}

console.log('artifactAppData: all assertions passed')

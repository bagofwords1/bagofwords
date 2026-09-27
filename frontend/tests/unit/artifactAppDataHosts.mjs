// Drive the real message handlers of both artifact hosts (in-app ArtifactFrame
// and the public /r page) with APP_DATA_REQUEST messages. The handler functions
// are extracted from the SFC sources (pattern of artifactVerificationDelivery.mjs);
// the HTTP boundary is a recording stub and the helper is the real one.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

import * as appData from '../../utils/artifactAppData.ts'

// Extracts top-level function declarations and `const` statements by name.
function extract(file, names) {
  const source = fs.readFileSync(new URL(file, import.meta.url), 'utf8')
  const script = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  const ast = ts.createSourceFile('host.ts', script, ts.ScriptTarget.Latest, true)
  const nameOf = (s) => ts.isFunctionDeclaration(s) ? s.name?.text
    : ts.isVariableStatement(s) ? s.declarationList.declarations[0].name.getText(ast) : undefined
  const selected = ast.statements.filter(s => names.includes(nameOf(s)))
  return { source, code: ts.transpileModule(selected.map(s => s.getText(ast)).join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, found: selected.map(nameOf) }
}

const flush = () => new Promise(resolve => setImmediate(resolve))
const frameWindow = (name) => { const posted = []; return { name, posted, postMessage(msg, target) { posted.push({ msg, target }) } } }
const request = (fields = {}) => ({ type: 'APP_DATA_REQUEST', rid: 'rid-42', op: 'list', collection: 'notes', ...fields })

function makeContext(extra) {
  const fetches = []
  let reply = async () => ({ data: { value: { items: [{ id: 'r1', version: 1 }] } }, error: { value: null } })
  const ctx = {
    window: { location: { origin: 'https://app.test' } },
    console: { log() {}, error() {}, warn() {} },
    isAppDataRequest: appData.isAppDataRequest,
    performAppDataRequest: appData.performAppDataRequest,
    useMyFetch: (path, init) => { fetches.push({ path, init }); return reply(path, init) },
    getErrorMessage: (err) => `localized:${err?.data?.error_code}`,
    ...extra,
  }
  vm.createContext(ctx)
  return { ctx, fetches, setReply: (fn) => { reply = fn } }
}

// A trusted frame's request that fails validation is answered (so the
// runtime's promise settles) and never fetched; a foreign one stays silent.
async function assertInvalidRequestsAnswered(ctx, fetches, trusted, foreign, send) {
  const invalid = [
    request({ rid: 'bad-op', op: 'drop' }),
    request({ rid: 'bad-coll', collection: '..' }),
    request({ rid: 'bad-coll2', collection: 'a/b' }),
    request({ rid: 'bad-id', op: 'delete', id: '..', version: 1 }),
    request({ rid: 'no-coll', collection: 5 }),
    { type: 'APP_DATA_REQUEST', rid: 'no-op', collection: 'notes' },
  ]
  const before = fetches.length
  for (const data of invalid) {
    send(foreign, data)
    await flush()
    assert.equal(foreign.posted.length, 0, 'foreign sources are ignored')
    send(trusted, data)
    await flush()
    assert.equal(fetches.length, before, `invalid ${data.rid} must not be fetched`)
    const reply = trusted.posted.at(-1)
    assert.equal(reply.target, 'https://app.test')
    assert.equal(reply.msg.type, 'APP_DATA_RESULT')
    assert.equal(reply.msg.rid, data.rid, `invalid ${data.rid} is answered`)
    assert.equal(reply.msg.ok, false)
    assert.equal(reply.msg.error.code, 'validation')
    assert.ok(reply.msg.error.message.length > 0)
  }
}

// Anything thrown while answering still settles the request with code 'error'.
async function assertThrowAnswered(ctx, setReply, trusted, send) {
  const original = ctx.getErrorMessage
  setReply(async () => ({ data: { value: null }, error: { value: { statusCode: 500, data: {} } } }))
  ctx.getErrorMessage = () => { throw new Error('i18n exploded') }
  send(trusted, request({ rid: 'rid-throw' }))
  await flush()
  ctx.getErrorMessage = original
  const reply = trusted.posted.at(-1)
  assert.equal(reply.msg.rid, 'rid-throw', 'a throwing handler must still answer')
  assert.equal(reply.msg.ok, false)
  assert.equal(reply.msg.error.code, 'error')
}

// --- in-app host: components/dashboard/ArtifactFrame.vue ---------------------
{
  const { source, code, found } = extract('../../components/dashboard/ArtifactFrame.vue', ['handleIframeMessage', 'handleAppDataRequest', 'srcdocArtifact', 'iframeSrcdoc'])
  assert.ok(found.includes('handleIframeMessage'))
  assert.ok(found.includes('iframeSrcdoc') && found.includes('srcdocArtifact'), 'the srcdoc must carry the artifact identity it was built for')

  const main = frameWindow('main'), full = frameWindow('fullscreen'), foreign = frameWindow('foreign')
  const { ctx, fetches, setReply } = makeContext({
    props: { verificationPreview: false },
    iframeRef: { value: { contentWindow: main } },
    fullscreenIframeRef: { value: { contentWindow: full } },
    // id is a VERSION id; artifact_id is the parent identity the data belongs to.
    selectedArtifact: { value: { id: 'version-7', artifact_id: 'parent-1' } },
    // Stand-ins for Vue's computed and the (large) document builder.
    computed: (fn) => ({ get value() { return fn() } }),
    buildIframeSrcdoc: () => srcdocHtml,
  })
  let srcdocHtml = '<html>doc for parent-1</html>'
  vm.runInContext(code, ctx)
  // The template reads iframeSrcdoc when it renders the document.
  const renderSrcdoc = () => vm.runInContext('iframeSrcdoc.value', ctx)
  assert.equal(renderSrcdoc(), '<html>doc for parent-1</html>')

  // A foreign window is ignored: no fetch, no reply anywhere.
  ctx.handleIframeMessage({ source: foreign, data: request() })
  ctx.handleIframeMessage({ source: null, data: request() })
  await flush()
  assert.equal(fetches.length, 0, 'foreign source must not trigger a fetch')
  assert.deepEqual([main.posted, full.posted, foreign.posted], [[], [], []])

  // Main iframe: fetch uses the PARENT artifact id; reply to the source, same rid, own origin.
  ctx.handleIframeMessage({ source: main, data: request() })
  await flush()
  assert.deepEqual(fetches, [{ path: '/api/artifacts/parent-1/data/notes', init: { method: 'GET' } }])
  assert.equal(main.posted.length, 1)
  assert.deepEqual(main.posted[0], { msg: { type: 'APP_DATA_RESULT', rid: 'rid-42', ok: true, items: [{ id: 'r1', version: 1 }] }, target: 'https://app.test' })
  assert.equal(full.posted.length, 0)

  // Fullscreen iframe is accepted and answered on ITS window.
  ctx.handleIframeMessage({ source: full, data: request({ rid: 'rid-full', op: 'create', data: { text: 'hi' } }) })
  await flush()
  assert.deepEqual(fetches.at(-1), { path: '/api/artifacts/parent-1/data/notes', init: { method: 'POST', body: { data: { text: 'hi' } } } })
  assert.equal(full.posted.length, 1)
  assert.equal(full.posted[0].msg.rid, 'rid-full')
  assert.equal(full.posted[0].target, 'https://app.test')
  assert.equal(main.posted.length, 1)

  // Server errors come back with the runtime code and the host's localized message.
  setReply(async () => ({ data: { value: null }, error: { value: { statusCode: 403, data: { error_code: 'app_data.forbidden', detail: 'x' } } } }))
  ctx.handleIframeMessage({ source: main, data: request({ rid: 'rid-403', op: 'update', id: 'r1', data: {}, version: 1 }) })
  await flush()
  assert.deepEqual(main.posted.at(-1).msg, { type: 'APP_DATA_RESULT', rid: 'rid-403', ok: false, error: { code: 'forbidden', message: 'localized:app_data.forbidden' } })

  // Without a string rid there is nothing to answer: no reply, no fetch.
  const before = fetches.length
  const postedBefore = main.posted.length
  ctx.handleIframeMessage({ source: main, data: { type: 'APP_DATA_REQUEST', op: 'list', collection: 'notes' } })
  ctx.handleIframeMessage({ source: main, data: { type: 'APP_DATA_REQUEST', rid: 7, op: 'list', collection: 'notes' } })
  await flush()
  assert.equal(fetches.length, before)
  assert.equal(main.posted.length, postedBefore)

  await assertInvalidRequestsAnswered(ctx, fetches, main, foreign, (source, data) => ctx.handleIframeMessage({ source, data }))
  await assertThrowAnswered(ctx, setReply, main, (source, data) => ctx.handleIframeMessage({ source, data }))
  setReply(async () => ({ data: { value: { items: [] } }, error: { value: null } }))

  // Artifact switch: the iframe keeps the OLD document until its srcdoc is
  // rebuilt, so its requests must stay on the old artifact's collections.
  ctx.selectedArtifact.value = { id: 'version-9', artifact_id: 'parent-2' }
  ctx.handleIframeMessage({ source: main, data: request({ rid: 'rid-old-doc' }) })
  await flush()
  assert.equal(fetches.at(-1).path, '/api/artifacts/parent-1/data/notes', 'old document must not reach the newly selected artifact')
  // While no document is built (switch in progress) nothing is answered with data.
  srcdocHtml = undefined
  renderSrcdoc()
  const beforeSwitch = fetches.length
  ctx.handleIframeMessage({ source: main, data: request({ rid: 'rid-switching' }) })
  await flush()
  assert.equal(fetches.length, beforeSwitch)
  assert.equal(main.posted.at(-1).msg.error.code, 'unavailable')
  // The new document is bound to the new artifact.
  srcdocHtml = '<html>doc for parent-2</html>'
  renderSrcdoc()
  ctx.handleIframeMessage({ source: full, data: request({ rid: 'rid-new-doc' }) })
  await flush()
  assert.equal(fetches.at(-1).path, '/api/artifacts/parent-2/data/notes')

  // No artifact loaded yet -> unavailable without a fetch.
  ctx.selectedArtifact.value = null
  srcdocHtml = '<html>sample</html>'
  renderSrcdoc()
  const beforeNone = fetches.length
  ctx.handleIframeMessage({ source: main, data: request({ rid: 'rid-none' }) })
  await flush()
  assert.equal(fetches.length, beforeNone)
  assert.equal(main.posted.at(-1).msg.error.code, 'unavailable')

  // Nothing credential-like is ever posted to the iframe.
  const everything = JSON.stringify([main.posted, full.posted])
  assert.doesNotMatch(everything, /authorization|bearer|token/i)
  assert.match(source, /ref="fullscreenIframeRef"/, 'fullscreen iframe needs a ref so its requests can be answered')
}

// --- public host: pages/r/[id]/index.vue ------------------------------------
{
  const { code, found } = extract('../../pages/r/[id]/index.vue', ['handleArtifactParamsMessage', 'handleAppDataRequest'])
  assert.ok(found.includes('handleArtifactParamsMessage'))

  const main = frameWindow('main'), foreign = frameWindow('foreign')
  const { ctx, fetches, setReply } = makeContext({
    artifactIframeRef: { value: { contentWindow: main } },
    artifact: { value: { id: 'version-3', artifact_id: 'parent-9' } },
  })
  vm.runInContext(code, ctx)

  ctx.handleArtifactParamsMessage({ source: foreign, data: request() })
  await flush()
  assert.equal(fetches.length, 0, 'foreign source must not trigger a fetch')
  assert.equal(foreign.posted.length, 0)

  ctx.handleArtifactParamsMessage({ source: main, data: request({ op: 'delete', id: 'r1', version: 2 }) })
  await flush()
  assert.deepEqual(fetches, [{ path: '/api/artifacts/parent-9/data/notes/r1', init: { method: 'DELETE', body: { version: 2 } } }])
  assert.equal(main.posted.length, 1)
  assert.equal(main.posted[0].msg.type, 'APP_DATA_RESULT')
  assert.equal(main.posted[0].msg.rid, 'rid-42')
  assert.equal(main.posted[0].target, 'https://app.test')

  await assertInvalidRequestsAnswered(ctx, fetches, main, foreign, (source, data) => ctx.handleArtifactParamsMessage({ source, data }))
  await assertThrowAnswered(ctx, setReply, main, (source, data) => ctx.handleArtifactParamsMessage({ source, data }))
}

console.log('artifactAppDataHosts: all assertions passed')

import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import test from 'node:test'

import ts from 'typescript'
import { computed, isProxy, isReactive, nextTick, ref, shallowRef, toRaw, watch } from 'vue'

// Run: node --test tests/unit/artifactFrameDataNoCopies.mjs
// The in-app dashboard (ArtifactFrame.vue) hands its data to its frame without
// copies made on the page: the queries response (/api/queries?report_id=&
// artifact_id=, or the report page's prefetch of it) is used as it came (no
// clone, never written to), the rows are not made deeply reactive, and neither
// the srcdoc seed nor ARTIFACT_DATA is built through a JSON text round trip.
// What the frame receives keeps the values (and the separate arrays per chart)
// it had when everything went through JSON; a payload the browser refuses to
// copy goes as the JSON copy, with one console warning. A frame that rendered
// from its srcdoc seed is not sent the same data again when it reports
// ARTIFACT_READY; what changed while it loaded is sent once.
//
// The real code: ArtifactFrame.vue's own functions, declarations and its
// data watcher, taken from its <script setup>, over the real useMyFetch. Only
// the network ($fetch) and the frames (postMessage copies like a browser:
// structuredClone) are played. Strict mode, as the page's module: a write into
// frozen data throws.

const require = createRequire(import.meta.url)
const read = (rel) => readFileSync(new URL(`../../${rel}`, import.meta.url), 'utf8')
const transpile = (code, module) => ts.transpileModule(code, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, ...(module ? { module: ts.ModuleKind.CommonJS } : {}) },
}).outputText
const load = (rel) => { const m = { exports: {} }; new Function('exports', 'module', 'require', transpile(read(rel), true))(m.exports, m, require); return m.exports }

const { artifactRowCounts } = load('utils/artifactRowCounts.ts')
// The frame payload helpers the frame imports (absent before this change).
const framePayload = () => (existsSync(new URL('../../utils/artifactFramePayload.ts', import.meta.url)) ? load('utils/artifactFramePayload.ts') : {})

function realUseMyFetch(responses, fetched) {
  const stubs = {
    process: { client: true },
    ref,
    useRuntimeConfig: () => ({ public: { baseURL: '/api' } }),
    useAuth: () => ({ token: ref('app-jwt') }),
    useOrganization: () => ({ organization: ref(null), ensureOrganization: async () => ({ id: 'org' }) }),
    useEmbedCredential: () => ref(null),
    useFetch: () => { throw new Error('useFetch is the server path; this is the client') },
    $fetch: async (request, opts) => {
      fetched.push({ request, opts })
      if (!(request in responses)) throw new Error(`unexpected request ${request}`)
      return responses[request]
    },
  }
  const exports = {}
  new Function('exports', ...Object.keys(stubs), transpile(read('composables/useMyFetch.ts'), true))(exports, ...Object.values(stubs))
  return exports.useMyFetch
}

// ── the frame's script ──────────────────────────────────────────────────────
const source = read('components/dashboard/ArtifactFrame.vue')
const script = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const ast = ts.createSourceFile('frame.ts', script, ts.ScriptTarget.Latest, true)
const fn = (name) => ast.statements.find((s) => ts.isFunctionDeclaration(s) && s.name?.text === name)?.getText(ast)
const declaration = (name) => ast.statements.find((s) => ts.isVariableStatement(s)
  && s.declarationList.declarations.some((d) => d.name.getText(ast) === name))?.getText(ast)
const dataWatcher = ast.statements.find((s) => ts.isExpressionStatement(s)
  && /^watch\(\[visualizationsData, filesData(, iframeReady)?\]/.test(s.getText(ast)))?.getText(ast)
assert.ok(dataWatcher, 'the frame re-sends its data when it changes (watcher)')

const FUNCTIONS = ['fetchData', 'runParamQueries', 'sendDataToIframe', 'paramSubsetForQuery', 'queriesWithIdentityParams', 'replaceVisualizationsData',
  'handleIframeMessage', 'frameContext', 'frameHasSeedData']
const DECLARED = ['useMyFetch', 'visualizationsData', 'visualizationsSendDue', 'srcdocSeed', 'seededFrom'].filter(declaration)
const present = FUNCTIONS.filter(fn)
const frameCode = "'use strict';\n" + transpile([
  ...DECLARED.map(declaration),
  ...present.map(fn),
  dataWatcher,
  `return { ${[...DECLARED, ...present].join(', ')} };`,
].join('\n'))

const deepFreeze = (o) => { o = toRaw(o); if (o && typeof o === 'object' && !Object.isFrozen(o)) { Object.freeze(o); Object.values(o).forEach(deepFreeze) } return o }
const flush = async () => { for (let i = 0; i < 5; i++) await nextTick() }

const QUERIES = '/api/queries?report_id=rep-1&artifact_id=art-1'
const RUN_Q1 = '/api/queries/q1/run'
const rowsQ1 = () => [{ region: 'EMEA', amount: 3 }, { region: 'APAC', amount: 5 }]
// Two charts over q1, one over q2.
const queriesResponse = () => [
  { id: 'q1', title: 'Sales', parameters: [{ name: 'region', source: 'input' }],
    visualizations: [{ id: 'viz-1', title: 'By region', view: { type: 'bar' } }, { id: 'viz-2', view: { type: 'table' } }],
    default_step: { status: 'success', data: { rows: rowsQ1(), columns: [{ field: 'region' }, { field: 'amount' }], info: { total_rows: 2 } },
      data_model: { type: 'bar' }, code: 'select 1' } },
  { id: 'q2', title: 'Count', visualizations: [{ id: 'viz-3', view: { type: 'count' } }],
    default_step: { status: 'success', data: { rows: [{ n: 1 }], columns: [{ field: 'n' }] }, data_model: {}, code: '' } },
]

function frame({ responses = {}, viewer = { id: 'u-1', name: 'Viewer' }, params = { values: {} }, verificationPreview = false, prefetch = null,
  runtime = { version: 11 }, layouts = { 'viz-1': { widths: { amount: 120 } } } } = {}) {
  const fetched = []
  const events = []
  const received = { main: [], fullscreen: [] }
  const frameWindow = (name) => ({ postMessage(message) { received[name].push(structuredClone(message)) } })
  const useApplicationFetch = realUseMyFetch(responses, fetched)
  const state = {
    console: { ...console, log: () => {}, error: () => {} },
    computed, ref, shallowRef, toRaw, watch, nextTick,
    ...framePayload(),
    useApplicationFetch,
    $fetch: async (path, opts) => { fetched.push({ request: path, opts, preview: true }); return responses[path] },
    artifactRowCounts,
    props: { reportId: 'rep-1', verificationPreview, prefetch },
    isLoading: ref(false), dataReady: ref(false), iframeReady: ref(false), fullscreenReady: ref(false),
    iframeRef: ref({ contentWindow: frameWindow('main') }),
    fullscreenRuntimeFrame: ref({ contentWindow: frameWindow('fullscreen') }),
    iframeError: ref(null),
    reportData: ref(null), reportSources: ref([]), filesData: ref([]),
    selectedArtifactId: ref('art-1'),
    selectedArtifact: ref({ id: 'art-1', artifact_id: 'dash-1', content: { code: 'App', visualization_ids: ['viz-1', 'viz-2', 'viz-3'] } }),
    queryParamSpecs: ref({}), paramValues: ref({}), viewAsMode: ref('you'),
    snapshotWithheld: ref(false), withheldCharts: ref([]), ownerFailedCharts: ref(false), allWithheld: ref(false),
    viewerRunNoAccess: ref(false), viewerRunNeedsConnect: ref(false), hasOwnResult: ref(false), credentialScoped: ref(false),
    viewerRunFailedReason: ref(null),
    effectiveViewerContext: ref(viewer),
    verificationRequests: new Map(), verificationRevision: 0, previewConversationPolicy: null,
    paramAckSeq: 0, paramRunCounter: 0, latestParamRunForQid: {}, logActivity: null,
    verificationEvent: (kind, fields) => events.push({ kind, ...fields }), handleTableLayoutMessage: () => false, postParamsStatus: () => {}, syncParamsToUrl: () => {}, readParamsFromUrl: () => ({}),
    watchHydration: () => {}, resolveParamOptions: async () => {}, fetchArtifactFiles: async () => [],
    whenArtifactLibsSettled: async () => {}, inlineableArtifactLibs: () => [], frameIframeOptions: (seed) => ({ data: seed }),
    paramsPayload: () => params, artifactRuntime: () => ({ ...runtime }),
    loadTableLayouts: () => structuredClone(layouts), tableLayoutKey: () => 'dash-1',
  }
  const own = new Function(...Object.keys(state), frameCode)(...Object.values(state))
  return { state, own, fetched, received, events, runtime, layouts }
}

// What the old frame sent and seeded: everything through one JSON copy.
const oldPayload = (list, extra = {}) => JSON.parse(JSON.stringify({
  report: { id: 'rep-1', title: 'Pipeline', theme: 'default', show_widget_info: false },
  visualizations: list, files: [], current_user: { id: 'u-1', name: 'Viewer', team: undefined, score: NaN },
  params: { values: { region: 'EMEA', top: NaN }, declarations: [{ name: 'region', default: undefined }] }, runtime: { version: 11 },
  ...extra,
}))
const report = { id: 'rep-1', title: 'Pipeline', theme_name: 'default' }

test('the queries response is used as it came, never written to; the data and the seed are plain', async () => {
  const body = deepFreeze(queriesResponse())
  const before = JSON.stringify(body)
  const { own, state, fetched } = frame({ responses: { [QUERIES]: body, '/api/reports/rep-1': report } })
  await own.fetchData('art-1')
  const list = own.visualizationsData.value
  assert.deepEqual(list.map((v) => v.id), ['viz-1', 'viz-2', 'viz-3'])
  assert.equal(toRaw(list[0].rows), body[0].default_step.data.rows, 'the rows are the response\'s own rows (no clone)')
  assert.equal('noClone' in fetched.find((f) => f.request === QUERIES).opts, false, 'the opt-out is not sent on to $fetch')
  assert.equal(isReactive(list) || isProxy(list[0]) || isProxy(list[0].rows), false, 'the data is not deeply reactive')
  const seed = own.srcdocSeed.value
  assert.equal(isProxy(seed) || isProxy(seed.visualizations[0].rows), false, 'the srcdoc seed is plain data')
  assert.equal(seed.visualizations[0].rows, body[0].default_step.data.rows, 'and holds the rows without a copy')
  state.iframeReady.value = true
  own.sendDataToIframe()
  assert.equal(JSON.stringify(body), before, 'loading, seeding and sending never write into the response')
})

test('the report page\'s prefetch of the queries is used as it came too', async () => {
  const body = deepFreeze(queriesResponse())
  const fetched = []
  const useMyFetch = realUseMyFetch({ [QUERIES]: body }, fetched)
  // The report page's real prefetch (pages/reports/[id]/index.vue).
  const page = read('pages/reports/[id]/index.vue')
  const pageAst = ts.createSourceFile('page.ts', page.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1], ts.ScriptTarget.Latest, true)
  const start = pageAst.statements.find((s) => ts.isFunctionDeclaration(s) && s.name?.text === 'startArtifactFramePrefetch').getText(pageAst)
  const prefetchRef = { value: null }
  const run = new Function('useMyFetch', 'ensureArtifactLibs', 'artifactPageLibs', 'artifactFramePrefetch', 'report_id',
    `let artifactFramePrefetchStarted = false;\n${transpile(start)}\nstartArtifactFramePrefetch('art-1');`)
  run((url, opts) => (url === QUERIES ? useMyFetch(url, opts) : Promise.resolve({ data: ref(null), error: ref(null) })),
    () => {}, () => [], prefetchRef, 'rep-1')
  const prefetch = { artifactId: 'art-1', queries: prefetchRef.value.queries }
  const { own } = frame({ responses: { '/api/reports/rep-1': report }, prefetch })
  await own.fetchData('art-1')
  assert.equal(toRaw(own.visualizationsData.value[0].rows), body[0].default_step.data.rows, 'the prefetched rows are used without a clone')
})

test('ARTIFACT_DATA and the seed keep the JSON copy\'s values; each chart keeps its own rows', async () => {
  const params = { values: { region: 'EMEA', top: NaN }, declarations: [{ name: 'region', default: undefined }] }
  const viewer = { id: 'u-1', name: 'Viewer', team: undefined, score: NaN }
  const { own, state, received } = frame({ responses: { [QUERIES]: queriesResponse(), '/api/reports/rep-1': report }, viewer, params })
  await own.fetchData('art-1')
  const list = own.visualizationsData.value
  // The seed also carries the viewer's saved table layouts, as a push does.
  const expected = oldPayload(list, { table_layouts: { 'viz-1': { widths: { amount: 120 } } } })
  assert.deepStrictEqual(structuredClone(own.srcdocSeed.value), expected, 'the seed: same values as the JSON-copied seed')
  assert.equal(JSON.stringify(own.srcdocSeed.value), JSON.stringify(expected), 'and the same text in the frame\'s HTML')

  state.iframeReady.value = true
  state.fullscreenReady.value = true
  received.main.length = 0
  own.sendDataToIframe()
  for (const name of ['main', 'fullscreen']) {
    assert.equal(received[name].length, 1, `${name} frame gets the data`)
    assert.deepStrictEqual(received[name][0].payload, expected, `${name}: same values as from the JSON copy`)
    const [a, b] = received[name][0].payload.visualizations
    assert.notEqual(a.rows, b.rows, `${name}: two charts of one query do not share one rows array (as with JSON)`)
  }
  // Nothing JSON-copied on the page for the first chart's rows.
  let posted = null
  state.iframeRef.value = { contentWindow: { postMessage(m) { posted = m; structuredClone(m) } } }
  state.fullscreenReady.value = false
  own.sendDataToIframe()
  assert.equal(posted.payload.visualizations[0].rows, toRaw(list[0].rows), 'the rows are handed to postMessage as they are')
  assert.equal(state.dataReady.value, true)
})

test('a payload postMessage refuses goes as the JSON copy, with one warning', async () => {
  const { own, state, received } = frame({ responses: { [QUERIES]: queriesResponse(), '/api/reports/rep-1': report } })
  await own.fetchData('art-1')
  own.visualizationsData.value = own.visualizationsData.value.map((v, i) => (i === 2 ? { ...v, rows: [{ n: 1, format: () => 'x' }] } : v))
  state.iframeReady.value = true
  const warnings = []
  const warn = console.warn
  console.warn = (...args) => warnings.push(args)
  try {
    received.main.length = 0
    own.sendDataToIframe()
    own.sendDataToIframe()
  } finally { console.warn = warn }
  assert.equal(received.main.length, 2, 'both sends reach the frame')
  assert.deepStrictEqual(received.main[0].payload.visualizations[2].rows, [{ n: 1 }], 'as the JSON copy (the function dropped)')
  assert.equal(warnings.length, 1, 'one console warning')
  assert.equal(state.iframeError.value, null, 'not an error on the dashboard')
})

test('a filter run and an in-place refresh replace frozen data; the watcher sends it once', async () => {
  const fresh = { status: 'success', data: { rows: [{ region: 'EMEA', amount: 9 }], columns: [{ field: 'region' }, { field: 'amount' }], info: { total_rows: 40 } } }
  const responses = { [QUERIES]: queriesResponse(), '/api/reports/rep-1': report, [RUN_Q1]: fresh }
  const { own, state, received } = frame({ responses })
  await own.fetchData('art-1')
  own.visualizationsData.value = own.visualizationsData.value.map((v) => deepFreeze(v))  // (test setup: freeze what was loaded)
  const loaded = own.visualizationsData.value
  state.iframeReady.value = true
  await flush()
  received.main.length = 0

  await own.runParamQueries({ region: 'EMEA' })
  await flush()
  const data = own.visualizationsData.value
  assert.notEqual(data, loaded, 'the filter run replaced the list')
  assert.deepEqual(data.map((v) => v.rows), [fresh.data.rows, fresh.data.rows, [{ n: 1 }]], 'both charts of q1 carry the filtered rows')
  assert.deepEqual(data.map((v) => [v.row_count, v.truncated, v.stepStatus]), [[40, true, 'success'], [40, true, 'success'], [1, false, 'success']])
  assert.equal(data[2], loaded[2], 'a chart the run did not touch is kept as it was')
  assert.deepEqual(loaded[0].rows, rowsQ1(), 'the old charts were not edited')
  assert.equal(received.main.length, 1, 'the watcher sends the filtered data to the frame once')
  assert.deepEqual(received.main[0].payload.visualizations[0].rows, fresh.data.rows)

  // In-place refresh (a rerun, a hydration landing): fresh rows, no new seed.
  const seed = own.srcdocSeed.value
  const reloaded = queriesResponse()
  reloaded[0].default_step.data.rows = [{ region: 'LATAM', amount: 7 }]
  responses[QUERIES] = reloaded
  const before = own.visualizationsData.value
  received.main.length = 0
  await own.fetchData('art-1', { inPlace: true })
  await flush()
  assert.notEqual(own.visualizationsData.value, before, 'the refresh replaced the list')
  assert.deepEqual(own.visualizationsData.value[0].rows, [{ region: 'LATAM', amount: 7 }])
  assert.equal(own.srcdocSeed.value, seed, 'the frame is not rebuilt (same seed)')
  assert.ok(received.main.length >= 1, 'the frame is sent the refreshed data')
  assert.deepEqual(received.main.at(-1).payload.visualizations[0].rows, [{ region: 'LATAM', amount: 7 }])
})

// A filter run over two queries (q1 feeds viz-1 and viz-2, q2 feeds viz-3) whose
// answers land one at a time (the test resolves them).
const RUN_Q2 = '/api/queries/q2/run'
const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r }); return { promise, resolve } }
const bothFiltered = () => { const r = queriesResponse(); r[1].parameters = [{ name: 'region', source: 'input' }]; return r }
const answer = (rows) => ({ status: 'success', data: { rows, columns: Object.keys(rows[0]).map((field) => ({ field })) } })

test('a filter run writes each query as it lands and sends the frame its data once per run', async () => {
  const responses = { [QUERIES]: bothFiltered(), '/api/reports/rep-1': report }
  const { own, state, received } = frame({ responses })
  await own.fetchData('art-1')
  state.iframeReady.value = true
  await flush()
  received.main.length = 0

  const a = deferred(); const b = deferred()
  responses[RUN_Q1] = a.promise
  responses[RUN_Q2] = b.promise
  const run = own.runParamQueries({ region: 'EMEA' })
  a.resolve(answer([{ region: 'EMEA', amount: 9 }]))
  await flush()
  assert.deepEqual(own.visualizationsData.value.map((v) => v.rows), [[{ region: 'EMEA', amount: 9 }], [{ region: 'EMEA', amount: 9 }], [{ n: 1 }]],
    'q1\'s filtered rows are in the data as soon as q1 lands')
  assert.equal(received.main.length, 0, 'a landed query alone is not sent to the frame')
  b.resolve(answer([{ n: 4 }]))
  await run
  await flush()
  assert.deepEqual(own.visualizationsData.value.map((v) => v.rows), [[{ region: 'EMEA', amount: 9 }], [{ region: 'EMEA', amount: 9 }], [{ n: 4 }]])
  assert.equal(received.main.length, 1, 'the run sends the frame its data once')
  assert.deepEqual(received.main[0].payload.visualizations.map((v) => v.rows), [[{ region: 'EMEA', amount: 9 }], [{ region: 'EMEA', amount: 9 }], [{ n: 4 }]])
})

test('a reload during a filter run is not overwritten by a query that landed before it', async () => {
  const responses = { [QUERIES]: bothFiltered(), '/api/reports/rep-1': report }
  const { own, state, received } = frame({ responses })
  await own.fetchData('art-1')
  state.iframeReady.value = true
  await flush()

  const a = deferred(); const b = deferred()
  responses[RUN_Q1] = a.promise
  responses[RUN_Q2] = b.promise
  const run = own.runParamQueries({ region: 'EMEA' })
  a.resolve(answer([{ region: 'EMEA', amount: 9 }]))  // q1 lands
  await flush()
  // A reload (the viewer's Run, Refresh): the list is rebuilt from the server.
  const reloaded = bothFiltered()
  reloaded[0].default_step.data.rows = [{ region: 'LATAM', amount: 7 }]
  responses[QUERIES] = reloaded
  await own.fetchData('art-1')
  b.resolve(answer([{ n: 4 }]))  // q2 lands after the reload
  await run
  await flush()
  const data = own.visualizationsData.value
  assert.deepEqual(data.slice(0, 2).map((v) => v.rows), [[{ region: 'LATAM', amount: 7 }], [{ region: 'LATAM', amount: 7 }]],
    'q1 landed before the reload: the reloaded rows stay')
  // As before this change: a query landing after the reload is written into the reloaded list.
  assert.deepEqual(data[2].rows, [{ n: 4 }])
  assert.deepEqual(received.main.at(-1).payload.visualizations.map((v) => v.rows), data.map((v) => v.rows), 'the frame gets the same data')
})

test('the verification preview does not send the clone opt-out to its broker', async () => {
  const { own, fetched } = frame({ responses: { [QUERIES]: queriesResponse(), '/api/reports/rep-1': report }, verificationPreview: true })
  await own.fetchData('art-1')
  const call = fetched.find((f) => f.request === QUERIES)
  assert.ok(call?.preview, 'the preview fetches through its broker')
  assert.equal('noClone' in (call.opts || {}), false)
})

// ── ARTIFACT_READY: the frame rendered from its srcdoc seed ─────────────────
// What the frame posts once its first render is on screen (utils/artifactIframe.ts).
const ready = async (own, state, name = 'main') => {
  const source = (name === 'main' ? state.iframeRef : state.fullscreenRuntimeFrame).value.contentWindow
  own.handleIframeMessage({ source, data: { type: 'ARTIFACT_READY' } })
  await flush()
}
const opened = async (options = {}) => {
  const f = frame({ responses: { [QUERIES]: queriesResponse(), '/api/reports/rep-1': report }, ...options })
  await f.own.fetchData('art-1')
  await flush()
  return f
}

test('a fresh open: the frame already has its data from the seed, ARTIFACT_READY sends nothing', async () => {
  const { own, state, received } = await opened()
  assert.equal(received.main.length, 0, 'nothing is sent while the frame loads')
  await ready(own, state)
  assert.equal(state.iframeReady.value, true, 'the frame is ready')
  assert.equal(received.main.length, 0, 'and is not sent the data its seed already holds')
  assert.equal(state.iframeError.value, null)
})

test('what changed while the frame loaded is sent once, on ARTIFACT_READY', async () => {
  const changes = {
    'the data list replaced (a late identity run)': ({ own }) => own.replaceVisualizationsData(
      own.visualizationsData.value.map((v, i) => (i === 0 ? { ...v, rows: [{ region: 'LATAM', amount: 7 }] } : v))),
    'the embedded files': ({ state }) => { state.filesData.value = [{ id: 'file-1', url: 'https://files.test/1' }] },
    'the viewer (view-as)': ({ state }) => { state.effectiveViewerContext.value = { id: 'u-2', name: 'Member' } },
    'the param values': ({ params }) => { params.values.region = 'APAC' },
    'the report (its title or theme)': ({ state }) => { state.reportData.value = { ...state.reportData.value, title: 'Pipeline (renamed)' } },
    'the runtime version': ({ runtime }) => { runtime.version = 12 },
    'the saved table layouts (a resize saved elsewhere)': ({ layouts }) => { layouts['viz-2'] = { widths: { n: 90 }, order: ['n'] } },
    'the param ack (a control committed)': ({ params }) => { params.ack = 2 },
  }
  for (const [what, change] of Object.entries(changes)) {
    const params = { values: { region: 'EMEA' } }
    const f = await opened({ params })
    change({ ...f, params })
    await flush()
    assert.equal(f.received.main.length, 0, `${what}: not sent before the frame is ready`)
    await ready(f.own, f.state)
    assert.equal(f.received.main.length, 1, `${what}: sent once when the frame is ready`)
    const sent = f.received.main[0].payload
    assert.deepEqual(sent.visualizations.map((v) => v.rows), f.own.visualizationsData.value.map((v) => v.rows), `${what}: the live data`)
    assert.deepEqual(sent.current_user, f.state.effectiveViewerContext.value, `${what}: the live viewer`)
    assert.deepEqual(sent.files, f.state.filesData.value, `${what}: the live files`)
    assert.deepEqual(sent.params, params, `${what}: the live params`)
    assert.deepEqual(sent.report, f.state.reportData.value, `${what}: the live report`)
    assert.deepEqual(sent.runtime, f.runtime, `${what}: the live runtime`)
    assert.deepEqual(sent.table_layouts, f.layouts, `${what}: the live table layouts`)
  }
})

test('after ARTIFACT_READY: a filter run, an in-place refresh and new files each reach the frame', async () => {
  const fresh = { status: 'success', data: { rows: [{ region: 'EMEA', amount: 9 }], columns: [{ field: 'region' }, { field: 'amount' }] } }
  const responses = { [QUERIES]: queriesResponse(), '/api/reports/rep-1': report, [RUN_Q1]: fresh }
  const { own, state, received } = frame({ responses })
  await own.fetchData('art-1')
  await flush()
  await ready(own, state)
  const sends = []
  const count = async (action) => { received.main.length = 0; await action(); await flush(); sends.push(received.main.length); return received.main.at(-1)?.payload }

  const filtered = await count(() => own.runParamQueries({ region: 'EMEA' }))
  assert.deepEqual(filtered.visualizations[0].rows, fresh.data.rows, 'the filter run end sends the filtered rows')
  const reloaded = queriesResponse()
  reloaded[0].default_step.data.rows = [{ region: 'LATAM', amount: 7 }]
  responses[QUERIES] = reloaded
  const refreshed = await count(() => own.fetchData('art-1', { inPlace: true }))
  assert.deepEqual(refreshed.visualizations[0].rows, [{ region: 'LATAM', amount: 7 }], 'the in-place refresh sends the refreshed rows')
  const withFile = await count(() => { state.filesData.value = [{ id: 'file-1', url: 'https://files.test/1' }] })
  assert.deepEqual(withFile.files, [{ id: 'file-1', url: 'https://files.test/1' }], 'new files are sent')
  // Unchanged by the ARTIFACT_READY change: an in-place refresh sends three
  // times as before (the watcher for the new list, again for its re-resolved
  // files list, and fetchData's own send at its end).
  assert.deepEqual(sends, [1, 3, 1], 'sends per change (filter run end, in-place refresh, files)')
})

test('the verification preview is always sent its data on ARTIFACT_READY, with its data_sent event', async () => {
  const { own, state, received, events } = await opened({ verificationPreview: true })
  await ready(own, state)
  assert.equal(received.main.length, 1, 'the preview gets one push on ready')
  assert.equal(received.main[0].payload.verification_revision, 1, 'with its revision')
  assert.deepEqual(events.filter((e) => e.kind === 'data_sent').map((e) => e.data_revision), [1], 'and one data_sent event for it')
})

test('the fullscreen frame is sent its data on its own ARTIFACT_READY', async () => {
  const { own, state, received } = await opened()
  await ready(own, state)
  await ready(own, state, 'fullscreen')
  assert.equal(state.fullscreenReady.value, true)
  assert.equal(received.fullscreen.length, 1, 'the fullscreen frame gets its data')
  assert.deepEqual(received.fullscreen[0].payload.visualizations.map((v) => v.rows), own.visualizationsData.value.map((v) => v.rows))
  assert.deepEqual(received.fullscreen[0].payload.table_layouts, { 'viz-1': { widths: { amount: 120 } } }, 'with the saved table layouts')
})

test('the seed carries the viewer\'s saved table layouts, so the frame has them on first paint', async () => {
  const { own } = await opened()
  assert.deepEqual(own.srcdocSeed.value.table_layouts, { 'viz-1': { widths: { amount: 120 } } })
})

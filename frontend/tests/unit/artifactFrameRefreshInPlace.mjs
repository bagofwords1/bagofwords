import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import test from 'node:test'

import ts from 'typescript'
import { computed, nextTick, ref, shallowRef, toRaw, watch } from 'vue'

// Run: node --test tests/unit/artifactFrameRefreshInPlace.mjs
// An in-place refresh of the in-app dashboard (a rerun landing, a hydration
// finishing: ArtifactFrame.vue's fetchData(id, { inPlace: true })) updates the
// rendered dashboard without reloading its frame: the frame's HTML (srcdoc)
// stays the same string, and the refreshed rows reach the frame in one
// ARTIFACT_DATA. A change that does define a new document (the artifact's
// code, a full load with a new seed, the per-widget info toggle) still reloads
// the frame; from that change until the new document reports ARTIFACT_READY no
// data is posted into it, and on READY it gets what it lacks once.
//
// The real code: ArtifactFrame.vue's own functions, declarations, its srcdoc
// computed, data watcher and srcdoc watcher, taken from its <script setup>,
// with the real HTML builder (utils/artifactIframe.ts) and payload helpers.
// Only the network and the frames are played. A frame "loads" whenever the
// srcdoc string it shows changes (as a browser reloads an iframe on a new
// srcdoc) and is loaded again when the test delivers its ARTIFACT_READY.

const require = createRequire(import.meta.url)
const read = (rel) => readFileSync(new URL(`../../${rel}`, import.meta.url), 'utf8')
const transpile = (code, module) => ts.transpileModule(code, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, ...(module ? { module: ts.ModuleKind.CommonJS } : {}) },
}).outputText
const load = (rel) => { const m = { exports: {} }; new Function('exports', 'module', 'require', transpile(read(rel), true))(m.exports, m, require); return m.exports }

globalThis.window ??= { location: { origin: 'http://app.test' } }
const { artifactRowCounts } = load('utils/artifactRowCounts.ts')
const framePayload = load('utils/artifactFramePayload.ts')
const { buildArtifactIframeHtml } = load('utils/artifactIframe.ts')

const source = read('components/dashboard/ArtifactFrame.vue')
const script = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const ast = ts.createSourceFile('frame.ts', script, ts.ScriptTarget.Latest, true)
const fn = (name) => ast.statements.find((s) => ts.isFunctionDeclaration(s) && s.name?.text === name)?.getText(ast)
const declaration = (name) => ast.statements.find((s) => ts.isVariableStatement(s)
  && s.declarationList.declarations.some((d) => d.name.getText(ast) === name))?.getText(ast)
const statement = (re) => ast.statements.filter((s) => ts.isExpressionStatement(s) && re.test(s.getText(ast))).map((s) => s.getText(ast))
const watchers = [
  ...statement(/^watch\(\[visualizationsData, filesData/),
  // Whatever the frame does when its srcdoc changes (absent before this change).
  ...statement(/^watch\(\(?\)? ?=?>? ?iframeSrcdoc/),
  ...statement(/^watch\(iframeSrcdoc\b/),
]
assert.ok(watchers.length >= 1, 'the frame re-sends its data when it changes (watcher)')

const FUNCTIONS = ['fetchData', 'runParamQueries', 'sendDataToIframe', 'paramSubsetForQuery', 'queriesWithIdentityParams', 'replaceVisualizationsData',
  'handleIframeMessage', 'frameContext', 'frameHasSeedData', 'frameIframeOptions', 'sameFrameInputs']
const DECLARED = ['useMyFetch', 'visualizationsData', 'visualizationsSendDue', 'srcdocSeed', 'seededFrom', 'builtSrcdoc', 'dataLoadsInFlight', 'loadSent',
  'iframeSrcdoc'].filter(declaration)
const present = FUNCTIONS.filter(fn)
const frameCode = "'use strict';\n" + transpile([
  ...DECLARED.map(declaration),
  ...present.map(fn),
  ...watchers,
  `return { ${[...DECLARED, ...present].join(', ')} };`,
].join('\n'))

const flush = async () => { for (let i = 0; i < 5; i++) await nextTick() }

const QUERIES = '/api/queries?report_id=rep-1&artifact_id=art-1'
const RUN_Q1 = '/api/queries/q1/run'
const queriesResponse = (rows = [{ region: 'EMEA', amount: 3 }, { region: 'APAC', amount: 5 }]) => [
  { id: 'q1', title: 'Sales', parameters: [{ name: 'region', source: 'input' }],
    visualizations: [{ id: 'viz-1', view: { type: 'bar' } }, { id: 'viz-2', view: { type: 'table' } }],
    default_step: { status: 'success', data: { rows, columns: [{ field: 'region' }, { field: 'amount' }] }, data_model: {}, code: '' } },
  { id: 'q2', title: 'Count', visualizations: [{ id: 'viz-3', view: { type: 'count' } }],
    default_step: { status: 'success', data: { rows: [{ n: 1 }], columns: [{ field: 'n' }] }, data_model: {}, code: '' } },
]
const report = { id: 'rep-1', title: 'Pipeline', theme_name: 'default', show_widget_info: false }
const REFRESHED = [{ region: 'LATAM', amount: 7 }]

function frame() {
  const responses = { [QUERIES]: queriesResponse(), '/api/reports/rep-1': report }
  // Each frame: what it was posted, and whether it was loading a new document then.
  const frames = {}
  const frameWindow = (name) => {
    const f = frames[name] = { received: [], loading: true, loads: 0 }
    return { postMessage(message) { f.received.push({ ...structuredClone(message), whileLoading: f.loading }) } }
  }
  const useMyFetch = async (url) => {
    if (!(url in responses)) throw new Error(`unexpected request ${url}`)
    return { data: ref(structuredClone(await responses[url])), error: ref(null) }
  }
  const state = {
    console: { ...console, log: () => {}, error: () => {} },
    computed, ref, shallowRef, toRaw, watch, nextTick,
    ...framePayload,
    useApplicationFetch: useMyFetch,
    $fetch: async () => { throw new Error('no preview broker here') },
    artifactRowCounts,
    buildArtifactIframeHtml,
    props: { reportId: 'rep-1', verificationPreview: false, prefetch: null },
    isLoading: ref(false), dataReady: ref(false), iframeReady: ref(false), fullscreenReady: ref(false),
    iframeRef: ref({ contentWindow: frameWindow('main') }),
    fullscreenRuntimeFrame: ref({ contentWindow: frameWindow('fullscreen') }),
    iframeError: ref(null),
    reportData: ref(null), reportSources: ref([]), filesData: ref([]),
    selectedArtifactId: ref('art-1'),
    selectedArtifact: ref({ id: 'art-1', artifact_id: 'dash-1', mode: 'page', content: { code: 'App', visualization_ids: ['viz-1', 'viz-2', 'viz-3'] } }),
    queryParamSpecs: ref({}), paramValues: ref({}), viewAsMode: ref('you'),
    snapshotWithheld: ref(false), withheldCharts: ref([]), ownerFailedCharts: ref(false), allWithheld: ref(false),
    viewerRunNoAccess: ref(false), viewerRunNeedsConnect: ref(false), hasOwnResult: ref(false), credentialScoped: ref(false),
    viewerRunFailedReason: ref(null),
    effectiveViewerContext: ref({ id: 'u-1', name: 'Viewer' }),
    verificationRequests: new Map(), verificationRevision: 0, previewConversationPolicy: null,
    paramAckSeq: 0, paramRunCounter: 0, latestParamRunForQid: {}, logActivity: null,
    verificationEvent: () => {}, handleTableLayoutMessage: () => false, postParamsStatus: () => {}, syncParamsToUrl: () => {}, readParamsFromUrl: () => ({}),
    watchHydration: () => {}, resolveParamOptions: async () => {}, fetchArtifactFiles: async () => [],
    whenArtifactLibsSettled: async () => {}, inlineableArtifactLibs: () => [], artifactLibSources: () => ({}),
    paramsPayload: () => ({ values: {} }), artifactRuntime: () => ({ version: 11 }),
    loadTableLayouts: () => ({}), tableLayoutKey: () => 'dash-1',
    // The srcdoc computed's other inputs.
    isDocMode: ref(false), showViewerGate: ref(false), slidesPreviewsMissing: ref(false), hasArtifact: ref(true),
    sampleArtifactCode: ref('Sample'), t: (k) => k, artifactColorMode: 'light',
  }
  const own = new Function(...Object.keys(state), frameCode)(...Object.values(state))
  // The page's template: each mounted frame shows the srcdoc; a new string reloads it.
  const shown = []
  watch(own.iframeSrcdoc, (html) => {
    shown.push(html)
    for (const f of Object.values(frames)) { f.loading = true; f.loads += 1 }
  }, { flush: 'sync', immediate: true })
  const ready = async (name = 'main') => {
    frames[name].loading = false
    const source = (name === 'main' ? state.iframeRef : state.fullscreenRuntimeFrame).value.contentWindow
    own.handleIframeMessage({ source, data: { type: 'ARTIFACT_READY' } })
    await flush()
  }
  const clear = () => { for (const f of Object.values(frames)) f.received.length = 0 }
  const types = (name = 'main') => frames[name].received.map((m) => m.type)
  return { own, state, responses, frames, shown, ready, clear, types }
}

// An open dashboard: loaded, its frame rendered from the seed and READY.
async function opened() {
  const f = frame()
  await f.own.fetchData('art-1')
  await flush()
  await f.ready()
  f.clear()
  return f
}

const refreshInPlace = async (f) => {
  f.responses[QUERIES] = queriesResponse(REFRESHED)
  await f.own.fetchData('art-1', { inPlace: true })
  await flush()
}

test('an in-place refresh keeps the frame\'s document and sends the refreshed rows once', async () => {
  const f = await opened()
  const html = f.own.iframeSrcdoc.value
  assert.ok(html && html.includes('<!DOCTYPE html>'), 'the frame shows the built document')
  const loads = f.frames.main.loads
  await refreshInPlace(f)
  assert.ok(f.own.iframeSrcdoc.value === html, 'the same srcdoc string: the frame is not reloaded')
  assert.equal(f.frames.main.loads, loads, 'no reload of the frame')
  assert.equal(f.state.iframeReady.value, true, 'the frame stays ready')
  assert.deepEqual(f.types(), ['ARTIFACT_DATA'], 'one push for one refresh')
  assert.deepEqual(f.frames.main.received[0].payload.visualizations.map((v) => v.rows), [REFRESHED, REFRESHED, [{ n: 1 }]], 'with the refreshed rows')
})

test('an in-place refresh with the fullscreen frame open sends each ready frame the rows once', async () => {
  const f = await opened()
  await f.ready('fullscreen')
  f.clear()
  await refreshInPlace(f)
  assert.deepEqual(f.types('main'), ['ARTIFACT_DATA'])
  assert.deepEqual(f.types('fullscreen'), ['ARTIFACT_DATA'])
  assert.deepEqual(f.frames.fullscreen.received[0].payload.visualizations[0].rows, REFRESHED)
})

test('new artifact code reloads the frame; nothing is posted to it until READY, then it gets the stale seed\'s refresh once', async () => {
  const f = await opened()
  await refreshInPlace(f)  // the seed is now stale: the refreshed rows exist only as a push
  f.clear()
  const html = f.own.iframeSrcdoc.value
  f.state.selectedArtifact.value = { ...f.state.selectedArtifact.value, content: { ...f.state.selectedArtifact.value.content, code: 'App2' } }
  assert.ok(f.own.iframeSrcdoc.value !== html, 'new code: a new document')
  assert.equal(f.state.iframeReady.value, false, 'the reloading frame is not ready')
  // Changes while it loads: a filter run, the files, another refresh.
  f.responses[RUN_Q1] = { status: 'success', data: { rows: [{ region: 'EMEA', amount: 9 }], columns: [] } }
  await f.own.runParamQueries({ region: 'EMEA' })
  f.state.filesData.value = [{ id: 'file-1', url: 'https://files.test/1' }]
  await flush()
  await refreshInPlace(f)
  assert.deepEqual(f.types(), [], 'no push targets the loading frame')
  await f.ready()
  assert.deepEqual(f.types(), ['ARTIFACT_DATA'], 'READY: one push of what the seed lacks')
  assert.equal(f.frames.main.received.some((m) => m.whileLoading), false)
  assert.deepEqual(f.frames.main.received[0].payload.visualizations[0].rows, REFRESHED)
})

test('the per-widget info toggle reloads the frame, which gets its data after READY', async () => {
  const f = await opened()
  const html = f.own.iframeSrcdoc.value
  f.state.reportData.value = { ...f.state.reportData.value, show_widget_info: true }
  assert.ok(f.own.iframeSrcdoc.value !== html, 'the toggle defines a new document')
  assert.equal(f.state.iframeReady.value, false)
  await f.ready()
  assert.deepEqual(f.types(), ['ARTIFACT_DATA'], 'the report changed since the seed: one push on READY')
  assert.equal(f.frames.main.received[0].payload.report.show_widget_info, true)
})

test('a full reload (new seed) while the frame is ready: no push into the reloading frame, READY finds the seed current', async () => {
  const f = await opened()
  const html = f.own.iframeSrcdoc.value
  f.responses[QUERIES] = queriesResponse(REFRESHED)
  await f.own.fetchData('art-1')
  await flush()
  assert.ok(f.own.iframeSrcdoc.value !== html, 'a new seed: a new document')
  assert.equal(f.frames.main.received.some((m) => m.whileLoading), false, 'nothing posted while it loads')
  assert.equal(f.state.iframeReady.value, false)
  await f.ready()
  assert.deepEqual(f.types(), ['ARTIFACT_DATA_CURRENT'], 'the new seed holds the data: no payload')
})

test('a filter run and new files after a refresh still reach the frame, once each', async () => {
  const f = await opened()
  await refreshInPlace(f)
  f.clear()
  f.responses[RUN_Q1] = { status: 'success', data: { rows: [{ region: 'EMEA', amount: 9 }], columns: [] } }
  await f.own.runParamQueries({ region: 'EMEA' })
  await flush()
  assert.deepEqual(f.types(), ['ARTIFACT_DATA'], 'the filter run sends once')
  f.clear()
  f.state.filesData.value = [{ id: 'file-1', url: 'https://files.test/1' }]
  await flush()
  assert.deepEqual(f.types(), ['ARTIFACT_DATA'], 'new files are sent once')
})

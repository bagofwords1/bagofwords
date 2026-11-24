import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import test from 'node:test'

import ts from 'typescript'
import { computed, isProxy, isReactive, nextTick, ref, shallowRef, toRaw } from 'vue'

// Run: node --test tests/unit/sharedDashboardDataNoCopies.mjs
// A shared dashboard's data reaches its frame without copies made on the page:
// the dashboard response is used as it came (no clone, never written to), the
// rows are not made deeply reactive, and the frame payload is not built through
// a JSON text round trip. What the frame receives keeps the same values as when
// it was built through JSON, and a payload the browser cannot copy falls back
// to the JSON copy (with one console warning), never to nothing.
//
// The real code: SharedDashboardView.vue's own functions and declarations,
// taken from its <script setup>, run against the real useMyFetch. Only the
// network ($fetch) and the frame (its contentWindow.postMessage, which copies
// like a browser does: structuredClone) are played. The script runs in strict
// mode, as the page's module does, so a write into frozen data throws.

const read = (rel) => readFileSync(new URL(`../../${rel}`, import.meta.url), 'utf8')
const transpile = (code, module) => ts.transpileModule(code, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, ...(module ? { module: ts.ModuleKind.CommonJS } : {}) },
}).outputText
const require = createRequire(import.meta.url)
const load = (rel) => { const m = { exports: {} }; new Function('exports', 'module', 'require', transpile(read(rel), true))(m.exports, m, require); return m.exports }
// The frame payload helpers the page imports (absent before this change).
const framePayload = () => (existsSync(new URL('../../utils/artifactFramePayload.ts', import.meta.url)) ? load('utils/artifactFramePayload.ts') : {})

const { artifactRowCounts } = load('utils/artifactRowCounts.ts')
const { awaitsViewerInput, isOwnSuccess } = load('utils/viewerInput.ts')

// The real useMyFetch; $fetch answers from `responses` (by URL) and records
// the options it was sent.
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
      const body = responses[request]
      return typeof body === 'function' ? body(opts) : body
    },
  }
  const exports = {}
  new Function('exports', ...Object.keys(stubs), transpile(read('composables/useMyFetch.ts'), true))(exports, ...Object.values(stubs))
  return exports.useMyFetch
}

// ── the page's script ──────────────────────────────────────────────────────
const view = read('components/dashboard/SharedDashboardView.vue')
const script = view.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)[1]
const ast = ts.createSourceFile('view.ts', script, ts.ScriptTarget.Latest, true)
const functions = new Map(ast.statements
  .filter((s) => ts.isFunctionDeclaration(s) && s.name)
  .map((s) => [s.name.text, s.getText(ast)]))
const declaration = (name) => ast.statements.find((s) => ts.isVariableStatement(s)
  && s.declarationList.declarations.some((d) => d.name.getText(ast) === name))?.getText(ast)
// The srcdoc seed is frozen inside onMounted: the expression it is set to.
let seedExpression = null
ast.forEachChild(function visit(node) {
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
    && node.left.getText(ast) === 'srcdocSeed.value' && !seedExpression) seedExpression = node.right.getText(ast)
  node.forEachChild(visit)
})
assert.ok(seedExpression, 'onMounted freezes the srcdoc seed')

// The page's functions this test runs, and those they reach (by name).
const ENTRY = ['loadVisualizationData', 'runParamQueries', 'pushArtifactData', 'postToArtifactIframe', 'fetchQueriesWithSteps', 'iframeOptions']
const STUBBED = new Set(['resolveParamOptions', 'schedulePendingPoll', 'embedSessionEndedOn', 'paramsPayload', 'artifactRuntime',
  'loadTableLayouts', 'chartOverlayScriptTag'])
const used = new Set()
const reach = (name) => {
  if (used.has(name) || STUBBED.has(name) || !functions.has(name)) return
  used.add(name)
  for (const other of functions.keys()) if (new RegExp(`\\b${other}\\b`).test(functions.get(name))) reach(other)
}
ENTRY.forEach(reach)
const DECLARED = ['visualizationsData', 'srcdocSeed', 'toolExecutions']
const pageCode = "'use strict';\n" + transpile([
  ...DECLARED.map(declaration),
  ...[...used].map((n) => functions.get(n)),
  `function frozenSeed() { return ${seedExpression}; }`,
  `return { ${[...DECLARED, ...ENTRY, 'frozenSeed'].join(', ')} };`,
].join('\n'))

const deepFreeze = (o) => { o = toRaw(o); if (o && typeof o === 'object' && !Object.isFrozen(o)) { Object.freeze(o); Object.values(o).forEach(deepFreeze) } return o }

// A page with its state, the network and the frame.
function page({ responses = {}, hostContext = null, viewer = { id: 'u-1', name: 'Viewer' }, params = { values: {} } } = {}) {
  const fetched = []
  const received = []
  const warnings = []
  const frame = {
    // What the browser does with a posted message: copies it, or throws.
    postMessage(message) { received.push(structuredClone(message)) },
  }
  const state = {
    console: { ...console, warn: (...args) => warnings.push(args), error: () => {} },
    computed, ref, shallowRef, toRaw, nextTick,
    useMyFetch: realUseMyFetch(responses, fetched),
    artifactRowCounts, awaitsViewerInput, isOwnSuccess,
    props: { source: 'dashboard', id: 'dash-1', hostContext },
    api: {
      queriesWithSteps: () => '/d/dash-1/queries_with_steps',
      paramRun: (qid) => `/d/dash-1/queries/${qid}/run`,
      queries: () => '/d/dash-1/queries',
      step: (qid) => `/d/dash-1/queries/${qid}/step`,
    },
    apiAuthorization: ref('app-jwt'),
    embedSessionEndedOn: () => false,
    initialQueries: null,
    servedAppliedParams: {},
    queryParamSpecs: ref({ q1: [{ name: 'region', source: 'input' }] }),
    paramValues: ref({}),
    paramAckSeq: 0,
    paramRunCounter: 0,
    latestParamRunForQid: {},
    ...framePayload(),
    resolveParamOptions: async () => {},
    schedulePendingPoll: () => {},
    paramsPayload: () => params,
    artifactRuntime: () => ({ version: 11 }),
    loadTableLayouts: () => ({ 'viz-1': { widths: { amount: 120 } } }),
    chartOverlayScriptTag: () => '',
    overlaysEnabled: ref(false),
    artifact: ref({ id: 'art-1', artifact_id: 'dash-1', mode: 'page', content: { code: 'App', visualization_ids: ['viz-1', 'viz-2'] } }),
    report: ref({ id: 'rep-1', title: 'Pipeline', theme_name: 'default', show_widget_info: false }),
    filesData: ref([]),
    viewerContext: ref(viewer),
    artifactIframeRef: ref({ contentWindow: frame }),
    artifactColorMode: 'light',
    viewerLastRunAt: ref(null), snapshotWithheld: ref(false), withheldCharts: ref([]), allWithheld: ref(false),
    viewerRunNoAccess: ref(false), viewerRunNeedsConnect: ref(false), viewerHasOwnSuccess: ref(false),
    showingAuthorData: ref(false), hasOwnResult: ref(false), viewerRunFailedReason: ref(null), pendingQueries: ref([]),
  }
  const own = new Function(...Object.keys(state), pageCode)(...Object.values(state))
  const context = { ...state, ...own }
  const get = (name) => own[name]
  return { context, get, fetched, received, warnings, frame }
}

// The dashboard response: two charts over one query (q1), one legacy chart
// with no visualization (q2: the page adds it without a queryId).
const rowsQ1 = () => [{ region: 'EMEA', amount: 3 }, { region: 'APAC', amount: 5 }]
const dashboardResponse = () => [
  { id: 'q1', title: 'Sales', parameters: [{ name: 'region', source: 'input' }],
    visualizations: [{ id: 'viz-1', title: 'By region', view: { type: 'bar' } }, { id: 'viz-2', view: { type: 'table' } }],
    default_step: { data: { rows: rowsQ1(), columns: [{ field: 'region' }, { field: 'amount' }], info: { total_rows: 2 } },
      data_model: { type: 'bar' }, code: 'select 1', applied_params: {} } },
  { id: 'q2', title: 'Legacy', visualizations: [],
    default_step: { view: { type: 'count' }, data: { rows: [{ n: 1 }], columns: [{ field: 'n' }] }, data_model: {}, code: '' } },
]
const QWS = '/d/dash-1/queries_with_steps'
const RUN_Q1 = '/d/dash-1/queries/q1/run'

// ── 1. the dashboard response is used as it came: no clone, never written to ──
test('the dashboard response is used as it came, never written to', async () => {
  const body = deepFreeze(dashboardResponse())
  const before = JSON.stringify(body)
  const { get, context, fetched } = page({ responses: {
    [QWS]: body,
    [RUN_Q1]: deepFreeze({ status: 'success', data: { rows: [{ region: 'EMEA', amount: 9 }], columns: [{ field: 'region' }, { field: 'amount' }] } }),
  } })
  await context.loadVisualizationData('art-1')
  const list = get('visualizationsData').value
  assert.deepEqual(list.map((v) => v.id), ['viz-1', 'viz-2', 'q2'], 'every chart is loaded')
  assert.equal(toRaw(list[0].rows), body[0].default_step.data.rows, 'the rows are the response\'s own rows (no clone)')
  assert.equal(fetched.find((f) => f.request === QWS).opts.noClone, undefined, 'the opt-out is not sent on to $fetch')
  context.paramValues.value = { region: 'EMEA' }
  await context.runParamQueries({ region: 'EMEA' })
  context.pushArtifactData()
  context.frozenSeed()
  assert.equal(JSON.stringify(body), before, 'loading, a filter run and the frame payloads never write into the response')
})

// ── 2. the dashboard data is not deeply reactive; the seed neither ───────────
test('the dashboard data and the srcdoc seed are plain, not deeply reactive', async () => {
  const { get, context } = page({ responses: { [QWS]: dashboardResponse() } })
  await context.loadVisualizationData('art-1')
  const list = get('visualizationsData').value
  assert.equal(isReactive(list), false, 'the list is not wrapped in a reactive proxy')
  assert.equal(isProxy(list[0]) || isProxy(list[0].rows) || isProxy(list[0].rows[0]), false, 'nor its charts, rows or values')
  get('srcdocSeed').value = context.frozenSeed()
  const seed = get('srcdocSeed').value
  assert.equal(isProxy(seed) || isProxy(seed.visualizations[0].rows), false, 'the srcdoc seed is plain data')
  const data = context.iframeOptions(context.artifact.value, seed).data
  assert.equal(isProxy(data.visualizations[0].rows) || isProxy(data.visualizations[0].rows[0]), false,
    'buildArtifactIframeHtml is handed plain rows (its JSON.stringify walks no proxies)')
})

// ── 3. a filter run and a refresh replace the data, never edit it in place ──
// The loaded charts and rows are frozen: an edit in place throws.
test('a filter run and a refresh replace frozen data; readers see both', async () => {
  const fresh = { status: 'success', data: { rows: [{ region: 'EMEA', amount: 9 }], columns: [{ field: 'region' }, { field: 'amount' }], info: { total_rows: 40 } } }
  const responses = { [QWS]: dashboardResponse(), [RUN_Q1]: fresh }
  const { get, context } = page({ responses })
  await context.loadVisualizationData('art-1')
  const data = get('visualizationsData')
  data.value = data.value.map((v) => deepFreeze(toRaw(v)))  // (test setup: freeze what was loaded)
  const loaded = data.value
  // Readers: the Data tab's charts (toolExecutions) and the list itself.
  const toolExecutions = get('toolExecutions')
  assert.deepEqual(toolExecutions.value[0].created_step.data.rows, rowsQ1())

  context.paramValues.value = { region: 'EMEA' }
  await context.runParamQueries({ region: 'EMEA' })
  assert.notEqual(data.value, loaded, 'the filter run replaced the list')
  assert.deepEqual(data.value.filter((v) => v.queryId === 'q1').map((v) => v.rows), [fresh.data.rows, fresh.data.rows],
    'both charts of the query carry the filtered rows')
  assert.deepEqual(data.value.map((v) => [v.row_count, v.loaded_row_count, v.truncated]),
    [[40, 1, true], [40, 1, true], [1, 1, false]], 'with their counts')
  assert.equal(data.value[2], loaded[2], 'a chart the run did not touch is kept as it was')
  assert.deepEqual(loaded[0].rows, rowsQ1(), 'the old charts were not edited')
  assert.deepEqual(toolExecutions.value.map((t) => t.created_step.data.rows.length), [1, 1, 1], 'the Data tab sees the filtered rows')
  assert.equal(get('visualizationsData').value.length, 3, 'the Data tab count and menu see the same list')

  // A refresh (refreshOnView, a viewer run, a landed pending chart) reloads.
  const reloaded = dashboardResponse()
  reloaded[0].default_step.data.rows = [{ region: 'LATAM', amount: 7 }]
  responses[QWS] = reloaded
  const beforeRefresh = data.value
  await context.loadVisualizationData('art-1')
  assert.notEqual(data.value, beforeRefresh, 'a refresh replaces the list')
  assert.deepEqual(toolExecutions.value[0].created_step.data.rows, [{ region: 'LATAM', amount: 7 }], 'and readers see the refreshed rows')
})

// ── 4. the frame payload: same values as the JSON copy, no copy made here ──
// Page-added fields with undefined and NaN, a host context and param values
// a JSON copy changes (Date → string, NaN → null, undefined → missing).
test('the frame payload and srcdoc seed keep the JSON copy\'s values without copying rows', async () => {
  const hostContext = { record: 'r-1', missing: undefined, ratio: NaN, at: new Date('2026-11-24T10:00:00Z'), nested: [1, undefined, NaN] }
  const params = { declarations: [{ name: 'region', query_ids: ['q1'], default: undefined }], values: { region: 'EMEA', top: NaN }, options: {}, ack: 0 }
  const { get, context, received } = page({ responses: { [QWS]: dashboardResponse() }, hostContext, params })
  await context.loadVisualizationData('art-1')
  const data = get('visualizationsData')
  // The page's own extras on a chart: a field it set undefined, and a NaN.
  data.value = data.value.map((v, i) => (i === 0 ? { ...v, subtitle: undefined, ratio: NaN, view: { ...v.view, color: undefined } } : v))
  const raw = data.value

  // What the old page built: everything through one JSON copy.
  const oldPayload = JSON.parse(JSON.stringify({
    report: { id: 'rep-1', title: 'Pipeline', theme: 'default' },
    visualizations: raw, files: [], current_user: { id: 'u-1', name: 'Viewer' }, host_context: hostContext,
    params, runtime: { version: 11 }, table_layouts: { 'viz-1': { widths: { amount: 120 } } },
  }))
  const oldSeed = JSON.parse(JSON.stringify({ ...oldPayload, table_layouts: undefined }))

  context.pushArtifactData()
  assert.equal(received.length, 1)
  assert.equal(received[0].type, 'ARTIFACT_DATA')
  assert.deepStrictEqual(received[0].payload, oldPayload, 'the frame receives the same values as from the JSON copy')
  assert.deepStrictEqual(structuredClone(received[0]), received[0], 'and the payload survives structuredClone')

  // Nothing copied on the page: the payload posted holds the page's own rows.
  let posted = null
  context.artifactIframeRef.value = { contentWindow: { postMessage(m) { posted = m; structuredClone(m) } } }
  context.pushArtifactData()
  assert.equal(posted.payload.visualizations[0].rows, toRaw(raw[0].rows), 'the rows are handed to postMessage as they are (no JSON round trip)')

  const seed = context.frozenSeed()
  assert.deepStrictEqual(structuredClone(seed), oldSeed, 'the srcdoc seed holds the same values as the JSON-copied seed')
  assert.equal(JSON.stringify(seed), JSON.stringify(oldSeed), 'and the same text in the frame\'s HTML')
  assert.equal(seed.visualizations[0].rows, toRaw(raw[0].rows), 'without a copy of the rows')
})

// ── 5. a payload the browser refuses falls back to the JSON copy, once warned ─
test('a payload postMessage refuses falls back to the JSON copy, with one warning', async () => {
  const { get, context, received, warnings } = page({ responses: { [QWS]: dashboardResponse() } })
  await context.loadVisualizationData('art-1')
  const data = get('visualizationsData')
  // A value structuredClone cannot copy (a function) inside the rows.
  data.value = data.value.map((v, i) => (i === 0 ? { ...v, rows: [{ region: 'EMEA', amount: 3, format: () => 'x' }] } : v))
  const warn = console.warn  // (the page's or its helper's warning)
  console.warn = (...args) => warnings.push(args)
  try {
    context.pushArtifactData()
    context.pushArtifactData()
  } finally { console.warn = warn }
  assert.equal(received.length, 2, 'both pushes reach the frame')
  assert.deepStrictEqual(received[0].payload.visualizations[0].rows, [{ region: 'EMEA', amount: 3 }], 'as the JSON copy (the function dropped)')
  assert.equal(warnings.length, 1, 'one console warning for the fallback')
})

// ── 6. useMyFetch still clones for every caller without the opt-out ──────────
test('useMyFetch clones for every caller but the one that opts out', async () => {
  const body = { rows: [{ a: 1 }] }
  const fetched = []
  const useMyFetch = realUseMyFetch({ '/x': body }, fetched)
  const cloned = await useMyFetch('/x')
  assert.deepEqual(toRaw(cloned.data.value), body)
  assert.notEqual(toRaw(cloned.data.value), body, 'a caller without the opt-out gets its own copy')
  const shared = await useMyFetch('/x', { noClone: true })
  assert.equal(toRaw(shared.data.value), body, 'the opt-out gets the body as it came')
  assert.equal('noClone' in fetched[1].opts, false, 'and $fetch is not sent the opt-out')
})

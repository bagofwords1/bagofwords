import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import ts from 'typescript'

// Run: node --test tests/unit/artifactSeedRowsOnce.mjs
// Charts of one query hold one rows array in the payload the dashboard hands
// to its frame (artifactFramePayload without separateRows). The frame's srcdoc
// seed (utils/artifactIframe.ts) writes those rows once, not once per chart;
// the seed's own data script gives every other chart its own copy before any
// other script runs, so window.ARTIFACT_DATA is what the JSON seed gave before:
// the same values in the same key order, a separate rows array per chart.
//
// The real code: buildArtifactIframeHtml and the data script in the HTML it
// builds, run as the frame runs it. Only the window is played.

const read = (rel) => readFileSync(new URL(`../../${rel}`, import.meta.url), 'utf8')
globalThis.window = { location: { origin: 'http://app.test' } }
const iframeModule = { exports: {} }
new Function('exports', 'module', ts.transpileModule(read('utils/artifactIframe.ts'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText)(iframeModule.exports, iframeModule)
const { buildArtifactIframeHtml } = iframeModule.exports

const APP = '<script type="text/babel">render()</script>'
const SCRIPT_END = '</' + 'script>'

// The inline script that sets window.ARTIFACT_DATA, as the HTML parser cuts it
// (at the first script end tag).
function dataScript(html) {
  const at = html.indexOf('window.ARTIFACT_DATA = ')
  assert.ok(at > 0, 'the seed sets window.ARTIFACT_DATA')
  const start = html.lastIndexOf('<script>', at) + '<script>'.length
  return html.slice(start, html.indexOf(SCRIPT_END, at))
}

// Runs the data script as the frame does; returns the frame's window.
function runSeed(data, { clone = structuredClone, json = JSON, cons = quietConsole().cons, win = {} } = {}) {
  new Function('window', 'structuredClone', 'JSON', 'console', dataScript(buildArtifactIframeHtml({ code: APP, data, showWidgetInfo: true })))(win, clone, json, cons)
  return win
}

// A console that records what the seed reports.
function quietConsole() {
  const warns = []
  const errors = []
  return { warns, errors, cons: { warn: (...a) => { warns.push(a) }, error: (...a) => { errors.push(a) } } }
}

// What the rest of the data script sets after ARTIFACT_DATA.
function assertLaterGlobalsSet(win) {
  assert.equal(typeof win.__BOW_RUNTIME_NONCE__, 'string')
  assert.equal(win.__BOW_RUNTIME_NONCE__.length, 32)
  assert.equal(win.__BOW_FIXTURE_MODE__, false)
  assert.equal(win.__BOW_INFO, true)
}

const count = (text, needle) => text.split(needle).length - 1

// Two queries: A shown by 3 charts, B by 2 (one rows array per query).
function dashboard() {
  const rowsA = Array.from({ length: 50 }, (_, i) => ({ region: `region-a-${i}`, amount: i * 10 }))
  const rowsB = Array.from({ length: 40 }, (_, i) => ({ name: `name-b-${i}`, total: i }))
  const viz = (id, rows, query) => ({ id, title: `Chart ${id}`, query_id: query, rows, columns: [{ field: 'x' }], row_count: rows.length, info: { total_rows: rows.length } })
  return {
    data: {
      runtime: { version: 11 },
      visualizations: [viz('a1', rowsA, 'qa'), viz('b1', rowsB, 'qb'), viz('a2', rowsA, 'qa'), viz('a3', rowsA, 'qa'), viz('b2', rowsB, 'qb')],
      table_layouts: {},
    },
    rowsA,
    rowsB,
  }
}

test('rows a query\'s charts share are written once in the seed', () => {
  const { data, rowsA, rowsB } = dashboard()
  const script = dataScript(buildArtifactIframeHtml({ code: APP, data }))
  assert.equal(count(script, '"region-a-7"'), 1, 'query A rows written once (3 charts)')
  assert.equal(count(script, '"name-b-7"'), 1, 'query B rows written once (2 charts)')
  // The data statement alone, without the other globals the script sets.
  const statement = script.slice(script.indexOf('window.ARTIFACT_DATA = '), script.indexOf('window.__BOW_RUNTIME_NONCE__'))
  const perChart = `window.ARTIFACT_DATA = ${JSON.stringify(data)};`.length
  const repeats = 2 * JSON.stringify(rowsA).length + JSON.stringify(rowsB).length
  // The slack is the re-attach code (its copy fallbacks and console warnings).
  assert.ok(statement.length < perChart - repeats + 1000,`data statement ${statement.length} bytes, per-chart JSON ${perChart}, repeats ${repeats}`)
})

test('the frame gets the same data, in the same key order, as the JSON seed gave', () => {
  const { data } = dashboard()
  const win = runSeed(data)
  assert.equal(JSON.stringify(win.ARTIFACT_DATA), JSON.stringify(data))
  assert.deepEqual(win.ARTIFACT_DATA, JSON.parse(JSON.stringify(data)))
})

test('every chart has its own rows: an in-place sort, reverse or row write in one leaves the others', () => {
  const { data } = dashboard()
  const vis = runSeed(data).ARTIFACT_DATA.visualizations
  const [a1, b1, a2, a3, b2] = vis
  assert.notEqual(a1.rows, a2.rows)
  assert.notEqual(a2.rows, a3.rows)
  assert.notEqual(a1.rows, a3.rows)
  assert.notEqual(b1.rows, b2.rows)
  assert.notEqual(a1.rows[0], a2.rows[0], 'row objects are separate too')
  const before = JSON.stringify(vis)
  a2.rows.sort((x, y) => y.amount - x.amount)
  b2.rows.reverse()
  a3.rows[0].amount = -1
  a3.rows.push({ region: 'added', amount: 0 })
  assert.equal(a1.rows[0].amount, 0, 'first chart of A untouched by the sort')
  assert.equal(a1.rows.length, 50)
  assert.equal(a2.rows.at(-1).amount, 0, 'the sorted chart is sorted')
  assert.equal(a2.rows.length, 50, 'no push leaked into the sorted chart')
  assert.equal(b1.rows[0].name, 'name-b-0', 'first chart of B untouched by the reverse')
  const unchanged = JSON.parse(before)
  assert.deepEqual(a1, unchanged[0])
  assert.deepEqual(b1, unchanged[1])
})

test('charts without shared rows are left as they are and not linked', () => {
  const sharedEmpty = []
  const rowsA = [{ n: 1 }, { n: 2 }]
  const data = {
    visualizations: [
      { id: 'a1', rows: rowsA },
      { id: 'withheld-1', rows: [], unavailable: true },
      { id: 'withheld-2', rows: [], unavailable: true },
      { id: 'no-rows' },
      { id: 'null-rows', rows: null },
      { id: 'text-rows', rows: 'not an array' },
      { id: 'object-rows', rows: { n: 1 } },
      null,
      { id: 'a2', rows: rowsA },
      { id: 'e1', rows: sharedEmpty },
      { id: 'e2', rows: sharedEmpty },
    ],
  }
  let clones = 0
  const win = runSeed(data, { clone: (v) => { clones += 1; return structuredClone(v) } })
  assert.equal(JSON.stringify(win.ARTIFACT_DATA), JSON.stringify(data))
  assert.equal(clones, 2, 'only the second chart of A and the second holder of the one empty array are copied')
  const vis = win.ARTIFACT_DATA.visualizations
  assert.notEqual(vis[1].rows, vis[2].rows, 'withheld charts keep their own empty arrays')
  assert.ok(!('rows' in vis[3]), 'a chart without rows gets none')
  assert.equal(vis[4].rows, null)
  assert.equal(vis[5].rows, 'not an array')
  assert.notEqual(vis[9].rows, vis[10].rows, 'one empty array shared by identity still ends up as two')

  // Without one shared array the seed is exactly the JSON seed.
  const distinct = { visualizations: data.visualizations.slice(1, 8) }
  assert.equal(dataScript(buildArtifactIframeHtml({ code: APP, data: distinct })).split(';window.__BOW_RUNTIME_NONCE__')[0].split('window.ARTIFACT_DATA = ')[1],
    JSON.stringify(distinct).replace(/</g, '\\u003c'))
})

test('row text cannot end the script; it reaches the frame unchanged', () => {
  const hostile = [
    { label: '</script><script>window.pwned=1</script>', note: '<!-- <script>' },
    { label: 'line sep para', note: '</SCRIPT >' },
    { '</script>': 'key', note: '<\/script>' },
  ]
  const data = { visualizations: [{ id: 'h1', rows: hostile, title: '<b>x</b>' }, { id: 'h2', rows: hostile }] }
  const html = buildArtifactIframeHtml({ code: APP, data })
  const script = dataScript(html)
  assert.ok(!script.includes('<'), 'no < inside the data script')
  const win = runSeed(data)
  assert.equal(win.pwned, undefined)
  assert.equal(JSON.stringify(win.ARTIFACT_DATA), JSON.stringify(data))
  assert.equal(win.ARTIFACT_DATA.visualizations[1].rows[1].label, 'line sep para')
})

test('a seed with nothing repeated is the JSON seed, byte for byte', () => {
  for (const data of [
    { visualizations: [{ id: 'a', rows: [{ n: 1 }] }, { id: 'b', rows: [{ n: 1 }] }], runtime: { version: 11 } },
    { visualizations: [] },
    { visualizations: 'not a list' },
    { runtime: { version: 11 } },
  ]) {
    const script = dataScript(buildArtifactIframeHtml({ code: APP, data }))
    assert.ok(script.includes(`window.ARTIFACT_DATA = ${JSON.stringify(data).replace(/</g, '\\u003c')};window.__BOW_RUNTIME_NONCE__=`), JSON.stringify(data))
  }
})

test('without structuredClone, or when it throws, each chart still gets its own copy', () => {
  // null, not undefined: undefined would pick runSeed's default (the real one).
  for (const clone of [null, () => { throw new Error('DataCloneError') }]) {
    const { data } = dashboard()
    const { warns, errors, cons } = quietConsole()
    const vis = runSeed(data, { clone, cons }).ARTIFACT_DATA.visualizations
    assert.equal(JSON.stringify(vis), JSON.stringify(data.visualizations))
    assert.notEqual(vis[0].rows, vis[2].rows)
    assert.notEqual(vis[0].rows[0], vis[2].rows[0])
    // Three charts were copied as JSON; the frame says so once.
    assert.equal(warns.length, 1, 'one warning per frame, not per chart')
    assert.match(String(warns[0][0]), /JSON copy/)
    assert.equal(errors.length, 0)
  }
})

test('a copied seed is silent', () => {
  const { data } = dashboard()
  const { warns, errors, cons } = quietConsole()
  runSeed(data, { cons })
  assert.equal(warns.length, 0)
  assert.equal(errors.length, 0)
})

test('when no copy can be made the chart shares the first chart\'s rows rather than having none', () => {
  const { data } = dashboard()
  const failingJson = { parse: JSON.parse, stringify: () => { throw new Error('out of memory') } }
  const { warns, errors, cons } = quietConsole()
  const win = runSeed(data, { clone: () => { throw new Error('DataCloneError') }, json: failingJson, cons })
  const vis = win.ARTIFACT_DATA.visualizations
  assert.equal(JSON.stringify(win.ARTIFACT_DATA), JSON.stringify(data))
  assert.equal(vis[2].rows, vis[0].rows, 'last resort: the source array itself')
  assert.equal(warns.length, 1, 'three charts share rows; the frame says so once')
  assert.match(String(warns[0][0]), /share/)
  assert.equal(errors.length, 0)
  assertLaterGlobalsSet(win)
})

test('a console that throws cannot stop the seed', () => {
  const { data } = dashboard()
  const failingJson = { parse: JSON.parse, stringify: () => { throw new Error('out of memory') } }
  const boom = () => { throw new Error('console unavailable') }
  const win = runSeed(data, { clone: () => { throw new Error('DataCloneError') }, json: failingJson, cons: { warn: boom, error: boom } })
  assert.equal(JSON.stringify(win.ARTIFACT_DATA), JSON.stringify(data))
  assertLaterGlobalsSet(win)
})

test('a re-attach that throws is reported; the rest of the data script still runs', () => {
  // The frame's visualizations cannot be read at all.
  const { data } = dashboard()
  const { warns, errors, cons } = quietConsole()
  let stored
  const win = {}
  Object.defineProperty(win, 'ARTIFACT_DATA', {
    set(d) { stored = d },
    get() { return { ...stored, get visualizations() { throw new Error('unreadable') } } },
  })
  runSeed(data, { cons, win })
  assert.equal(errors.length, 1, 'the failure is logged')
  assert.equal(warns.length, 0)
  assertLaterGlobalsSet(win)
})

test('when re-attach fails part way, the charts left share the source rows rather than having none', () => {
  // The third chart of query A refuses its rows; the charts after it still get theirs.
  const { data } = dashboard()
  const { errors, cons } = quietConsole()
  let stored
  const win = {}
  Object.defineProperty(win, 'ARTIFACT_DATA', {
    set(d) {
      stored = d
      Object.defineProperty(d.visualizations[3], 'rows', { get: () => null, set: () => { throw new Error('read-only') } })
    },
    get() { return stored },
  })
  runSeed(data, { cons, win })
  const [a1, b1, a2, a3, b2] = stored.visualizations
  assert.notEqual(a2.rows, a1.rows, 'the chart before the failure has its own copy')
  assert.equal(JSON.stringify(a2.rows), JSON.stringify(a1.rows))
  assert.equal(a3.rows, null, 'the refusing chart is left as it is')
  assert.equal(b2.rows, b1.rows, 'the chart after the failure shares the source rows')
  assert.equal(errors.length, 1)
  assertLaterGlobalsSet(win)
})

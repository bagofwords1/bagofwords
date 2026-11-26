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
function runSeed(data, { clone = structuredClone, json = JSON } = {}) {
  const win = {}
  new Function('window', 'structuredClone', 'JSON', dataScript(buildArtifactIframeHtml({ code: APP, data })))(win, clone, json)
  return win
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
  assert.ok(statement.length < perChart - repeats + 400, `data statement ${statement.length} bytes, per-chart JSON ${perChart}, repeats ${repeats}`)
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
  for (const clone of [undefined, () => { throw new Error('DataCloneError') }]) {
    const { data } = dashboard()
    const vis = runSeed(data, { clone }).ARTIFACT_DATA.visualizations
    assert.equal(JSON.stringify(vis), JSON.stringify(data.visualizations))
    assert.notEqual(vis[0].rows, vis[2].rows)
    assert.notEqual(vis[0].rows[0], vis[2].rows[0])
  }
})

test('when no copy can be made the chart shares the first chart\'s rows rather than having none', () => {
  const { data } = dashboard()
  const failingJson = { parse: JSON.parse, stringify: () => { throw new Error('out of memory') } }
  const win = runSeed(data, { clone: () => { throw new Error('DataCloneError') }, json: failingJson })
  const vis = win.ARTIFACT_DATA.visualizations
  assert.equal(JSON.stringify(win.ARTIFACT_DATA), JSON.stringify(data))
  assert.equal(vis[2].rows, vis[0].rows, 'last resort: the source array itself')
})

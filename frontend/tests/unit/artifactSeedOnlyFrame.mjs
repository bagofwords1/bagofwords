import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import ts from 'typescript'

// Run: node --test tests/unit/artifactSeedOnlyFrame.mjs
// The in-app dashboard frame renders from its srcdoc seed, and when nothing
// changed while it loaded the host sends no ARTIFACT_DATA after ARTIFACT_READY
// (components/dashboard/ArtifactFrame.vue). What that push used to do in the
// frame still happens without it:
//  - data listeners (bow.data.subscribe, useArtifactData) fire once when the
//    frame reports ready, as they did for the push; a later push still
//    notifies; the verification preview (always pushed) and other hosts that
//    pass no option are not notified at ready;
//  - saving one DataTable's layout keeps the other tables' layouts from the
//    seed; a push still replaces the layouts as before.
//
// The real code: the frame's live data store, DataTable layout store and data
// message handler from public/libs/artifact-globals.js, the ready script from
// the HTML that utils/artifactIframe.ts builds with ArtifactFrame.vue's own
// frame options. Only the window, document and timers are played (this
// environment has no vendored React to render a whole frame).

const read = (rel) => readFileSync(new URL(`../../${rel}`, import.meta.url), 'utf8')
const transpile = (code, module) => ts.transpileModule(code, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, ...(module ? { module: ts.ModuleKind.CommonJS } : {}) },
}).outputText

// ── the frame runtime (artifact-globals.js) ─────────────────────────────────
const globalsSrc = read('public/libs/artifact-globals.js')
const gAst = ts.createSourceFile('globals.js', globalsSrc, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
const gFunctions = {}
const gStatements = []
;(function walk(node) {
  if (ts.isFunctionDeclaration(node) && node.name) gFunctions[node.name.text] = node.getText(gAst)
  if (ts.isExpressionStatement(node)) gStatements.push(node.getText(gAst))
  ts.forEachChild(node, walk)
})(gAst)
const assignment = (target) => gStatements.find((s) => s.startsWith(`${target} =`))
const dataMessageListener = gStatements.find((s) => s.startsWith("window.addEventListener('message'")
  && s.includes("e.data.type === 'ARTIFACT_DATA'"))
assert.ok(dataMessageListener, 'the frame handles ARTIFACT_DATA messages')
const runtimeCode = [
  assignment('window.__artifactDataListeners'),
  gFunctions.notifyArtifactData,
  assignment('window.__notifyArtifactData'),
  assignment('window.__setArtifactData'),
  gFunctions._dtSavedLayout,
  gFunctions._dtSaveLayout,
  dataMessageListener,
  'return { _dtSavedLayout, _dtSaveLayout };',
].filter(Boolean).join('\n')

// ── the host's frame options and HTML (ArtifactFrame.vue, artifactIframe.ts) ─
const vue = read('components/dashboard/ArtifactFrame.vue')
const vAst = ts.createSourceFile('frame.ts', vue.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1], ts.ScriptTarget.Latest, true)
const frameOptionsFn = vAst.statements.find((s) => ts.isFunctionDeclaration(s) && s.name?.text === 'frameIframeOptions').getText(vAst)
const frameIframeOptions = (verificationPreview) => new Function(
  'selectedArtifact', 'props', 't', 'reportData', 'artifactColorMode',
  `${transpile(frameOptionsFn)}\nreturn frameIframeOptions;`,
)({ value: { mode: 'page', content: {} } }, { verificationPreview }, (k) => k, { value: {} }, 'light')(seed(), APP)
const APP = '<script type="text/babel">render()</script>'

globalThis.window = { location: { origin: 'http://app.test' } }
const iframeModule = { exports: {} }
new Function('exports', 'module', transpile(read('utils/artifactIframe.ts'), true))(iframeModule.exports, iframeModule)
const { buildArtifactIframeHtml } = iframeModule.exports

// The script the frame runs once its first render is on screen.
function readyScript(options) {
  const html = buildArtifactIframeHtml({ code: APP, ...options })
  const start = html.lastIndexOf('// After Babel processes')
  assert.ok(start > 0, 'the frame HTML carries its ready script')
  return html.slice(start, html.indexOf('</' + 'script>', start))
}

// One frame: a seeded window with the runtime loaded, its parent's inbox.
function frame(seed) {
  const posted = []
  const handlers = {}
  const parent = { postMessage: (message) => posted.push(structuredClone(message)) }
  const win = {
    ARTIFACT_DATA: seed,
    parent,
    requestAnimationFrame: (fn) => fn(),
    addEventListener: (type, fn) => (handlers[type] ||= []).push(fn),
    __paramStore: { _ingest() {}, _status() {} },
  }
  const runtime = new Function('window', runtimeCode)(win)
  const dispatch = (type, event = {}) => (handlers[type] || []).forEach((fn) => fn(event))
  // What bow.data.subscribe (artifact-sdk.js) does with a callback.
  const subscribe = (fn) => win.__artifactDataListeners.push(fn)
  const runReady = (options) => {
    const document = { getElementById: (id) => (id === 'root' ? { children: [{}] } : null) }
    new Function('window', 'document', 'setTimeout', 'reportArtifactError', readyScript(options))(
      win, document, (fn) => fn(), (msg) => posted.push({ type: 'ARTIFACT_ERROR', msg }))
    dispatch('DOMContentLoaded')
  }
  const push = (payload) => dispatch('message', { source: parent, data: { type: 'ARTIFACT_DATA', payload } })
  return { win, posted, runtime, subscribe, runReady, push }
}

const seed = () => ({
  runtime: { version: 11 },
  visualizations: [{ id: 'viz-a', rows: [{ n: 1 }] }],
  table_layouts: {
    'viz-a': { widths: { region: 140 }, order: ['region', 'amount'] },
    'viz-b': { widths: { name: 220 }, order: ['name', 'total'] },
  },
})

// ── data listeners on a seed-only open ──────────────────────────────────────
test('an in-app open with nothing pushed: subscribe callbacks fire once when the frame reports ready', () => {
  const f = frame(seed())
  const seen = []
  f.subscribe(() => seen.push(f.win.ARTIFACT_DATA))
  f.runReady(frameIframeOptions(false))
  assert.equal(seen.length, 1, 'the callback fires once, as it did for the push after ready')
  assert.deepEqual(seen[0].visualizations, seed().visualizations, 'with the seed as the data')
  assert.deepEqual(f.posted.map((m) => m.type), ['ARTIFACT_READY'], 'the frame reports ready and acks no data revision')
})

test('a push after ready still notifies, once more', () => {
  const f = frame(seed())
  let calls = 0
  f.subscribe(() => { calls += 1 })
  f.runReady(frameIframeOptions(false))
  f.push({ ...seed(), visualizations: [{ id: 'viz-a', rows: [{ n: 2 }] }] })
  assert.equal(calls, 2, 'ready, then the push')
  assert.deepEqual(f.win.ARTIFACT_DATA.visualizations[0].rows, [{ n: 2 }], 'the pushed data is live')
})

test('the verification preview is not notified at ready; its push notifies and acks its revision', () => {
  const f = frame(seed())
  let calls = 0
  f.subscribe(() => { calls += 1 })
  f.runReady(frameIframeOptions(true))
  assert.equal(calls, 0, 'nothing at ready (the preview is always pushed its data)')
  f.push({ ...seed(), verification_revision: 1 })
  assert.equal(calls, 1, 'one notification, from the push')
  assert.deepEqual(f.posted.map((m) => m.type), ['ARTIFACT_READY', 'ARTIFACT_DATA_RECEIVED'])
})

test('a host that passes no option (the shared page) is not notified at ready, as before', () => {
  const f = frame(seed())
  let calls = 0
  f.subscribe(() => { calls += 1 })
  f.runReady({ data: seed(), code: APP })
  assert.equal(calls, 0)
  assert.deepEqual(f.posted.map((m) => m.type), ['ARTIFACT_READY'])
})

// ── DataTable layouts on a seed-only open ───────────────────────────────────
test('saving one table\'s layout keeps the other tables\' layouts from the seed', () => {
  const f = frame(seed())
  const moved = { widths: { region: 180 }, order: ['amount', 'region'] }
  f.runtime._dtSaveLayout('viz-a', moved)
  assert.deepEqual(f.posted.at(-1), { type: 'ARTIFACT_TABLE_LAYOUT', key: 'viz-a', ...moved }, 'the host is told to store it')
  assert.deepEqual(f.runtime._dtSavedLayout('viz-a'), moved, 'the saved table has its new layout')
  assert.deepEqual(f.runtime._dtSavedLayout('viz-b'), seed().table_layouts['viz-b'], 'the other table keeps its seeded layout')
  // A later save of the other table starts from its real layout, not an empty one.
  const widened = { ...f.runtime._dtSavedLayout('viz-b'), widths: { name: 260 } }
  f.runtime._dtSaveLayout('viz-b', widened)
  assert.deepEqual(f.posted.at(-1).order, ['name', 'total'], 'its saved order is kept')
  assert.deepEqual(f.runtime._dtSavedLayout('viz-a'), moved, 'and the first table keeps its own')
  assert.deepEqual(f.win.ARTIFACT_DATA.table_layouts, seed().table_layouts, 'the seed itself is not written to')
})

test('a push replaces the saved layouts, as before', () => {
  const f = frame(seed())
  f.runtime._dtSaveLayout('viz-a', { widths: { region: 180 }, order: [] })
  f.push({ ...seed(), table_layouts: { 'viz-b': { widths: { name: 90 }, order: [] } } })
  assert.equal(f.runtime._dtSavedLayout('viz-a'), null, 'the pushed layouts are the whole set')
  assert.deepEqual(f.runtime._dtSavedLayout('viz-b'), { widths: { name: 90 }, order: [] })
})

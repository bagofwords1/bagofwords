// Real Chromium: the chat building blocks in artifact-globals.js.
//
// ChatResult renders one conversation result (the table always, the chart only
// for a hint that names real columns, the truncation note), ChatMessage renders
// one message (markdown with dir="auto" per block, tool steps, results, the
// incomplete note and the in-progress indicator), and useConversation drives
// them from bow.conversations (here in the SDK's fixture mode).
//
//   cd frontend && PW_CHROMIUM_PATH=/path/to/chrome node tests/unit/artifactChatComponents.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';

const root = new URL('../../', import.meta.url);
if (!fs.existsSync(new URL('public/libs/babel-standalone.min.js', root))) {
  console.log('SKIP: vendored libs missing (scripts/download-vendor-libs.sh)');
  process.exit(0);
}
const source = fs.readFileSync(new URL('utils/artifactIframe.ts', root), 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
const executablePath = process.env.PW_CHROMIUM_PATH || process.env.BOW_CHROMIUM_EXECUTABLE || undefined;

// ── harness ─────────────────────────────────────────────────────────────────
// The test artifact is a plain script (no Babel) that runs after the globals and
// the SDK. It wraps echarts.init so every chart instance records its container's
// [data-vector], its init count and every setOption option.
const SPY = `
  window.__charts = [];
  (function () {
    var init = echarts.init;
    echarts.init = function (el, theme, opts) {
      var inst = init.call(echarts, el, theme, opts);
      var holder = el.closest('[data-vector]');
      var rec = window.__charts.find(function (c) { return c.el === el; });
      if (rec) rec.inits++;
      else { rec = {el: el, vector: holder ? holder.getAttribute('data-vector') : null, inits: 1, options: []}; window.__charts.push(rec); }
      var setOption = inst.setOption;
      inst.setOption = function (o) { rec.options.push(JSON.parse(JSON.stringify(o))); return setOption.apply(inst, arguments); };
      return inst;
    };
  })();`;

const browser = await chromium.launch({headless: true, executablePath});
let frames = 0;

async function openPage() {
  const page = await browser.newPage({viewport: {width: 1100, height: 900}});
  const consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => consoleErrors.push(String(e)));
  await page.route('**/*', async (route) => {
    const u = new URL(route.request().url());
    if (u.pathname.startsWith('/libs/')) {
      const file = new URL('public' + u.pathname, root);
      if (fs.existsSync(file)) return route.fulfill({path: file.pathname, headers: {'Access-Control-Allow-Origin': '*'}});
    }
    return route.fulfill({status: 200, body: '<html><body></body></html>', contentType: 'text/html'});
  });
  await page.goto('http://chat-components.test/');
  await page.addScriptTag({content: 'window.exports={};' + compiled});
  await page.evaluate(() => {
    window.__msgs = [];
    window.addEventListener('message', (e) => { if (e.data && typeof e.data.type === 'string' && e.data.type.startsWith('ARTIFACT_')) window.__msgs.push(e.data.type + (e.data.payload && e.data.payload.message ? ': ' + e.data.payload.message : '')); });
  });
  return {page, consoleErrors};
}

async function mount(page, code, {fixtureMode = false} = {}) {
  const name = 'art-' + (++frames);
  await page.evaluate(({code, name, fixtureMode}) => {
    document.querySelectorAll('iframe').forEach((f) => f.remove());
    window.__msgs = [];
    const f = document.createElement('iframe');
    f.name = name;
    f.sandbox = 'allow-scripts';
    f.style.width = '1000px';
    f.style.height = '4000px';
    f.srcdoc = exports.buildArtifactIframeHtml({data: {visualizations: [], runtime: {version: 11}}, code, reactBuild: 'development', fixtureMode});
    document.body.appendChild(f);
  }, {code, name, fixtureMode});
  await page.waitForFunction(() => window.__msgs.length > 0, null, {timeout: 20000});
  const msgs = await page.evaluate(() => window.__msgs.slice());
  assert.deepEqual(msgs, ['ARTIFACT_READY'], `rendered without errors: ${msgs}`);
  return page.frame({name});
}

async function until(frame, fn, arg, what, timeout = 8000) {
  const end = Date.now() + timeout;
  for (;;) {
    const v = await frame.evaluate(fn, arg);
    if (v) return v;
    if (Date.now() > end) throw new Error('timed out waiting for ' + what);
    await new Promise((r) => setTimeout(r, 25));
  }
}
const twoFrames = (frame) => frame.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))));
const artifact = (body) => `<script>${SPY}
(function () {
  var h = React.createElement;
  ${body}
})();
</script>`;
const msgsAfter = (page) => page.evaluate(() => window.__msgs.slice());

// ── ChatResult ──────────────────────────────────────────────────────────────
const cols = (names) => names.map((c) => ({field: c, headerName: c[0].toUpperCase() + c.slice(1), type: 'string'}));
const res = (id, chart, rows, names, extra = {}) => ({id, title: 'Result ' + id, chart, rows, columns: cols(names), row_count: rows.length, column_count: names.length, truncated: false, ...extra});
const ROWS = [{m: 'a', v: 1, w: 4}, {m: 'b', v: 2, w: 5}];
const GROUPED = [{m: 'a', g: 'x', v: 1}, {m: 'a', g: 'y', v: 2}, {m: 'b', g: 'x', v: 3}];
const NULLS = [{s: 'open', r: 'north', id: 'o1'}, {s: null, r: 'north', id: 'o2'}, {s: '', r: null, id: 'o3'}, {s: 'open', r: '', id: 'o4'}, {s: null, r: 'south', id: 'o5'}];
const ORDERS = [{s: 'open', r: 'north', id: 'o1'}, {s: 'open', r: 'south', id: 'o2'}, {s: 'open', r: 'north', id: 'o3'}, {s: 'closed', r: 'north', id: 'o4'}];
const VECTORS = {
  bar: res('bar', {type: 'bar', x: 'm', y: ['v'], series: null}, ROWS, ['m', 'v']),
  area: res('area', {type: 'area', x: 'm', y: ['v', 'w'], series: null}, ROWS, ['m', 'v', 'w']),
  line: res('line', {type: 'line', x: 'm', y: ['v'], series: 'g'}, GROUPED, ['m', 'g', 'v']),
  pie: res('pie', {type: 'pie', x: 'm', y: ['v'], series: null}, ROWS, ['m', 'v']),
  scatter: res('scatter', {type: 'scatter', x: 'v', y: ['w'], series: null}, ROWS, ['v', 'w']),
  heatmap: res('heatmap', {type: 'heatmap', x: 'm', y: ['v'], series: null}, ROWS, ['m', 'v']),
  nochart: res('nochart', null, ROWS, ['m', 'v']),
  unknownX: res('unknownX', {type: 'bar', x: 'nope', y: ['v'], series: null}, ROWS, ['m', 'v']),
  unknownY: res('unknownY', {type: 'bar', x: 'm', y: ['v', 'nope'], series: null}, ROWS, ['m', 'v']),
  unknownSeries: res('unknownSeries', {type: 'line', x: 'm', y: ['v'], series: 'nope'}, GROUPED, ['m', 'g', 'v']),
  emptyY: res('emptyY', {type: 'bar', x: 'm', y: [], series: null}, ROWS, ['m', 'v']),
  emptyRows: res('emptyRows', {type: 'bar', x: 'm', y: ['v'], series: null}, [], ['m', 'v']),
  // aggregate 'count' (the backend's counted hint): the rows per x (and series); y is ignored.
  countBar: res('countBar', {type: 'bar', x: 's', y: ['id'], series: null, aggregate: 'count'}, ORDERS, ['s', 'r', 'id']),
  countLine: res('countLine', {type: 'line', x: 's', y: ['id'], series: 'r', aggregate: 'count'}, ORDERS, ['s', 'r', 'id']),
  countPie: res('countPie', {type: 'pie', x: 's', y: ['id'], series: null, aggregate: 'count'}, ORDERS, ['s', 'r', 'id']),
  countScatter: res('countScatter', {type: 'scatter', x: 's', y: ['id'], series: null, aggregate: 'count'}, ORDERS, ['s', 'r', 'id']),
  // Nulls as EChartsVisual buckets them: a null x joins '' (one category), a null or '' series is
  // dropped (no unnamed series), a pie skips a null category.
  countNullX: res('countNullX', {type: 'bar', x: 's', y: ['id'], series: null, aggregate: 'count'}, NULLS, ['s', 'r', 'id']),
  countNullSeries: res('countNullSeries', {type: 'bar', x: 's', y: ['id'], series: 'r', aggregate: 'count'}, NULLS, ['s', 'r', 'id']),
  countNullPie: res('countNullPie', {type: 'pie', x: 's', y: ['id'], series: null, aggregate: 'count'}, NULLS, ['s', 'r', 'id']),
  otherAggregate: res('otherAggregate', {type: 'bar', x: 's', y: ['id'], series: null, aggregate: 'sum'}, ORDERS, ['s', 'r', 'id']),
};
const CHARTED = ['bar', 'area', 'line', 'pie', 'scatter', 'countBar', 'countLine', 'countPie', 'countNullX', 'countNullSeries', 'countNullPie'];
const TWO = [{n: 'A', a: 1}, {n: 'B', a: 2}];
const NOTES = {
  whole: {...res('whole', null, TWO, ['n', 'a'])},
  rowsCut: {...res('rowsCut', null, TWO, ['n', 'a']), row_count: 120, truncated: true},
  colsCut: {...res('colsCut', null, TWO, ['n', 'a']), column_count: 5, truncated: true},
  bothCut: {...res('bothCut', null, TWO, ['n', 'a']), row_count: 120, column_count: 5, truncated: true},
  partial: {...res('partial', null, TWO, ['n', 'a']), truncated: true},
};
const HEBREW = {rowsShown: 'מוצגות {shown} מתוך {total} שורות', columnsShown: '{shown} מתוך {total} עמודות', partial: 'טבלה חלקית'};

const RESULT_APP = artifact(`
  var VECTORS = ${JSON.stringify(VECTORS)}, NOTES = ${JSON.stringify(NOTES)}, HEBREW = ${JSON.stringify(HEBREW)};
  var R6 = ${JSON.stringify(res('r6', {type: 'bar', x: 'm', y: ['v'], series: null}, ROWS, ['m', 'v']))};
  window.__componentType = typeof window.ChatResult;
  var C = window.ChatResult;
  function Stable() {
    var s = React.useState(0);
    window.__bump = function () { s[1](function (n) { return n + 1; }); };
    // A new labels object on every render: ChatResult re-renders, its chart must not change.
    return h('div', {'data-vector': 'r6', 'data-renders': s[0]}, h(C, {result: R6, labels: {partial: 'Partial ' + s[0]}}));
  }
  function App() {
    if (!C) return h('p', {id: 'missing'}, 'ChatResult is not defined');
    var out = [];
    Object.keys(VECTORS).forEach(function (k) { out.push(h('div', {key: k, 'data-vector': k}, h(C, {result: VECTORS[k]}))); });
    Object.keys(NOTES).forEach(function (k) { out.push(h('div', {key: 'n-' + k, 'data-vector': 'note-' + k}, h(C, {result: NOTES[k]}))); });
    out.push(h('div', {key: 'he', 'data-vector': 'note-he'}, h(C, {result: NOTES.bothCut, labels: HEBREW})));
    out.push(h('div', {key: 'he-p', 'data-vector': 'note-he-partial'}, h(C, {result: NOTES.partial, labels: HEBREW})));
    out.push(h('div', {key: 'null', 'data-vector': 'bad-null'}, h(C, {result: null})));
    out.push(h('div', {key: 'str', 'data-vector': 'bad-string'}, h(C, {result: 'x'})));
    out.push(h('div', {key: 'rows', 'data-vector': 'bad-rows'}, h(C, {result: {id: 'b1', rows: 'x', columns: VECTORS.bar.columns, chart: VECTORS.bar.chart, truncated: true}})));
    out.push(h('div', {key: 'cols', 'data-vector': 'bad-columns'}, h(C, {result: {id: 'b2', rows: [{city: 'Haifa', n: 3}], chart: {type: 'bar', x: 'city', y: ['n']}}})));
    out.push(h(Stable, {key: 'stable'}));
    return h('div', null, out);
  }
  ReactDOM.createRoot(document.getElementById('root')).render(h(App));
`);

{
  const {page, consoleErrors} = await openPage();
  const frame = await mount(page, RESULT_APP);
  assert.equal(await frame.evaluate(() => window.__componentType), 'object', 'window.ChatResult is a (memo) component');
  await until(frame, () => document.querySelectorAll('[data-vector] canvas').length >= 12, null, 'the charts');
  await twoFrames(frame);
  const charts = await frame.evaluate(() => window.__charts.map((c) => ({vector: c.vector, inits: c.inits, options: c.options})));
  const last = (v) => { const c = charts.find((x) => x.vector === v); assert.ok(c, `a chart for ${v}`); return c.options.at(-1); };
  const vectorHas = (v, sel) => frame.evaluate(([v, sel]) => !!document.querySelector(`[data-vector="${v}"] ${sel}`), [v, sel]);

  // R1: the reference's option shapes, one chart per valid hint.
  const bar = last('bar');
  assert.deepEqual(bar.xAxis, {type: 'category', data: ['a', 'b']});
  assert.deepEqual(bar.series.map((s) => [s.type, s.data]), [['bar', [1, 2]]]);
  const area = last('area');
  assert.deepEqual(area.series.map((s) => [s.name, s.type, s.data, 'areaStyle' in s]), [['V', 'line', [1, 2], true], ['W', 'line', [4, 5], true]]);
  const line = last('line');
  assert.deepEqual(line.series.map((s) => [s.name, s.type, s.data]), [['x', 'line', [1, 3]], ['y', 'line', [2, null]]]);
  assert.deepEqual(last('pie').series[0].data, [{name: 'a', value: 1}, {name: 'b', value: 2}]);
  assert.deepEqual(last('scatter').series[0].data, [[1, 4], [2, 5]]);
  // Counted hints: open appears 3 times, closed once; with the series, north/south per status.
  const countBar = last('countBar');
  assert.deepEqual(countBar.xAxis, {type: 'category', data: ['open', 'closed']});
  assert.deepEqual(countBar.series.map((s) => [s.type, s.data]), [['bar', [3, 1]]]);
  assert.deepEqual(last('countLine').series.map((s) => [s.name, s.type, s.data]), [['north', 'line', [2, 1]], ['south', 'line', [1, null]]]);
  assert.deepEqual(last('countPie').series[0].data, [{name: 'open', value: 3}, {name: 'closed', value: 1}]);
  const nullX = last('countNullX');
  assert.deepEqual(nullX.xAxis.data, ['open', ''], 'a null x is the empty category, never "null"');
  assert.deepEqual(nullX.series[0].data, [2, 3]);
  const nullSeries = last('countNullSeries');
  assert.deepEqual(nullSeries.xAxis.data, ['open', '']);
  assert.deepEqual(nullSeries.series.map((s) => [s.name, s.data]), [['north', [1, 1]], ['south', [null, 1]]], 'no unnamed series');
  assert.deepEqual(last('countNullPie').series[0].data, [{name: 'open', value: 2}, {name: '', value: 1}], 'a null category is skipped');
  for (const v of CHARTED) {
    assert.ok(await vectorHas(v, 'canvas'), `${v} draws a chart`);
    assert.ok(await vectorHas(v, '[data-testid="bow-datatable"]'), `${v} also shows the table`);
  }
  for (const v of Object.keys(VECTORS).filter((k) => !CHARTED.includes(k))) {
    assert.equal(await vectorHas(v, 'canvas'), false, `${v} draws no chart`);
    assert.equal(charts.some((c) => c.vector === v), false, `${v} never initialises a chart`);
    assert.ok(await vectorHas(v, VECTORS[v].rows.length ? '[data-testid="bow-datatable"] table' : '[data-testid="bow-datatable"]'), `${v} shows the table`);
  }
  const title = await frame.evaluate(() => { const t = document.querySelector('[data-vector="bar"] [data-testid="bow-chat-result-title"]'); return t && [t.textContent, t.getAttribute('dir')]; });
  assert.deepEqual(title, ['Result bar', 'auto']);
  assert.ok(await vectorHas('bar', '[data-testid="bow-chat-result"]'));

  // R2: notes.
  const note = (v) => frame.evaluate((v) => { const n = document.querySelector(`[data-vector="${v}"] [data-testid="bow-chat-result-note"]`); return n ? n.textContent : null; }, v);
  assert.equal(await note('note-whole'), null, 'a whole table has no note');
  assert.equal(await note('note-rowsCut'), 'Showing 2 of 120 rows');
  assert.equal(await note('note-colsCut'), 'Showing 2 of 2 rows, 2 of 5 columns');
  assert.equal(await note('note-bothCut'), 'Showing 2 of 120 rows, 2 of 5 columns');
  assert.equal(await note('note-partial'), 'Partial table', 'cut cells only: partial table');
  assert.equal(await note('note-he'), 'מוצגות 2 מתוך 120 שורות, 2 מתוך 5 עמודות', 'labels are substituted');
  assert.equal(await note('note-he-partial'), 'טבלה חלקית');
  const heDir = await frame.evaluate(() => getComputedStyle(document.querySelector('[data-vector="note-he"] [data-testid="bow-chat-result-note"]')).direction);
  assert.equal(heDir, 'rtl', 'a Hebrew note renders right-to-left');

  // R3: malformed input never throws.
  assert.equal(await vectorHas('bad-null', '*'), false, 'result={null} renders nothing');
  assert.equal(await vectorHas('bad-string', '*'), false, 'a non-object result renders nothing');
  assert.ok(await vectorHas('bad-rows', '[data-testid="bow-datatable"]'), 'rows that are not an array: an empty table');
  assert.equal(await vectorHas('bad-rows', 'canvas'), false);
  assert.equal(await note('bad-rows'), 'Partial table', 'missing counts default to what is shown');
  assert.ok(await vectorHas('bad-columns', '[data-testid="bow-datatable"] table'), 'missing columns: the table uses the row keys');
  assert.equal(await vectorHas('bad-columns', 'canvas'), false, 'missing columns: no chart field can validate');

  // R6: re-rendering the same result object keeps its chart.
  const r6 = () => frame.evaluate(() => { const c = window.__charts.find((x) => x.vector === 'r6'); return c && {inits: c.inits, sets: c.options.length}; });
  const base = await r6();
  assert.ok(base && base.sets >= 1, 'the r6 chart settled');
  for (let i = 1; i <= 5; i++) {
    await frame.evaluate(() => window.__bump());
    await until(frame, (n) => document.querySelector('[data-vector="r6"]').getAttribute('data-renders') === String(n), i, 're-render ' + i);
  }
  await twoFrames(frame);
  assert.equal(await frame.evaluate(() => document.querySelector('[data-vector="r6"] [data-testid="bow-chat-result-note"]')), null);
  assert.deepEqual(await r6(), base, 'no new setOption and no re-init after 5 parent re-renders');

  // R5 + R3: no React warnings (development build) and no artifact error.
  assert.deepEqual(await msgsAfter(page), ['ARTIFACT_READY']);
  assert.deepEqual(consoleErrors.filter((e) => /Warning|key/.test(e)), [], 'no React warnings');
  await page.close();
  console.log('PASS: ChatResult draws valid chart hints only, always shows the table, writes the truncation notes and keeps its chart across re-renders');
}

// ── ChatMessage ─────────────────────────────────────────────────────────────
const MIXED = 'שלום, זו התשובה לשאלה שלך.\n\nThis paragraph is English.\n\n- פריט ראשון ברשימה\n- second item in English\n\n```\nשלום = 1\n```';
const M = (id, extra) => ({id, role: 'assistant', text: '', status: 'success', created_at: null, tools: [], results: [], results_incomplete: false, ...extra});
const MESSAGES = {
  mixed: M('mixed', {text: MIXED}),
  userHe: M('userHe', {role: 'user', text: 'כמה הכנסות היו בחודש שעבר?\nשורה שנייה'}),
  tools: M('tools', {text: 'Done.', tools: [
    {id: 't1', name: 'run_query', title: 'Running the query', status: 'running'},
    {name: 'make_chart', status: 'success'},
    {name: 'fetch_rows', title: {nested: true}, status: 'error'},
    {status: 'success'},
    'not a tool',
  ]}),
  results: M('results', {text: 'Two results.', results: [VECTORS.bar, NOTES.bothCut], results_incomplete: true}),
  typingEmpty: M('typingEmpty', {status: 'in_progress'}),
  typingText: M('typingText', {status: 'in_progress', text: 'Partial answer so far'}),
  bad: M('bad', {text: 'Robust.', tools: 'x', results: null, results_incomplete: 'yes', steps: 'x'}),
  steps: M('steps', {text: 'Final answer.', steps: ['בודק את הנתונים.', 'Running **the** query.', '', 7]}),
  failedEmpty: M('failedEmpty', {status: 'error'}),
  stoppedEmpty: M('stoppedEmpty', {status: 'stopped'}),
  failedText: M('failedText', {status: 'error', text: 'Half an answer'}),
  stoppedText: M('stoppedText', {status: 'stopped', text: 'Cut short'}),
  userFailed: M('userFailed', {role: 'user', status: 'error', text: 'q'}),
};

const MESSAGE_APP = `<script>function ChatResult() { return null; }</script>` + artifact(`
  var MESSAGES = ${JSON.stringify(MESSAGES)};
  window.__componentType = typeof window.ChatMessage;
  window.__appChatResult = window.ChatResult && window.ChatResult.name === 'ChatResult' && typeof window.ChatResult === 'function';
  var CM = window.ChatMessage;
  function App() {
    if (!CM) return h('p', {id: 'missing'}, 'ChatMessage is not defined');
    return h('div', {dir: 'rtl'}, [
      h('div', {key: 1, 'data-vector': 'mixed'}, h(CM, {message: MESSAGES.mixed})),
      h('div', {key: 2, 'data-vector': 'userHe'}, h(CM, {message: MESSAGES.userHe})),
      h('div', {key: 3, 'data-vector': 'tools'}, h(CM, {message: MESSAGES.tools})),
      h('div', {key: 4, 'data-vector': 'toolsHidden'}, h(CM, {message: MESSAGES.tools, showTools: false})),
      h('div', {key: 5, 'data-vector': 'results'}, h(CM, {message: MESSAGES.results})),
      h('div', {key: 6, 'data-vector': 'resultsLabels'}, h(CM, {message: MESSAGES.results, labels: {incomplete: 'חלק מהתוצאות לא הוצגו'}})),
      h('div', {key: 7, 'data-vector': 'typingEmpty'}, h(CM, {message: MESSAGES.typingEmpty})),
      h('div', {key: 8, 'data-vector': 'typingText'}, h(CM, {message: MESSAGES.typingText})),
      h('div', {key: 9, 'data-vector': 'typingLabel'}, h(CM, {message: MESSAGES.typingEmpty, labels: {inProgress: 'עונה...'}})),
      h('div', {key: 10, 'data-vector': 'done'}, h(CM, {message: MESSAGES.mixed})),
      h('div', {key: 11, 'data-vector': 'null'}, h(CM, {message: null})),
      h('div', {key: 12, 'data-vector': 'bad'}, h(CM, {message: MESSAGES.bad})),
      h('div', {key: 13, 'data-vector': 'steps'}, h(CM, {message: MESSAGES.steps})),
      h('div', {key: 14, 'data-vector': 'stepsLabel'}, h(CM, {message: MESSAGES.steps, labels: {steps: 'שלבים'}})),
      h('div', {key: 15, 'data-vector': 'failedEmpty'}, h(CM, {message: MESSAGES.failedEmpty})),
      h('div', {key: 16, 'data-vector': 'stoppedEmpty'}, h(CM, {message: MESSAGES.stoppedEmpty})),
      h('div', {key: 17, 'data-vector': 'failedText'}, h(CM, {message: MESSAGES.failedText})),
      h('div', {key: 18, 'data-vector': 'stoppedText'}, h(CM, {message: MESSAGES.stoppedText})),
      h('div', {key: 19, 'data-vector': 'endedLabels'}, [
        h(CM, {key: 'f', message: MESSAGES.failedEmpty, labels: {failed: 'התשובה נכשלה'}}),
        h(CM, {key: 's', message: MESSAGES.stoppedText, labels: {stopped: 'התשובה נעצרה'}})
      ]),
      h('div', {key: 20, 'data-vector': 'userFailed'}, h(CM, {message: MESSAGES.userFailed}))
    ]);
  }
  ReactDOM.createRoot(document.getElementById('root')).render(h(App));
`);

{
  const {page, consoleErrors} = await openPage();
  const frame = await mount(page, MESSAGE_APP);
  assert.equal(await frame.evaluate(() => window.__componentType), 'object', 'window.ChatMessage is a (memo) component');
  assert.equal(await frame.evaluate(() => window.__appChatResult), true, 'the app replaced window.ChatResult with its own');
  await until(frame, () => document.querySelector('[data-vector="results"] canvas'), null, 'the result chart');
  const q = (v, sel, fn) => frame.evaluate(([v, sel, fn]) => Array.from(document.querySelectorAll(`[data-vector="${v}"] ${sel}`)).map((el) => new Function('el', 'return ' + fn)(el)), [v, sel, fn]);
  const dir = 'getComputedStyle(el).direction';

  // M1: bidi per block in an RTL container.
  const root = await q('mixed', '[data-testid="bow-chat-message"]', '[el.dataset.role, el.dataset.status, el.getAttribute("aria-busy")]');
  assert.deepEqual(root, [['assistant', 'success', 'false']]);
  assert.deepEqual(await q('mixed', '[data-testid="bow-chat-text"] p', dir), ['rtl', 'ltr'], 'Hebrew paragraph rtl, English paragraph ltr');
  assert.deepEqual(await q('mixed', '[data-testid="bow-chat-text"] li', dir), ['rtl', 'ltr'], 'each list item gets its own direction');
  assert.deepEqual(await q('mixed', '[data-testid="bow-chat-text"] pre', dir), ['ltr'], 'code stays left-to-right');
  assert.deepEqual(await q('mixed', '[data-testid="bow-chat-text"] :is(p, ul, li)', 'el.getAttribute("dir")'), ['auto', 'auto', 'auto', 'auto', 'auto']);
  const user = await q('userHe', '[data-testid="bow-chat-text"]', '[el.tagName, el.getAttribute("dir"), getComputedStyle(el).direction, getComputedStyle(el).whiteSpace, el.textContent]');
  assert.deepEqual(user, [['P', 'auto', 'rtl', 'pre-wrap', MESSAGES.userHe.text]], 'user text is plain, rtl, whitespace kept');
  assert.equal((await q('userHe', '[data-testid="bow-chat-message"]', 'el.dataset.role'))[0], 'user');

  // M2: tool steps, labelled by title or name, never by the object.
  const tools = await q('tools', '[data-testid="bow-chat-tool"]', '[el.textContent, el.getAttribute("dir"), !!el.querySelector("svg")]');
  assert.deepEqual(tools.map((t) => t[0]), ['Running the query', 'make_chart', 'fetch_rows'], 'empty and non-object tools are skipped');
  assert.ok(tools.every((t) => t[1] === 'auto'));
  assert.ok(tools[0][2], 'a running step shows the spinner');
  assert.ok(!(await frame.evaluate(() => document.body.textContent.includes('[object Object]'))), 'no object is ever printed');
  assert.deepEqual(await q('toolsHidden', '[data-testid="bow-chat-tools"]', '1'), [], 'showTools={false} hides the steps');
  assert.deepEqual(await q('toolsHidden', '[data-testid="bow-chat-text"]', 'el.textContent'), ['Done.']);

  // M3 + R4: results through the built-in ChatResult, despite the app's own.
  assert.equal((await q('results', '[data-testid="bow-chat-result"]', '1')).length, 2, 'one ChatResult per result');
  assert.equal((await q('results', 'canvas', '1')).length, 1, 'the chart only for the valid hint');
  assert.deepEqual(await q('results', '[data-testid="bow-chat-result-note"]', 'el.textContent'), ['Showing 2 of 120 rows, 2 of 5 columns']);
  assert.deepEqual(await q('results', '[data-testid="bow-chat-incomplete"]', '[el.textContent, el.getAttribute("dir")]'), [['Some results of this answer could not be shown', 'auto']]);
  assert.deepEqual(await q('resultsLabels', '[data-testid="bow-chat-incomplete"]', 'el.textContent'), ['חלק מהתוצאות לא הוצגו']);
  assert.deepEqual(await q('done', '[data-testid="bow-chat-incomplete"]', '1'), []);

  // M6: the in-progress indicator.
  const typing = (v) => q(v, '[data-testid="bow-chat-typing"]', '[el.textContent, el.getAttribute("role"), !!el.querySelector("svg")]');
  assert.deepEqual(await typing('typingEmpty'), [['Answer in progress...', 'status', true]], 'shown with empty text');
  assert.deepEqual(await typing('typingText'), [['Answer in progress...', 'status', true]], 'shown below partial text');
  assert.deepEqual(await q('typingText', '[data-testid="bow-chat-text"]', 'el.textContent'), ['Partial answer so far']);
  assert.deepEqual(await q('typingText', '[data-testid="bow-chat-message"]', 'el.getAttribute("aria-busy")'), ['true']);
  assert.deepEqual((await typing('typingLabel')).map((t) => t[0]), ['עונה...']);
  assert.deepEqual(await typing('done'), [], 'a finished answer has no indicator');

  // M7: the narration before the answer (steps), collapsed; the answer alone in the text.
  const steps = await q('steps', '[data-testid="bow-chat-steps"]', '[el.tagName, el.open, el.querySelector("summary").textContent]');
  assert.deepEqual(steps, [['DETAILS', false, 'Steps']], 'one collapsed steps section');
  assert.deepEqual(await q('steps', '[data-testid="bow-chat-step"]', '[el.textContent, el.getAttribute("dir")]'),
    [['בודק את הנתונים.', 'auto'], ['Running the query.', 'auto']], 'each step is markdown, with its own direction; empty and non-text steps are skipped');
  assert.deepEqual(await q('steps', '[data-testid="bow-chat-step"] strong', 'el.textContent'), ['the']);
  assert.deepEqual(await q('steps', '[data-testid="bow-chat-text"]', 'el.textContent'), ['Final answer.']);
  assert.deepEqual(await q('steps', '[data-testid="bow-chat-step"]', 'el.checkVisibility()'), [false, false], 'hidden until opened');
  assert.deepEqual(await q('stepsLabel', '[data-testid="bow-chat-steps"] summary', 'el.textContent'), ['שלבים']);
  assert.deepEqual(await q('mixed', '[data-testid="bow-chat-steps"]', '1'), [], 'no steps, no section');
  assert.deepEqual(await q('bad', '[data-testid="bow-chat-steps"]', '1'), [], 'steps that are not a list are ignored');

  // M8: a failed or stopped answer is marked (muted, dir="auto"), after its text when it has some.
  const ended = (v) => q(v, '[data-testid="bow-chat-ended"]', '[el.textContent, el.getAttribute("dir"), el.dataset.status, el.className]');
  const note = await q('results', '[data-testid="bow-chat-incomplete"]', 'el.className');
  assert.deepEqual(await ended('failedEmpty'), [['The answer failed', 'auto', 'error', note[0]]], 'an empty failed answer is marked');
  assert.deepEqual(await ended('stoppedEmpty'), [['The answer was stopped', 'auto', 'stopped', note[0]]], 'an empty stopped answer is marked');
  assert.deepEqual((await ended('failedText')).map((e) => e[0]), ['The answer failed']);
  assert.deepEqual((await ended('stoppedText')).map((e) => e[0]), ['The answer was stopped']);
  for (const v of ['failedText', 'stoppedText']) {
    const order = await q(v, '[data-testid="bow-chat-message"] > *', 'el.dataset.testid');
    assert.deepEqual(order, ['bow-chat-text', 'bow-chat-ended'], v + ': the marker comes after the text');
  }
  assert.deepEqual((await ended('endedLabels')).map((e) => e[0]), ['התשובה נכשלה', 'התשובה נעצרה'], 'labels.failed / labels.stopped');
  assert.deepEqual(await ended('userFailed'), [], 'a user item is not marked');
  for (const v of ['mixed', 'typingEmpty', 'typingText']) assert.deepEqual(await ended(v), [], v + ': no marker');

  // M4: malformed messages.
  assert.deepEqual(await q('null', '*', '1'), [], 'message={null} renders nothing');
  assert.deepEqual(await q('bad', '[data-testid="bow-chat-text"]', 'el.textContent'), ['Robust.']);
  assert.deepEqual(await q('bad', ':is([data-testid="bow-chat-tool"], [data-testid="bow-chat-result"])', '1'), []);

  assert.deepEqual(await msgsAfter(page), ['ARTIFACT_READY']);
  assert.deepEqual(consoleErrors.filter((e) => /Warning|key/.test(e)), [], 'no React warnings');
  await page.close();
  console.log('PASS: ChatMessage renders markdown with dir="auto" per block, tool steps, its results (even when the app defines ChatResult), the incomplete note and the in-progress indicator');
}

// M5: without window.bow (the MCP shell, exports) the text is plain.
{
  const {page} = await openPage();
  const frame = await mount(page, artifact(`
    delete window.bow;
    function App() {
      if (!window.ChatMessage) return h('p', {id: 'missing'}, 'ChatMessage is not defined');
      return h(window.ChatMessage, {message: {id: 'a', role: 'assistant', text: '**Bold** answer\\nsecond line', status: 'success', tools: [], results: []}});
    }
    ReactDOM.createRoot(document.getElementById('root')).render(h(App));
  `));
  const plain = await frame.evaluate(() => { const t = document.querySelector('[data-testid="bow-chat-text"]'); return t && [t.textContent, !!t.querySelector('strong'), t.querySelector('[dir="auto"]') ? 'auto' : null]; });
  assert.deepEqual(plain, ['**Bold** answer\nsecond line', false, 'auto']);
  assert.deepEqual(await msgsAfter(page), ['ARTIFACT_READY']);
  await page.close();
  console.log('PASS: ChatMessage falls back to plain text without bow.markdown');
}

// F1: useConversation + ChatMessage in the SDK's fixture mode.
{
  const {page, consoleErrors} = await openPage();
  const frame = await mount(page, artifact(`
    window.__creates = 0;
    var create = bow.conversations.create;
    bow.conversations.create = function () { window.__creates++; return create.apply(this, arguments); };
    window.__hookType = typeof window.useConversation;
    function Chat() {
      if (window.__hookType !== 'function') return h('p', {id: 'missing'}, 'useConversation is not defined');
      var chat = useConversation();
      window.__chat = chat;
      return h('div', {'data-vector': 'f1'}, chat.messages.map(function (m) { return h(ChatMessage, {key: m.id, message: m}); }));
    }
    ReactDOM.createRoot(document.getElementById('root')).render(h('div', null, h(Chat)));
  `), {fixtureMode: true});
  assert.equal(await frame.evaluate(() => window.__hookType), 'function', 'window.useConversation is a hook');
  const settled = async (n) => {
    await until(frame, (n) => window.__chat && !window.__chat.sending && window.__chat.messages.length === n && document.querySelectorAll('canvas').length === n / 2, n, n + ' messages');
    await twoFrames(frame);
  };
  const snap = () => frame.evaluate(() => JSON.parse(JSON.stringify({messages: window.__chat.messages, conversations: window.__chat.conversations, conversationId: window.__chat.conversationId, creates: window.__creates,
    charts: window.__charts.map((c) => ({inits: c.inits, sets: c.options.length}))})));
  await until(frame, () => window.__chat && !window.__chat.loading, null, 'the initial load');
  await frame.evaluate(() => window.__chat.send('Revenue per month?'));
  await settled(2);
  let c = await snap();
  assert.deepEqual(c.messages.map((m) => [m.role, m.status]), [['user', 'success'], ['assistant', 'success']]);
  assert.ok(c.messages.every((m) => !String(m.id).startsWith('local-')), 'server ids after the re-read');
  assert.equal(c.messages[0].text, 'Revenue per month?');
  assert.equal(c.messages[1].text, 'Preview answer. No agent was called.');
  assert.equal(c.conversations.length, 1);
  assert.equal(c.conversationId, c.conversations[0].id);
  const results = await frame.evaluate(() => Array.from(document.querySelectorAll('[data-testid="bow-chat-result"]')).map((r) => [!!r.querySelector('canvas'), r.querySelector('[data-testid="bow-chat-result-note"]')?.textContent || null]));
  assert.deepEqual(results, [[true, null], [false, 'Showing 2 of 120 rows, 2 of 5 columns']]);
  const base = c.charts[0];
  assert.ok(base.inits === 1 && base.sets >= 1, 'the first chart settled');
  await frame.evaluate(() => window.__chat.send('And per week?'));
  await settled(4);
  c = await snap();
  assert.deepEqual(c.messages.map((m) => m.role), ['user', 'assistant', 'user', 'assistant']);
  assert.deepEqual(c.messages.filter((m) => m.role === 'user').map((m) => m.text), ['Revenue per month?', 'And per week?']);
  assert.equal(c.creates, 1, 'the second send reuses the conversation');
  assert.equal(c.conversations.length, 1);
  assert.deepEqual(c.charts[0], base, 'the first chart: one init, no new setOption after both re-reads');
  assert.deepEqual(await msgsAfter(page), ['ARTIFACT_READY']);
  assert.deepEqual(consoleErrors.filter((e) => /Warning|key/.test(e)), [], 'no React warnings');
  await page.close();
  console.log('PASS: useConversation in fixture mode: one create, server ids, two results (chart + cut table note), a stable chart across re-reads');
}

await browser.close();

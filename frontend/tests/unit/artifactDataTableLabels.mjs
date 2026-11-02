// Real Chromium: DataTable's own words (search, CSV, "1–10 of 24", the empty
// text) follow the app's language. `labels` wins; otherwise Hebrew or English
// from a ChatResult hint (Hebrew chat labels), the frame's <html lang> (the
// viewer's BOW language, set by buildArtifactIframeHtml's `locale`), and only
// without either, the table's own text. searchPlaceholder/emptyText still win.
//
//   cd frontend && PW_CHROMIUM_PATH=/path/to/chrome node tests/unit/artifactDataTableLabels.mjs
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

const browser = await chromium.launch({headless: true, executablePath});
const page = await browser.newPage({viewport: {width: 1100, height: 900}});
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
await page.route('**/*', async (route) => {
  const u = new URL(route.request().url());
  if (u.pathname.startsWith('/libs/')) {
    const file = new URL('public' + u.pathname, root);
    if (fs.existsSync(file)) return route.fulfill({path: file.pathname, headers: {'Access-Control-Allow-Origin': '*'}});
  }
  return route.fulfill({status: 200, body: '<html><body></body></html>', contentType: 'text/html'});
});
await page.goto('http://datatable-labels.test/');
await page.addScriptTag({content: 'window.exports={};' + compiled});
await page.evaluate(() => {
  window.__msgs = [];
  window.addEventListener('message', (e) => { if (e.data && typeof e.data.type === 'string' && e.data.type.startsWith('ARTIFACT_')) window.__msgs.push(e.data.type + (e.data.payload && e.data.payload.message ? ': ' + e.data.payload.message : '')); });
});

let n = 0;
// Renders `body` (a React element expression using h) and returns the frame.
async function mount(body, locale) {
  const name = 'f' + (++n);
  const code = `<script>(function(){ var h = React.createElement;
    ReactDOM.createRoot(document.getElementById('root')).render(${body}); window.__done = true; })();</script>`;
  await page.evaluate(({code, name, locale}) => {
    document.querySelectorAll('iframe').forEach((f) => f.remove());
    window.__msgs = [];
    const f = document.createElement('iframe');
    f.name = name; f.sandbox = 'allow-scripts'; f.style.width = '1000px'; f.style.height = '900px';
    f.srcdoc = exports.buildArtifactIframeHtml({data: {visualizations: [], runtime: {version: 11}}, code, reactBuild: 'development', locale});
    document.body.appendChild(f);
  }, {code, name, locale});
  await page.waitForFunction(() => window.__msgs.length > 0, null, {timeout: 20000});
  const msgs = await page.evaluate(() => window.__msgs.slice());
  assert.deepEqual(msgs, ['ARTIFACT_READY'], `rendered without errors: ${msgs}`);
  const frame = page.frames().find((f) => f.name() === name);
  await frame.waitForSelector('[data-testid="bow-datatable"]', {timeout: 8000});
  return frame;
}
const chrome = (frame) => frame.evaluate(() => {
  const q = (s) => document.querySelector(s);
  return {
    lang: document.documentElement.getAttribute('lang'),
    search: q('[data-testid="bow-dt-search"]')?.getAttribute('placeholder') ?? null,
    csv: q('[data-testid="bow-dt-export"]')?.textContent ?? null,
    download: q('[data-testid="bow-dt-export"]')?.getAttribute('title') ?? null,
    range: q('[data-testid="bow-dt-range"]')?.textContent ?? null,
    text: q('[data-testid="bow-datatable"]').textContent,
  };
});

const EN_ROWS = JSON.stringify(Array.from({length: 24}, (_, i) => ({customer: 'Customer ' + (i + 1), total: i * 10})));
const HE_ROWS = JSON.stringify(Array.from({length: 24}, (_, i) => ({customer: 'לקוח ' + (i + 1), total: i * 10})));
const table = (rows, extra = '') => `h(DataTable, Object.assign({rows: ${rows}, pageSize: 10, searchable: true}, ${extra || '{}'}))`;

// The viewer's language is Hebrew; the data is English field names and values.
let c = await chrome(await mount(table(EN_ROWS), 'he'));
assert.equal(c.lang, 'he', 'the frame carries the viewer language');
assert.equal(c.search, 'חיפוש...');
assert.equal(c.download, 'הורדת CSV');
assert.match(c.range, /^\u20661–10\u2069 מתוך 24/);

// English viewer, Hebrew data: the table's words follow the viewer, not the data.
c = await chrome(await mount(table(HE_ROWS), 'en'));
assert.equal(c.search, 'Search...');
assert.match(c.range, /^1–10 of 24/);

// No page language (an older host): the table's own text decides.
c = await chrome(await mount(table(HE_ROWS), null));
assert.equal(c.lang, null);
assert.equal(c.search, 'חיפוש...');
c = await chrome(await mount(table(EN_ROWS), null));
assert.equal(c.search, 'Search...');

// Explicit labels win over everything; the legacy props win over labels.
c = await chrome(await mount(table(EN_ROWS, `{labels: {search: 'Buscar...', range: '{start}-{end} de {total}', csv: 'Exportar'}}`), 'he'));
assert.equal(c.search, 'Buscar...');
assert.equal(c.csv, 'Exportar');
assert.match(c.range, /^1-10 de 24/);
c = await chrome(await mount(table(EN_ROWS, `{searchPlaceholder: 'Find a customer', labels: {search: 'ignored'}}`), 'he'));
assert.equal(c.search, 'Find a customer');

// Empty, and the search match count, in Hebrew.
c = await chrome(await mount(`h(DataTable, {rows: [], columns: [{field: 'a', headerName: 'A'}]})`, 'he'));
assert.match(c.text, /אין נתונים להצגה/);

// ChatResult: Hebrew chat labels make its table Hebrew in an English frame.
const RESULT = JSON.stringify({id: 'r1', title: 'Top', chart: null, rows: Array.from({length: 12}, (_, i) => ({name: 'Artist ' + i, sales: i})),
  columns: [{field: 'name', headerName: 'name', type: 'string'}, {field: 'sales', headerName: 'sales', type: 'number'}], row_count: 12, column_count: 2, truncated: false});
c = await chrome(await mount(`h(ChatResult, {result: ${RESULT}, labels: {send: 'שלח'}})`, 'en'));
assert.match(c.range, /^\u20661–10\u2069 מתוך 12/);
c = await chrome(await mount(`h(ChatResult, {result: ${RESULT}})`, 'en'));
assert.match(c.range, /^1–10 of 12/);

assert.deepEqual(errors, []);
await browser.close();
console.log('PASS: DataTable words follow labels, then a ChatResult hint, then <html lang>, then the table text');

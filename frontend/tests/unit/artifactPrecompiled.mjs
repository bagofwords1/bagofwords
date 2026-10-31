// Real Chromium: the fast artifact iframe paths render EXACTLY like the
// original one.
//
//   A. original      — every lib by <script src>, babel-standalone compiles
//                      the artifact's text/babel script on DOMContentLoaded
//   B. inline libs   — libs handed in as source text (host-fetched, see
//                      utils/artifactLibs.ts) and run as inline scripts;
//                      Babel itself stays a <script src>
//   C. precompiled   — B + no Babel: the code Babel generated in A is passed
//                      as `precompiled` and run the way Babel runs it
//   D. stale key     — precompiled for different code must be ignored (Babel
//                      path), never executed
//
// Each variant renders the same complex artifact; the DOM, the ARTIFACT_*
// messages, the order of DOMContentLoaded listeners and artifact-globals'
// import stripping must match A. Also pins precompiledKey against the
// backend's vectors (backend/tests/unit/test_artifact_precompile.py).
//
//   cd frontend && node tests/unit/artifactPrecompiled.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';

const root = new URL('../../', import.meta.url);
const source = fs.readFileSync(new URL('utils/artifactIframe.ts', root), 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
const libsDir = new URL('public/libs/', root);
if (!fs.existsSync(new URL('babel-standalone.min.js', libsDir))) {
  console.log('SKIP: vendored libs missing (scripts/download-vendor-libs.sh)');
  process.exit(0);
}

// precompiledKey vectors shared with the backend.
{
  const m = {exports: {}};
  new Function('exports', 'module', 'require', compiled)(m.exports, m, () => ({}));
  const k = m.exports.precompiledKey;
  for (const [code, key] of [
    ['', '0:811c9dc5'],
    ['abc', '3:1a47e90b'],
    ['<script type="text/babel">\nconst x = <div/>;\n</script>', '54:904bcd58'],
    ['ünïcødé — 😀 \r\n end', '19:699069e9'],
  ]) assert.equal(k(code), key, `precompiledKey(${JSON.stringify(code)})`);
}

const ARTIFACT = `<script type="text/babel">
import React from 'react';
import { useMemo } from 'react';
const fmt = (v, d = 0) => new Intl.NumberFormat('en-US', { maximumFractionDigits: d }).format(v ?? 0);
let renders = 0;
class Stats { static of(v) { return new Stats(v); } #v; constructor(v) { this.#v = [...v]; } get sum() { return this.#v.reduce((a, b) => a + b, 0); } }
function* pairs(r) { for (let i = 1; i < r.length; i++) yield [r[i - 1], r[i]]; }
class Boundary extends React.Component {
  state = { error: null };
  static getDerivedStateFromError(error) { return { error }; }
  render() { return this.state.error ? <i id="caught">{this.state.error.message}</i> : this.props.children; }
}
function Bomb() { throw new Error('boom'); }
function App() {
  const data = useArtifactData();
  const [n, setN] = useState(0);
  const [log, setLog] = useState([]);
  renders++;
  useEffect(() => { (async () => { for await (const x of (async function* () { yield 'a'; yield 'b'; })()) setLog((l) => [...l, x]); })(); }, []);
  const rows = data?.visualizations?.[0]?.rows ?? [];
  const deltas = [...pairs(rows)].map(([a, b]) => b.v - a.v);
  return (
    <div id="app" className="p-4 rounded-lg shadow">
      <h1 id="title">{data?.report?.title} — ünïcødé 😀</h1>
      <p id="sum">{fmt(Stats.of(rows.map((r) => r.v)).sum)}</p>
      <p id="deltas">{deltas.join(',')}</p>
      <p id="log">{log.join('|')}</p>
      <p id="globals">{typeof EChart}/{typeof KPICard}/{typeof lucide}/{typeof echarts}</p>
      <button id="inc" onClick={() => setN((v) => v + 1)}>n={n}</button>
      <Boundary><Bomb /></Boundary>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const DATA = {report: {title: 'Parity'}, visualizations: [{id: 'v1', rows: [{v: 1}, {v: 4}, {v: 9}, {v: 16}]}]};

const browser = await chromium.launch({headless: true, executablePath: process.env.PW_CHROMIUM_PATH || undefined});
const libCache = new Map();
const libText = (pathname) => {
  if (!libCache.has(pathname)) libCache.set(pathname, fs.readFileSync(new URL('public' + pathname, root), 'utf8'));
  return libCache.get(pathname);
};

async function render(variant, precompiledJs) {
  const page = await browser.newPage();
  const libRequests = [];
  await page.route('**/*', async (route) => {
    const u = new URL(route.request().url());
    if (u.pathname.startsWith('/libs/')) {
      libRequests.push(u.pathname);
      const file = new URL('public' + u.pathname, root);
      if (fs.existsSync(file)) return route.fulfill({path: file.pathname, headers: {'Access-Control-Allow-Origin': '*'}});
    }
    return route.fulfill({status: 200, body: '<html><body></body></html>', contentType: 'text/html'});
  });
  await page.goto('http://artifact-parity.test/');
  await page.addScriptTag({content: 'window.exports={};' + compiled});
  const inlineLibs = {};
  if (variant !== 'A') {
    const urls = await page.evaluate(({code, data, pre}) => exports.inlineableArtifactLibs({code, data, precompiled: pre}), {
      code: ARTIFACT, data: DATA, pre: variant === 'C' ? {js: precompiledJs, key: null} : variant === 'D' ? {js: precompiledJs, key: '1:deadbeef'} : null,
    });
    for (const url of urls) inlineLibs[url] = libText(new URL(url, 'http://x').pathname);
  }
  const babelOut = await page.evaluate(({code, data, inlineLibs, variant, precompiledJs}) => {
    window.__msgs = [];
    window.addEventListener('message', (e) => { if (e.data && typeof e.data.type === 'string') window.__msgs.push(e.data.type === 'ARTIFACT_ERROR' ? 'ARTIFACT_ERROR:' + (e.data.payload && e.data.payload.message) : e.data.type); });
    const pre = variant === 'C' ? {js: precompiledJs, key: exports.precompiledKey(code)}
      : variant === 'D' ? {js: precompiledJs, key: '1:deadbeef'} : null;
    // Instrumentation injected AHEAD of the artifact: record DOMContentLoaded
    // listener order and capture Babel's generated script (variant A).
    const probe = `<script>window.__dcl=[];var __ael=window.addEventListener;window.addEventListener=function(t,f,o){if(t==='DOMContentLoaded'){var i=window.__dcl.length;var g=function(){window.__dcl.push('run'+i);return f.apply(this,arguments)};return __ael.call(this,t,g,o)}return __ael.apply(this,arguments)};window.__gen=[];var __ap=Node.prototype.appendChild;Node.prototype.appendChild=function(n){if(n&&n.tagName==='SCRIPT'&&!n.src&&n.parentNode===null&&this===document.head){window.__gen.push(n.text)}return __ap.call(this,n)};<\/script>`;
    let html = exports.buildArtifactIframeHtml({data, code, inlineLibs: variant === 'A' ? null : inlineLibs, precompiled: pre});
    html = html.replace('<meta charset="UTF-8">', '<meta charset="UTF-8">' + probe);
    const f = document.createElement('iframe');
    f.sandbox = 'allow-scripts';
    f.srcdoc = html;
    document.body.appendChild(f);
    return html.includes('babel-standalone');
  }, {code: ARTIFACT, data: DATA, inlineLibs, variant, precompiledJs});
  const frame = page.frames().find((f) => f.parentFrame());
  try {
    await frame.waitForFunction(() => document.getElementById('log') && document.getElementById('log').textContent === 'a|b', null, {timeout: 20000});
  } catch (e) {
    console.log('DEBUG', variant, await page.evaluate(() => JSON.stringify(window.__msgs)), await frame.evaluate(() => document.getElementById('root').innerHTML.slice(0, 300)));
    throw e;
  }
  await frame.click('#inc');
  await frame.click('#inc');
  await page.waitForFunction(() => window.__msgs.some((m) => m === 'ARTIFACT_READY' || m.startsWith('ARTIFACT_ERROR')), null, {timeout: 10000});
  const result = await frame.evaluate(() => ({
    html: document.getElementById('root').innerHTML,
    dcl: window.__dcl,
    gen: window.__gen,
    computedPadding: getComputedStyle(document.getElementById('app')).padding,
    babel: typeof window.Babel,
  }));
  result.msgs = await page.evaluate(() => window.__msgs.filter((t) => t.startsWith('ARTIFACT_')));
  result.loadsBabel = babelOut;
  result.libRequests = libRequests;
  await page.close();
  return result;
}

try {
  const A = await render('A');
  assert.equal(A.babel, 'object', 'A compiles with Babel');
  assert.equal(A.gen.length, 1, 'Babel appended exactly one compiled script');
  assert.ok(A.msgs.includes('ARTIFACT_READY'), `A rendered: ${A.msgs}`);
  assert.ok(A.libRequests.filter((u) => u.endsWith('.js')).length >= 8, 'A loads its libs over the network');
  assert.equal(A.computedPadding, '16px', 'Tailwind applied');

  const B = await render('B');
  const C = await render('C', A.gen[0]);
  const D = await render('D', A.gen[0]);

  for (const [name, r] of [['B', B], ['C', C], ['D', D]]) {
    assert.equal(r.html, A.html, `${name}: rendered DOM matches the original path`);
    assert.deepEqual(r.msgs, A.msgs, `${name}: same ARTIFACT_* messages`);
    assert.deepEqual(r.dcl, A.dcl, `${name}: DOMContentLoaded listeners run in the same order`);
    assert.equal(r.computedPadding, A.computedPadding, `${name}: Tailwind applied`);
  }
  const scripts = (r) => r.libRequests.filter((u) => u.endsWith('.js'));
  assert.deepEqual(scripts(B), ['/libs/babel-standalone.min.js'], 'B: only Babel (never inlined) comes from the network');
  assert.equal(B.babel, 'object');
  assert.deepEqual(scripts(C), [], 'C: no network either');
  assert.equal(C.babel, 'undefined', 'C: Babel never ran');
  assert.deepEqual(C.gen, A.gen, 'C executes exactly the code Babel generated');
  assert.equal(D.babel, 'object', 'D: a key mismatch falls back to Babel');
  assert.deepEqual(D.gen, A.gen, 'D: Babel compiled the real code, the mismatched payload never ran');
  console.log('artifactPrecompiled: OK');
} finally {
  await browser.close();
}

// Real Chromium: the artifact iframe reports READY only once the app has
// replaced the "Loading…" placeholder, and reports an error when it never does.
//
// The placeholder lives inside #root until React's first commit replaces it.
// Counting it as content made an App that is never mounted (no
// ReactDOM.createRoot(...).render call) report READY and sit on "Loading…"
// forever instead of showing "Dashboard failed to render" + Fix Error.
//
//   cd frontend && node tests/unit/artifactMountReadiness.mjs
//   (PW_CHROMIUM_PATH=/opt/pw-browsers/chromium when the pinned Playwright's own
//    browser is not installed)
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import ts from 'typescript';
import {chromium} from 'playwright';
import {CHROME_PATH} from './support/chromium.mjs';

const root = new URL('../../', import.meta.url);
const source = fs.readFileSync(new URL('utils/artifactIframe.ts', root), 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
if (!fs.existsSync(new URL('public/libs/babel-standalone.min.js', root))) {
  console.log('SKIP: vendored libs missing (scripts/download-vendor-libs.sh)');
  process.exit(0);
}

const APP = `function App() {
  const data = useArtifactData();
  return <main><h1>Albums</h1><p>{data ? 'loaded' : 'loading'}</p></main>;
}
`;
const MOUNT = `ReactDOM.createRoot(document.getElementById('root')).render(<App />);\n`;
const babel = (body) => `<script type="text/babel">\n${body}</script>`;

const browser = await chromium.launch({headless: true, executablePath: CHROME_PATH});
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  const file = new URL('public' + u.pathname, root);
  if (u.pathname.startsWith('/libs/') && fs.existsSync(file)) {
    res.writeHead(200, {'Access-Control-Allow-Origin': '*', 'Content-Type': u.pathname.endsWith('.css') ? 'text/css' : u.pathname.endsWith('.woff2') ? 'font/woff2' : 'text/javascript'});
    return fs.createReadStream(file).pipe(res);
  }
  res.writeHead(u.pathname === '/' ? 200 : 404, {'Content-Type': 'text/html'});
  res.end('<html><body style="margin:0"></body></html>');
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const origin = `http://127.0.0.1:${server.address().port}`;

// The first ARTIFACT_READY / ARTIFACT_ERROR the iframe posts to its host.
async function firstSignal(code) {
  const page = await browser.newPage({viewport: {width: 800, height: 600}});
  try {
    await page.goto(origin + '/');
    await page.addScriptTag({content: 'window.exports={};' + compiled});
    await page.evaluate((code) => {
      window.__msgs = [];
      window.addEventListener('message', (e) => {
        if (e.data && typeof e.data.type === 'string' && e.data.type.startsWith('ARTIFACT_')) {
          window.__msgs.push({type: e.data.type, message: (e.data.payload && e.data.payload.message) || ''});
        }
      });
      const f = document.createElement('iframe');
      f.sandbox = 'allow-scripts';
      f.srcdoc = exports.buildArtifactIframeHtml({data: {visualizations: [], runtime: {version: 11}}, code, loadingLabel: 'Loading artifact…'});
      document.body.appendChild(f);
    }, code);
    await page.waitForFunction(() => window.__msgs.length > 0, null, {timeout: 30000});
    return (await page.evaluate(() => window.__msgs))[0];
  } finally {
    await page.close();
  }
}

try {
  const mounted = await firstSignal(babel(APP + MOUNT));
  assert.equal(mounted.type, 'ARTIFACT_READY', `a mounted app is ready: ${JSON.stringify(mounted)}`);

  // Mounting after the first readiness check (500ms) is a slow app, not a
  // broken one: it must still come up READY, not as an error.
  const slow = await firstSignal(babel(APP + `setTimeout(function () { ${MOUNT} }, 2000);\n`));
  assert.equal(slow.type, 'ARTIFACT_READY', `a late mount is still ready: ${JSON.stringify(slow)}`);

  const neverMounted = await firstSignal(babel(APP));
  assert.equal(neverMounted.type, 'ARTIFACT_ERROR', `an app that never mounts is an error: ${JSON.stringify(neverMounted)}`);
  assert.match(neverMounted.message, /createRoot/, 'the error says how to mount');

  const rendersNull = await firstSignal(babel('function App() { return null; }\n' + MOUNT));
  assert.equal(rendersNull.type, 'ARTIFACT_ERROR', `an app that renders nothing is an error: ${JSON.stringify(rendersNull)}`);

  console.log('ok - artifact mount readiness');
} finally {
  await browser.close();
  server.close();
}

// Real Chromium: runtime helpers reached through the `bow` object.
//
// Every component and helper is a bare global (vizById, KPICard, …). Models
// also write `const { KPICard, vizById } = bow`, `bow.useArtifactData()` and
// `<bow.KPICard>`, because the SDK features (bow.data, bow.records, …) do live
// on `bow`. Each of those was undefined and the page died with
// "useArtifactData is not a function". The helpers are now readable through
// `bow` too — ADDITIVELY: pages written with bare globals render exactly as
// before, the SDK's own members are untouched, and `bow` gains no enumerable
// keys.
//
//   cd frontend && node tests/unit/artifactBowHelpers.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
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

const VIZ = '3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b';
const DATA = {visualizations: [{id: VIZ, title: 'Albums per artist', rows: [{artist: 'Iron Maiden', albums: 21}, {artist: 'Led Zeppelin', albums: 14}], columns: [{field: 'artist'}, {field: 'albums'}]}]};
const BODY = (ns) => `
  const viz = ${ns}vizById("${VIZ}");
  const rows = (viz && viz.rows) || [];
  return (
    <div>
      <${ns}SectionCard title="Albums">
        <${ns}KPICard title="Artists" value={rows.length} />
        <p id="first">{rows[0] ? rows[0].artist : 'none'}</p>
        <p id="total">{${ns}fmt(rows.reduce((n, r) => n + r.albums, 0))}</p>
      </${ns}SectionCard>
    </div>
  );`;
const MOUNT = "ReactDOM.createRoot(document.getElementById('root')).render(<App />);";

const PAGES = {
  // How pages have always been written. Must keep rendering unchanged.
  'bare globals (existing dashboards)': `<script type="text/babel">
function App() { const data = useArtifactData(); if (!data) return <LoadingSpinner />; ${BODY('')} }
${MOUNT}
</script>`,
  'destructured from bow': `<script type="text/babel">
const { SectionCard, KPICard, LoadingSpinner, useArtifactData, vizById, fmt, setTheme } = bow;
setTheme('slate');
function App() { const data = useArtifactData(); if (!data) return <LoadingSpinner />; ${BODY('')} }
${MOUNT}
</script>`,
  'bow.<name> everywhere': `<script type="text/babel">
bow.setTheme('slate');
function App() { const data = bow.useArtifactData(); if (!data) return <bow.LoadingSpinner />; ${BODY('bow.')} }
${MOUNT}
</script>`,
};

const browser = await chromium.launch({headless: true, executablePath: CHROME_PATH});
try {
  for (const [name, code] of Object.entries(PAGES)) {
    const page = await browser.newPage();
    await page.route('**/*', async (route) => {
      const u = new URL(route.request().url());
      if (u.pathname.startsWith('/libs/')) {
        const file = new URL('public' + u.pathname, root);
        if (fs.existsSync(file)) return route.fulfill({path: file.pathname, headers: {'Access-Control-Allow-Origin': '*'}});
      }
      return route.fulfill({status: 200, body: '<html><body></body></html>', contentType: 'text/html'});
    });
    await page.goto('http://artifact-bow-helpers.test/');
    await page.addScriptTag({content: 'window.exports={};' + compiled});
    await page.evaluate(({code, data}) => {
      window.__msgs = [];
      window.addEventListener('message', (e) => { if (e.data && typeof e.data.type === 'string') window.__msgs.push(e.data.type + (e.data.payload && e.data.payload.message ? ': ' + e.data.payload.message : '')); });
      const f = document.createElement('iframe');
      f.sandbox = 'allow-scripts';
      f.srcdoc = exports.buildArtifactIframeHtml({data, code});
      document.body.appendChild(f);
    }, {code, data: DATA});
    await page.waitForFunction(() => window.__msgs.some((m) => m.startsWith('ARTIFACT_READY') || m.startsWith('ARTIFACT_ERROR')), null, {timeout: 20000});
    const msgs = await page.evaluate(() => window.__msgs.filter((m) => m.startsWith('ARTIFACT_')));
    assert.deepEqual(msgs, ['ARTIFACT_READY'], `[${name}] rendered without errors: ${msgs}`);
    const frame = page.frames().find((f) => f.parentFrame());
    await frame.waitForSelector('#first', {timeout: 10000});
    assert.equal(await frame.evaluate(() => document.getElementById('first').textContent), 'Iron Maiden', `[${name}] the page shows the visualization's rows`);
    assert.equal(await frame.evaluate(() => document.getElementById('total').textContent), '35', `[${name}] fmt() formats the total`);

    const bow = await frame.evaluate(() => ({
      exists: typeof window.bow === 'object' && window.bow !== null,
      // The SDK's own surface, as artifact-sdk.js defines it.
      sdk: [typeof bow.data, typeof bow.records.collection, typeof bow.context.get, typeof bow.markdown, bow.version].join(','),
      keys: Object.keys(bow).sort().join(','),
      same: bow.vizById === window.vizById && bow.KPICard === window.KPICard && bow.useArtifactData === window.useArtifactData,
      json: JSON.stringify(bow).includes('vizById'),
    }));
    assert.ok(bow.exists, `[${name}] window.bow is the SDK object`);
    assert.equal(bow.sdk.split(',').slice(1).join(','), 'function,function,function,1', `[${name}] SDK members are untouched: ${bow.sdk}`);
    assert.ok(bow.same, `[${name}] bow.<helper> is the very same global`);
    for (const helper of ['vizById', 'KPICard', 'useArtifactData', 'setTheme', 'useState'])
      assert.ok(!bow.keys.split(',').includes(helper), `[${name}] ${helper} is not an enumerable key of bow (${bow.keys})`);
    assert.equal(bow.json, false, `[${name}] serialising bow is unchanged`);
    await page.close();
    console.log(`PASS: ${name}`);
  }
  console.log('artifactBowHelpers: OK');
} finally {
  await browser.close();
}

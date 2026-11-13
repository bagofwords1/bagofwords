// The per-widget ⓘ provenance popover is a per-dashboard setting
// (show_widget_info → window.__BOW_INFO). buildArtifactIframeHtml writes the
// flag only when the option is given, so:
//   omitted  → the ⓘ shows (standalone / precompile / legacy renders)
//   false    → the ⓘ is gone (both the detached overlay and any inline popover)
//   true     → the ⓘ shows
// This covers the overlay path (a detached data-bow-viz marker) and the inline
// path (a KPI Card rendered with a viz, which mounts window.InfoPopover).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
import {CHROME_PATH} from './support/chromium.mjs';

const root = new URL('../../', import.meta.url);
const source = fs.readFileSync(new URL('utils/artifactIframe.ts', root), 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
const VIZ = '9f407b76-3037-4612-8c97-d3d9f23dba1b';

// A detached overlay owner (data-bow-viz) AND an inline Card bound to a viz
// (window.Card draws window.InfoPopover when given one).
const code = `<script type="text/babel">
function App() {
  return (
    <div className="p-6 space-y-6">
      <label data-bow-viz="${VIZ}">Project</label>
      {window.Card ? <window.Card title="Revenue" value="451" viz={{id:'${VIZ}',title:'Revenue'}} rows={[{Total:451}]} /> : null}
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const DATA = {runtime: {version: 11}, visualizations: [{id: VIZ, title: 'Revenue', rows: [{Total: 451}], columns: [{field: 'Total'}]}]};

const browser = await chromium.launch({headless: true, executablePath: CHROME_PATH});
try {
  // Every ⓘ affordance: detached overlay buttons (data-bow-ibtn) and inline
  // popover buttons (aria-label="Details" inside a data-bow-info span).
  async function infoCount(opts) {
    const page = await browser.newPage();
    await page.route('**/*', async route => {
      const u = new URL(route.request().url());
      if (u.pathname.startsWith('/libs/')) {
        const file = new URL('public' + u.pathname, root);
        if (fs.existsSync(file)) return route.fulfill({path: file.pathname});
      }
      return route.fulfill({status: 200, body: '<html><body></body></html>', contentType: 'text/html'});
    });
    await page.goto('http://artifact-review.test/');
    await page.addScriptTag({content: 'window.exports={};' + compiled});
    await page.evaluate(({code, data, opts}) => {
      const html = window.exports.buildArtifactIframeHtml(Object.assign({code, data}, opts));
      const el = document.createElement('iframe');
      el.style.width = '800px'; el.style.height = '400px'; el.srcdoc = html;
      document.body.appendChild(el);
    }, {code, data: DATA, opts});
    const frame = page.frames().find(fr => fr.parentFrame());
    // Wait for the app to render before counting: a fixed wait alone raced the
    // in-browser compile on a loaded machine and found 0 where the ⓘ shows.
    await frame.waitForSelector('[data-bow-viz]', {timeout: 30000});
    await page.waitForTimeout(500);
    const n = await frame.evaluate(() =>
      document.querySelectorAll('[data-bow-ibtn], [data-bow-info] button[aria-label="Details"]').length);
    await page.close();
    return n;
  }

  const omitted = await infoCount({});
  const off = await infoCount({showWidgetInfo: false});
  const on = await infoCount({showWidgetInfo: true});

  assert.ok(omitted >= 1, `flag omitted: the ⓘ shows (found ${omitted})`);
  assert.equal(off, 0, `show_widget_info off: no ⓘ anywhere (found ${off})`);
  assert.ok(on >= 1, `show_widget_info on: the ⓘ shows (found ${on})`);
  console.log(`PASS: per-dashboard ⓘ toggle — omitted=${omitted} (shown), off=0 (hidden), on=${on} (shown)`);
} finally {
  await browser.close();
}

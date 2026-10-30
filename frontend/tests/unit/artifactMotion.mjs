// Real Chromium: motion in artifacts — charts, modals, drawers, tabs.
//
// 1. EChart must not redraw from scratch when its option is rebuilt with the
//    same content. Artifact code builds `option` inline, so ANY state change
//    (opening a modal, switching a tab) used to call setOption(…, notMerge) on
//    every chart and replay its intro animation behind the overlay.
//    A data-only change must merge (bars morph) rather than rebuild.
// 2. Modal / Drawer stay mounted while they animate out, trap focus, close on
//    Escape (topmost only), restore focus and lock page scroll.
// 3. Tabs keep a visited panel mounted, so its chart is never re-initialised.
//
//   cd frontend && node tests/unit/artifactMotion.mjs
//   (PW_CHROMIUM=/opt/pw-browsers/chromium when the pinned Playwright's own
//    browser is not installed)
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import ts from 'typescript';
import {chromium} from 'playwright';

const root = new URL('../../', import.meta.url);
const source = fs.readFileSync(new URL('utils/artifactIframe.ts', root), 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
if (!fs.existsSync(new URL('public/libs/babel-standalone.min.js', root))) {
  console.log('SKIP: vendored libs missing (scripts/download-vendor-libs.sh)');
  process.exit(0);
}

// Counts echarts.init and every setOption call (with its merge mode).
const PROBE = `<script>
window.__ec = { inits: 0, rebuilds: 0, merges: 0 };
(function() {
  var init = echarts.init;
  echarts.init = function() {
    var c = init.apply(this, arguments);
    window.__ec.inits++;
    var set = c.setOption;
    c.setOption = function(opt, a) {
      var notMerge = a === true || !!(a && typeof a === 'object' && a.notMerge);
      if (notMerge) window.__ec.rebuilds++; else window.__ec.merges++;
      return set.apply(this, arguments);
    };
    return c;
  };
})();
</script>`;

const CHART_APP = `${PROBE}<script type="text/babel">
function App() {
  const [other, setOther] = useState(0);
  const [n, setN] = useState(3);
  const [extra, setExtra] = useState(false);
  const series = [{ type: 'bar', data: [1, 2, n], label: { show: true, formatter: (p) => p.value + 'x' } }];
  if (extra) series.push({ type: 'line', data: [3, 2, 1] });
  const option = { xAxis: { type: 'category', data: ['a', 'b', 'c'] }, yAxis: { type: 'value' }, series };
  return (
    <div>
      <button id="other" onClick={() => setOther(other + 1)}>other {other}</button>
      <button id="data" onClick={() => setN(n + 1)}>data</button>
      <button id="shape" onClick={() => setExtra(true)}>shape</button>
      <EChart option={option} height={200} />
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;

// Counts Tabs renders: an idle tab bar must not keep re-rendering itself.
const TABS_PROBE = `<script>
window.__tabRenders = 0;
(function() { var T = window.Tabs; if (T) window.Tabs = function(p) { window.__tabRenders++; return T(p); }; })();
</script>`;

const KIT_APP = `${PROBE}${TABS_PROBE}<script type="text/babel">
function App() {
  const [open, setOpen] = useState(false);
  const [nested, setNested] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [tab, setTab] = useState('chart');
  const option = { xAxis: { type: 'category', data: ['a', 'b'] }, yAxis: { type: 'value' }, series: [{ type: 'bar', data: [1, 2] }] };
  return (
    <div style={{ height: 3000 }}>
      <button id="open" onClick={() => setOpen(true)}>Open</button>
      <button id="drawer" onClick={() => setDrawer(true)}>Drawer</button>
      <Tabs value={tab} onChange={setTab} items={[
        { value: 'chart', label: 'Chart', content: <EChart option={option} height={160} /> },
        { value: 'table', label: 'Table', content: <p id="table-panel">rows</p> },
        { value: 'later', label: 'Later', content: () => <p id="later-panel">lazy</p> },
      ]} />
      <Modal open={open} onClose={() => setOpen(false)} title="Album details" subtitle="Sales" footer={<button id="nested-open" onClick={() => setNested(true)}>More</button>}>
        <input id="first-input" />
        <button id="last-btn">Save</button>
      </Modal>
      <Modal open={nested} onClose={() => setNested(false)} title="Nested" size="sm"><p id="nested-body">inner</p></Modal>
      <Drawer open={drawer} onClose={() => setDrawer(false)} title="Filters"><p id="drawer-body">filters</p></Drawer>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;

const launch = process.env.PW_CHROMIUM ? {headless: true, executablePath: process.env.PW_CHROMIUM} : {headless: true};
const browser = await chromium.launch(launch);

// A real local server (not page.route): request interception does not reach
// a sandboxed srcdoc iframe in every Chromium build.
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

async function render(code, dir) {
  const page = await browser.newPage({viewport: {width: 1000, height: 700}});
  if (process.env.DEBUG_MOTION) page.on('console', (m) => console.log('[console]', m.text().slice(0, 300)));
  await page.goto(origin + '/');
  await page.addScriptTag({content: 'window.exports={};' + compiled});
  await page.evaluate(({code, dir}) => {
    window.__msgs = [];
    window.addEventListener('message', (e) => { if (e.data && typeof e.data.type === 'string') window.__msgs.push(e.data.type + (e.data.payload && e.data.payload.message ? ': ' + e.data.payload.message : '')); });
    const f = document.createElement('iframe');
    f.sandbox = 'allow-scripts';
    f.style.cssText = 'width:1000px;height:700px;border:0';
    let html = exports.buildArtifactIframeHtml({data: {visualizations: [], runtime: {version: 11}}, code});
    if (dir) html = html.replace('<html', `<html dir="${dir}"`);
    f.srcdoc = html;
    document.body.appendChild(f);
  }, {code, dir});
  await page.waitForFunction(() => window.__msgs.some((m) => m.startsWith('ARTIFACT_READY') || m.startsWith('ARTIFACT_ERROR')), null, {timeout: 20000});
  const msgs = await page.evaluate(() => window.__msgs.filter((m) => m.startsWith('ARTIFACT_')));
  assert.deepEqual(msgs, ['ARTIFACT_READY'], `rendered without errors: ${msgs}`);
  return {page, frame: page.frames().find((f) => f.parentFrame())};
}

const ec = (frame) => frame.evaluate(() => ({...window.__ec}));
const settle = (frame, ms = 400) => frame.evaluate((t) => new Promise((r) => setTimeout(r, t)), ms);

try {
  // ── 1. EChart redraws ──────────────────────────────────────────────────────
  {
    const {page, frame} = await render(CHART_APP);
    await settle(frame);
    const start = await ec(frame);
    assert.equal(start.inits, 1, 'one chart instance');
    assert.ok(start.rebuilds <= 1, `mount builds the chart once (got ${start.rebuilds} rebuilds)`);

    for (let i = 0; i < 3; i++) await frame.click('#other');
    await settle(frame, 100);
    const afterOther = await ec(frame);
    assert.equal(afterOther.rebuilds, start.rebuilds, 'unrelated re-renders never rebuild the chart (no replayed intro animation)');
    assert.equal(afterOther.inits, 1, 'and never re-initialise it');

    await frame.click('#data');
    await settle(frame, 100);
    const afterData = await ec(frame);
    assert.equal(afterData.rebuilds, start.rebuilds, 'a data-only change morphs (merge), not rebuild');
    assert.ok(afterData.merges > afterOther.merges, 'the new data is applied');
    const shown = await frame.evaluate(() => echarts.getInstanceByDom(document.querySelector('[_echarts_instance_]')).getOption().series[0].data);
    assert.deepEqual(shown, [1, 2, 4], 'chart shows the new data');

    await frame.click('#shape');
    await settle(frame, 100);
    const afterShape = await ec(frame);
    assert.equal(afterShape.rebuilds, start.rebuilds + 1, 'a structural change (new series) rebuilds so nothing stale survives');
    const seriesCount = await frame.evaluate(() => echarts.getInstanceByDom(document.querySelector('[_echarts_instance_]')).getOption().series.length);
    assert.equal(seriesCount, 2);
    await page.close();
  }

  // ── 2. Modal / Drawer / Tabs ───────────────────────────────────────────────
  {
    const {page, frame} = await render(KIT_APP);
    await settle(frame);
    for (const g of ['Modal', 'Drawer', 'Tabs', 'usePresence']) {
      assert.equal(await frame.evaluate((n) => typeof window[n], g), 'function', `${g} is a sandbox global`);
    }
    const dialogs = () => frame.evaluate(() => document.querySelectorAll('[role="dialog"]').length);
    const opacityOf = (sel) => frame.evaluate((s) => { const e = document.querySelector(s); return e ? Number(getComputedStyle(e).opacity) : null; }, sel);
    const chartsBefore = await ec(frame);

    // Opens with an entrance animation, focus moves inside, page scroll locks
    // without the page shifting sideways as the scrollbar goes away.
    const xBefore = await frame.evaluate(() => document.getElementById('open').getBoundingClientRect().left);
    await frame.click('#open');
    await frame.waitForSelector('[role="dialog"]');
    const midEnter = await opacityOf('[role="dialog"]');
    assert.ok(midEnter < 1, `dialog animates in (opacity ${midEnter} right after opening)`);
    await settle(frame, 350);
    assert.equal(await opacityOf('[role="dialog"]'), 1, 'and settles fully visible');
    const dlg = await frame.evaluate(() => {
      const d = document.querySelector('[role="dialog"]');
      return {
        modal: d.getAttribute('aria-modal'),
        label: document.getElementById(d.getAttribute('aria-labelledby'))?.textContent,
        focusInside: d.contains(document.activeElement),
        scrollLocked: getComputedStyle(document.documentElement).overflow === 'hidden',
      };
    });
    assert.deepEqual(dlg, {modal: 'true', label: 'Album details', focusInside: true, scrollLocked: true});
    assert.equal(await frame.evaluate(() => document.getElementById('open').getBoundingClientRect().left), xBefore, 'the page does not shift when scroll locks');
    assert.equal((await ec(frame)).rebuilds, chartsBefore.rebuilds, 'opening the modal does not redraw the chart behind it');

    // Tab is trapped inside the dialog.
    for (let i = 0; i < 8; i++) {
      await frame.press('[role="dialog"]', 'Tab');
      assert.ok(await frame.evaluate(() => document.querySelector('[role="dialog"]').contains(document.activeElement)), 'focus stays in the dialog');
    }

    // Nested modal: Escape closes only the topmost.
    await frame.click('#nested-open');
    await frame.waitForSelector('#nested-body');
    await settle(frame, 300);
    await frame.evaluate(() => document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})));
    await settle(frame, 400);
    assert.equal(await frame.$('#nested-body'), null, 'Escape closed the nested dialog');
    assert.ok(await frame.$('#first-input'), 'and left the parent dialog open');

    // Closes with an exit animation: still mounted mid-exit, gone after.
    await frame.evaluate(() => document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})));
    await settle(frame, 40);
    assert.equal(await dialogs(), 1, 'dialog stays mounted while it animates out');
    const midExit = await opacityOf('[role="dialog"]');
    assert.ok(midExit < 1, `and is fading (opacity ${midExit})`);
    assert.equal(await frame.evaluate(() => getComputedStyle(document.querySelector('[role="dialog"]').parentElement).pointerEvents), 'none', 'an exiting overlay never blocks clicks');
    await settle(frame, 350);
    assert.equal(await dialogs(), 0, 'then unmounts');
    const restored = await frame.evaluate(() => ({id: document.activeElement?.id, overflow: getComputedStyle(document.documentElement).overflow}));
    assert.deepEqual(restored, {id: 'open', overflow: 'visible'}, 'focus returns to the trigger and scroll unlocks');

    // Backdrop click closes.
    await frame.click('#open');
    await settle(frame, 300);
    await page.mouse.click(5, 5);
    await settle(frame, 400);
    assert.equal(await dialogs(), 0, 'clicking the backdrop closes the dialog');

    // Drawer slides in from the inline end (right in LTR).
    await frame.click('#drawer');
    await frame.waitForSelector('#drawer-body');
    await settle(frame, 400);
    const box = await frame.evaluate(() => document.querySelector('[role="dialog"]').getBoundingClientRect().toJSON());
    assert.ok(Math.abs(box.right - 1000) < 2 && box.left > 400, `drawer docks to the right edge (${box.left}–${box.right})`);
    await frame.evaluate(() => document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})));
    await settle(frame, 400);
    assert.equal(await dialogs(), 0, 'drawer closes on Escape');

    // Tabs: lazy first visit, then kept mounted — the chart is never re-created.
    assert.equal(await frame.$('#later-panel'), null, 'an unvisited panel is not mounted');
    const tabs = [0, 1, 2].map((i) => frame.locator('[role="tab"]').nth(i));
    assert.equal(await frame.locator('[role="tab"]').count(), 3);
    assert.equal(await tabs[0].getAttribute('aria-selected'), 'true');
    const initsBefore = (await ec(frame)).inits;
    await tabs[1].click();
    await settle(frame, 300);
    assert.ok(await frame.isVisible('#table-panel'), 'second panel shows');
    // Idle after switching: no render loop (it starved real mouse input).
    const idleStart = await frame.evaluate(() => window.__tabRenders);
    await settle(frame, 600);
    assert.equal(await frame.evaluate(() => window.__tabRenders), idleStart, 'an idle tab bar does not re-render');

    await tabs[0].click();
    await settle(frame, 300);
    await tabs[1].click();
    await tabs[0].click();
    await settle(frame, 300);
    assert.equal((await ec(frame)).inits, initsBefore, 'switching back to a visited tab keeps its chart instance');
    const chartW = await frame.evaluate(() => document.querySelector('[_echarts_instance_] canvas').width);
    assert.ok(chartW > 100, 'the kept chart is still sized after being hidden');

    // Keyboard: arrows move between tabs, roving tabindex.
    await tabs[0].focus();
    await frame.press('[role="tab"][aria-selected="true"]', 'ArrowRight');
    assert.equal(await frame.evaluate(() => document.activeElement.textContent), 'Table');
    assert.equal(await tabs[1].getAttribute('aria-selected'), 'true', 'arrow keys select');
    assert.equal(await tabs[1].getAttribute('tabindex'), '0');
    assert.equal(await tabs[0].getAttribute('tabindex'), '-1');
    await frame.press('[role="tab"][aria-selected="true"]', 'End');
    await settle(frame, 300);
    assert.ok(await frame.isVisible('#later-panel'), 'End jumps to the last tab and lazily mounts it');

    // The indicator sits under the active tab.
    const ind = await frame.evaluate(() => {
      const t = document.querySelector('[role="tab"][aria-selected="true"]').getBoundingClientRect();
      const i = document.querySelector('[data-bow-tab-indicator]').getBoundingClientRect();
      return {dl: Math.abs(t.left - i.left), dw: Math.abs(t.width - i.width)};
    });
    assert.ok(ind.dl < 2 && ind.dw < 2, `indicator aligned with the active tab (${JSON.stringify(ind)})`);
    await page.close();
  }

  // ── 3. RTL: drawer docks to the inline end (left), arrows flip ─────────────
  {
    const {page, frame} = await render(KIT_APP, 'rtl');
    await settle(frame);
    await frame.click('#drawer');
    await frame.waitForSelector('#drawer-body');
    await settle(frame, 400);
    const box = await frame.evaluate(() => document.querySelector('[role="dialog"]').getBoundingClientRect().toJSON());
    assert.ok(box.left < 2 && box.right < 600, `RTL drawer docks to the left edge (${box.left}–${box.right})`);
    await frame.evaluate(() => document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})));
    await settle(frame, 400);
    await frame.focus('[role="tab"][aria-selected="true"]');
    await frame.press('[role="tab"][aria-selected="true"]', 'ArrowLeft');
    assert.equal(await frame.evaluate(() => document.activeElement.textContent), 'Table', 'ArrowLeft moves forward in RTL');
    const ind = await frame.evaluate(() => {
      const t = document.querySelector('[role="tab"][aria-selected="true"]').getBoundingClientRect();
      const i = document.querySelector('[data-bow-tab-indicator]').getBoundingClientRect();
      return Math.abs(t.left - i.left) + Math.abs(t.width - i.width);
    });
    await settle(frame, 300);
    const ind2 = await frame.evaluate(() => {
      const t = document.querySelector('[role="tab"][aria-selected="true"]').getBoundingClientRect();
      const i = document.querySelector('[data-bow-tab-indicator]').getBoundingClientRect();
      return Math.abs(t.left - i.left) + Math.abs(t.width - i.width);
    });
    assert.ok(ind2 < 3, `RTL indicator aligned with the active tab (off by ${ind2}, mid-slide ${ind})`);
    await page.close();
  }

  console.log('artifactMotion: OK');
} finally {
  await browser.close();
  server.close();
}

// Stored charts: the value axis and tooltip show units from the view's
// axisY.format, and a crowded new chart (showDataZoom) opens on its latest
// points. Both renderers use the same helper.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
const root = new URL('../../', import.meta.url);
const m = { exports: {} };
new Function('exports', 'module', ts.transpileModule(fs.readFileSync(new URL('utils/chartValueFormat.ts', root), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText)(m.exports, m);
const { valueFormatter, zoomWindow, latestWindowZoom } = m.exports;

assert.equal(valueFormatter(undefined), undefined, 'nothing stored: plain numbers, as before');
assert.equal(valueFormatter({ format: 'number' }), undefined);
const usd = valueFormatter({ format: 'currency' });
assert.equal(usd(1234.5), '$1,235'); assert.equal(usd(12.5), '$12.5'); assert.equal(usd(2_500_000), '$2.5M');
assert.equal(valueFormatter({ format: 'currency', currency: 'ils' })(40).includes('₪'), true, 'the stored currency code');
assert.equal(valueFormatter({ format: 'percent' })(12.5), '12.5%', 'percent: the value is already a percentage, as on metric cards');
assert.equal(valueFormatter({ format: 'compact' })(1500), '1.5K');
assert.equal(usd(null), '-'); assert.equal(usd(undefined), '-'); assert.equal(usd('n/a'), 'n/a');
assert.equal(zoomWindow(60, 3), 24); assert.equal(zoomWindow(24, 3), null); assert.equal(zoomWindow(60, 1), null); assert.equal(zoomWindow(90, 1), 60);
const z = latestWindowZoom(60, 3);
assert.deepEqual([z[0].startValue, z[0].endValue, z[1].type], [36, 59, 'slider'], 'opens on the latest 24 of 60');
assert.equal(latestWindowZoom(30, 1), null);
console.log('PASS: stored-chart value formats and the latest-window zoom');

const read = f => fs.readFileSync(new URL(f, root), 'utf8');
for (const f of ['components/RenderVisual.vue', 'components/dashboard/charts/EChartsVisual.vue']) {
  const src = read(f);
  assert.match(src, /valueFormatter\(/, `${f} formats the value axis`);
  assert.match(src, /showDataZoom \? latestWindowZoom\(/, `${f} opens crowded charts on the latest window`);
}
console.log('PASS: both renderers use them');

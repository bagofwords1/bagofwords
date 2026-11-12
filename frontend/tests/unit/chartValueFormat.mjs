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
// The latest points are wherever the newest categories are: the end when rows
// run oldest first, the start when they run newest first (ORDER BY ... DESC).
const months = Array.from({ length: 90 }, (_, i) => `${2018 + Math.floor(i / 12)}-${String(i % 12 + 1).padStart(2, '0')}`);
const asc = latestWindowZoom(months, 1), desc = latestWindowZoom([...months].reverse(), 1);
assert.deepEqual([asc[0].startValue, asc[0].endValue], [30, 89], 'oldest first: the trailing 60');
assert.deepEqual([desc[0].startValue, desc[0].endValue], [0, 59], 'newest first: the leading 60');
const quarters = Array.from({ length: 30 }, (_, i) => `${2018 + Math.floor(i / 4)}-Q${i % 4 + 1}`).reverse();
assert.equal(latestWindowZoom(quarters, 2)[0].startValue, 0, 'newest-first quarters');
const days = Array.from({ length: 70 }, (_, i) => new Date(Date.UTC(2025, 0, 1 + i)).toISOString().slice(0, 10)).reverse();
assert.equal(latestWindowZoom(days, 1)[0].startValue, 0, 'newest-first dates');
const names = Array.from({ length: 70 }, (_, i) => `item ${i}`);
assert.equal(latestWindowZoom(names, 1)[0].startValue, 10, 'labels without a time order keep the trailing window');
console.log('PASS: stored-chart value formats and the latest-window zoom');

const read = f => fs.readFileSync(new URL(f, root), 'utf8');
for (const f of ['components/RenderVisual.vue', 'components/dashboard/charts/EChartsVisual.vue']) {
  const src = read(f);
  assert.match(src, /valueFormatter\(/, `${f} formats the value axis`);
  assert.match(src, /showDataZoom \? latestWindowZoom\(/, `${f} opens crowded charts on the latest window`);
}
console.log('PASS: both renderers use them');

// Saving any setting in the chart editor rebuilds axisY; the stored units must survive it.
const editor = read('components/tools/VisualizationConfigEditor.vue');
assert.match(editor, /axisY\?\.format/, 'the editor reads the stored format');
assert.match(editor, /axisY\?\.currency/, 'the editor reads the stored currency');
assert.match(editor, /format: local\.yAxisFormat/, 'and writes the format back');
assert.match(editor, /currency: local\.yAxisCurrency/, 'and the currency');
console.log('PASS: the chart editor keeps the units');

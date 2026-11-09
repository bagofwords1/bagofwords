// Category labels: a saved value wins, "not stored" means automatic, and both
// renderers and the chart editor follow that rule.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
const root = new URL('../../', import.meta.url);
const m = { exports: {} };
new Function('exports', 'module', ts.transpileModule(fs.readFileSync(new URL('utils/chartAxisLabels.ts', root), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText)(m.exports, m);
const { autoAxisLabels: auto, resolveAxisLabels: resolve } = m.exports;

assert.deepEqual(auto(60), { interval: 3, rotate: 45, hideOverlap: true }, '60 categories print every 4th label');
assert.equal(auto(12).interval, 0, '12 labels fit, even in a narrow column');
assert.equal(auto(18).interval, 0);
assert.equal(auto(19).interval, 1, 'past 18, rotated labels collide in a narrow column');
assert.equal(auto(8).interval, 0);
assert.equal(auto(4).rotate, 0, 'a handful of labels stay upright');

for (const axis of [undefined, null, {}, { show: true }, { rotate: null, interval: null }])
  assert.deepEqual(resolve(axis, 60), auto(60), `nothing stored is automatic: ${JSON.stringify(axis)}`);

assert.deepEqual(resolve({ rotate: 45, interval: 0 }, 60), { rotate: 45, interval: 0, hideOverlap: true }, 'a saved "show all" is respected');
assert.equal(resolve({ rotate: 90 }, 60).rotate, 90);
assert.equal(resolve({ rotate: 90 }, 60).interval, 3, 'a saved angle with no spacing still thins');
console.log('PASS: saved label spacing wins; nothing stored is automatic');

const read = f => fs.readFileSync(new URL(f, root), 'utf8');
assert.match(read('components/RenderVisual.vue'), /resolveAxisLabels\(props\.view\?\.view\?\.axisX, numCategories\)/);
assert.match(read('components/dashboard/charts/EChartsVisual.vue'), /resolveAxisLabels\(viewV2\.axisX, numCategories\)/);
const editor = read('components/tools/VisualizationConfigEditor.vue');
assert.doesNotMatch(editor, /interval: local\.xAxisInterval \?\? 0/, 'choosing Auto must not save "show all"');
assert.match(editor, /xAxisInterval: v\.axisX\?\.interval \?\? v\.xAxisLabelInterval \?\? null/, 'a chart with nothing stored opens on Auto');
assert.doesNotMatch(editor, /rotate: local\.xAxisRotate \?\? 45/, 'choosing Auto must not save 45 degrees');
assert.match(editor, /xAxisRotate: v\.axisX\?\.rotate \?\? v\.xAxisLabelRotate \?\? null/, 'a chart with nothing stored opens on Auto rotation');
console.log('PASS: both renderers and the chart editor follow it');

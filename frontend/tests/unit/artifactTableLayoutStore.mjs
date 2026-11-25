// The host side of DataTable layouts (utils/artifactTableLayout.ts): what the
// sandboxed dashboard posts is untrusted, so only bounded, well-formed widths
// (column -> sane px) and orders (distinct column names) are kept, per table,
// per dashboard, in this browser; an empty layout forgets the table; blocked
// storage never throws.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import ts from 'typescript';
const root = new URL('../../', import.meta.url);
const src = fs.readFileSync(new URL('utils/artifactTableLayout.ts', root), 'utf8');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tl-'));
const file = path.join(tmp, 'l.mjs');
fs.writeFileSync(file, ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText);
const store = new Map();
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) };
const l = await import(file);

assert.deepEqual(l.loadTableLayouts('d1'), {}, 'nothing saved yet');
assert.ok(l.handleTableLayoutMessage({ type: 'ARTIFACT_TABLE_LAYOUT', key: 'viz:a', widths: { Name: 240.4, Price: 2 }, order: ['Price', 'Name'] }, 'd1'), 'a layout message is handled');
assert.deepEqual(l.loadTableLayouts('d1'), { 'viz:a': { widths: { Name: 240, Price: 40 }, order: ['Price', 'Name'] } }, 'widths rounded and clamped, order kept');
assert.equal(l.handleTableLayoutMessage({ type: 'ARTIFACT_DATA' }, 'd1'), false, 'other messages are not ours');
l.saveTableLayout('d1', 'viz:a', { Name: 'wide', '': 10, ok: Infinity, Real: 120 }, ['A', 'A', 7, '', 'B']);
assert.deepEqual(l.loadTableLayouts('d1')['viz:a'], { widths: { Real: 120 }, order: ['A', 'B'] }, 'junk widths dropped; order de-duplicated, names only');
l.saveTableLayout('d1', 'viz:b', undefined, ['X', 'Y']);
assert.deepEqual(l.loadTableLayouts('d1')['viz:b'], { widths: {}, order: ['X', 'Y'] }, 'an order alone is a layout');
assert.equal(l.saveTableLayout('d1', 'x'.repeat(500), { a: 100 }, []), false, 'an oversized key is refused');
assert.equal(l.saveTableLayout('d1', 'viz:c', ['not', 'a', 'map'], []), false, 'widths must be a map');
assert.equal(l.saveTableLayout('d1', 'viz:c', {}, 'Name'), false, 'order must be a list');
assert.equal(l.saveTableLayout('', 'viz:a', { a: 100 }, []), false, 'no dashboard id, nothing stored');
l.saveTableLayout('d1', 'viz:a', {}, []);
assert.equal(l.loadTableLayouts('d1')['viz:a'], undefined, 'an empty layout forgets the table');
const many = Object.fromEntries([...Array(500)].map((_, i) => [`c${i}`, 100]));
l.saveTableLayout('d1', 'viz:a', many, Object.keys(many));
assert.equal(Object.keys(l.loadTableLayouts('d1')['viz:a'].widths).length, 100, 'columns per table are capped (widths)');
assert.equal(l.loadTableLayouts('d1')['viz:a'].order.length, 100, 'columns per table are capped (order)');
// A generated dashboard can't fill the origin's storage: per-dashboard and total byte budgets.
const longName = (i) => `${'x'.repeat(110)}${i}`;
const fat = Object.fromEntries([...Array(100)].map((_, i) => [longName(i), 100]));
let refused = false;
for (let t = 0; t < 20; t++) if (!l.saveTableLayout('greedy', `viz:${t}`, fat, Object.keys(fat))) refused = true;
assert.ok(refused, 'one dashboard past its byte budget is refused');
assert.ok(JSON.stringify(l.loadTableLayouts('greedy')).length <= 32 * 1024 + 2048, 'and stays within it');
for (let d = 0; d < 40; d++) for (let t = 0; t < 4; t++) l.saveTableLayout(`big${d}`, `viz:${t}`, fat, []);
assert.ok(store.get('bow.artifactTableLayouts.v1').length <= 256 * 1024, `the whole store stays within its byte budget (${store.get('bow.artifactTableLayouts.v1').length})`);
assert.ok(Object.keys(l.loadTableLayouts('big39')).length > 0, 'the newest dashboard is kept');
for (let i = 0; i < 120; i++) l.saveTableLayout(`dash${i}`, 'viz:a', { a: 100 }, []);
assert.ok(Object.keys(JSON.parse(store.get('bow.artifactTableLayouts.v1'))).length <= 100, 'dashboards are capped (oldest dropped)');
globalThis.localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
assert.deepEqual(l.loadTableLayouts('d1'), {}, 'blocked storage reads as nothing saved');
assert.equal(l.saveTableLayout('d1', 'viz:a', { a: 100 }, []), false, 'and a save does not throw');
for (const host of ['components/dashboard/ArtifactFrame.vue', 'components/dashboard/SharedDashboardView.vue']) {
  const s = fs.readFileSync(new URL(host, root), 'utf8');
  assert.match(s, /handleTableLayoutMessage\(event\.data, /, `${host}: stores layouts sent by its own frame`);
  assert.match(s, /table_layouts: loadTableLayouts\(/, `${host}: hands saved layouts back with the data`);
}
fs.rmSync(tmp, { recursive: true, force: true });
console.log('PASS: dashboard table layouts (widths + column order) are validated, capped and kept per dashboard by the host page');

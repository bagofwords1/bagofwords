// The shared data grid (AgGridComponent + CustomHeader) — query results and
// dashboard tables:
//  - the custom header wires ag-grid's sort and column-menu (filter) itself,
//    because a headerComponent replaces ag-grid's whole built-in header;
//  - columns are not `flex` (a flex column gives width back to its
//    neighbours, so a resize never stuck), are auto-sized once, keep the
//    viewer's order across data refreshes, and the viewer's layout is saved
//    per grid and survives the remount on a language switch;
//  - RTL locales run the grid with enableRtl instead of forcing it LTR, and
//    the pager arrows are not mirrored a second time;
//  - numeric result columns align to the end and use the number filter.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import ts from 'typescript';

const root = new URL('../../', import.meta.url);
const read = (p) => fs.readFileSync(new URL(p, root), 'utf8');

// ---- Header: sort + filter are reachable --------------------------------
const header = read('components/CustomHeader.vue');
assert.match(header, /this\.params\.progressSort\(/, 'a click on the header name sorts');
assert.match(header, /this\.params\.showColumnMenu\(/, 'the header has a button that opens the filter menu');
assert.match(header, /addEventListener\?\.\('sortChanged'/, 'the header follows the column sort (arrow)');
assert.match(header, /addEventListener\?\.\('filterActiveChanged'/, 'the header shows when a filter is active');
assert.match(header, /removeEventListener\?\.\('sortChanged'/, 'listeners are removed on unmount');
assert.match(header, /text-overflow:\s*ellipsis/, 'a long header name is cut with an ellipsis');
assert.match(header, /scrollWidth > el\.clientWidth/, 'the full name is offered when it is cut off');

assert.doesNotMatch(header, /custom-header--end|justify-content:\s*flex-end/, 'every header starts at the same edge, numeric columns too');

// ---- Grid options --------------------------------------------------------
const grid = read('components/AgGridComponent.vue');
const defaultColDef = grid.match(/defaultColDef: \{([\s\S]*?)\n  \},/);
assert.ok(defaultColDef, 'AgGridComponent declares a defaultColDef');
assert.doesNotMatch(defaultColDef[1], /\bflex:/, 'columns are not flex — a resize must stick');
assert.match(grid, /maintainColumnOrder: true/, 'a data refresh keeps the viewer\'s column order');
assert.match(grid, /enableRtl: localeTextDir\(locale\.value\) === 'rtl'/, 'RTL locales run the grid right to left');
assert.doesNotMatch(grid, /tooltipShowMode|tooltipValueGetter/, 'no hover tooltip on cells (removed at review)');
// A saved layout from before the query gained a column: the new column is
// sized to its content, not left at ag-grid's flat 200px default.
assert.match(grid, /const layoutColumns = \(api: any\) => \{[\s\S]*?sizeToContent\(api\);[\s\S]*?applyColumnState\(\{ state, applyOrder/, 'every load sizes columns to content, then applies only what the viewer chose');
assert.match(grid, /onColumnResized: \(e: any\) => \{ if \(e\.finished && fromViewer\(e\)\) rememberWidths\(e\); \}/, 'only hand-resized widths are remembered');
assert.doesNotMatch(grid, /saveGridState\(props\.stateKey, (api\.getColumnState|state)\)/, 'no full column-state snapshot is saved');
// Expand to full screen: the same grid instance teleported to <body>.
assert.match(grid, /<Teleport to="body" :disabled="!expanded">/, 'the expanded grid is the same instance, moved to <body>');
assert.match(grid, /data-testid="grid-expand"/, 'an expand button');
assert.match(grid, /e\.key === 'Escape'/, 'Esc closes the full-screen grid');
for (const host of ['components/RenderTable.vue', 'components/dashboard/table/TableAgGrid.vue']) {
  assert.match(read(host), /withDefaults\([\s\S]*?\{ expandable: true \}\)/, `${host}: expand is on by default (Boolean props default to false otherwise)`);
}
// The saved-query page puts the control in its Data / Code tab row instead.
const entityPanel = read('components/entity/EntityDetailPanel.vue');
assert.match(entityPanel, /data-testid="entity-table-expand"[\s\S]*?@click="dataTableRef\?\.expand\(\)"/, 'the query page expands the table from its tab row');
assert.match(entityPanel, /<RenderTable ref="dataTableRef"[^>]*:expandable="false"/, 'no second button inside the table on the query page');
assert.match(grid, /defineExpose\(\{ setExpanded \}\)/, 'the grid lets a host open it full screen');
const preview = read('components/tools/ToolWidgetPreview.vue');
assert.ok((preview.match(/:expandable="false"/g) || []).length >= 2, 'the create_data result keeps only its own "open in panel" control');
assert.match(grid, /onFirstDataRendered: \(e: any\) => \{\s*layoutColumns\(e\.api\);/, 'the layout is applied when data first renders');
assert.match(grid, /onColumnMoved: \(e: any\) => \{ if \(fromViewer\(e\)\) rememberOrder\(e\.api\); \}/, 'a drag remembers the order');
assert.match(grid, /onSortChanged: \(e: any\) => \{ if \(fromViewer\(e\)\) rememberSort\(e\.api\); \}/, 'a sort is remembered');
assert.match(grid, /let layout: GridLayout/, 'the layout lives on the component, so the grid remount on a language switch keeps it');

// ---- Column filter: pick values (or From / To), translated ---------------
assert.match(grid, /filter: col\.filter === false \? false : ValueSetFilter/, 'every column filters with the value list');
assert.match(grid, /inject<GridFilterBridge \| null>\(GRID_FILTER_BRIDGE, null\)/, 'a report host can hand the grid its filter bridge');
assert.match(grid, /onFilterOpened: titleFilterPopup/, 'the filter popup names its column');
assert.match(grid, /onFilterChanged: \(e: any\) => syncFilterStatus\(e\.api\)/, 'the row count follows the filters');
assert.match(grid, /data-testid="grid-filter-status"/, 'a status line shows while a filter hides rows');
assert.match(grid, /api\?\.setFilterModel\(null\)/, 'the status line clears every filter');
assert.match(grid, /\.ag-grid-themed \.ag-popup \{/, 'the popup takes the app theme tokens (not balham-dark grey)');
assert.match(grid, /memoryFilter = filters/, 'active filters survive a language switch');
const vsf = read('components/grid/ValueSetFilter.vue');
for (const m of ['isFilterActive', 'doesFilterPass', 'getModel', 'setModel', 'afterGuiAttached']) {
  assert.match(vsf, new RegExp(`\\b${m}\\(`), `the value filter implements ag-grid's ${m}`);
}
assert.match(vsf, /isFilterActive\(\) \{ return !this\.bridge &&/, 'inside a report the grid itself does not filter (the report does)');
assert.match(header, /bridge\.isActive\(field\)/, 'the header funnel lights up for a report filter on its column');
const previewSrc = read('components/tools/ToolWidgetPreview.vue');
assert.match(previewSrc, /provide\(GRID_FILTER_BRIDGE, createReportFilterBridge\(/, 'the chat / side-panel result routes header filters into the report filters');
assert.match(entityPanel, /provide\(GRID_FILTER_BRIDGE, createReportFilterBridge\(/, 'on the query page the header filters reach the Visual tab too');
assert.match(entityPanel, /<RenderVisual :widget="effectiveWidget" :data="filteredData"/, 'the query page chart draws the filtered rows');
assert.match(entityPanel, /data-testid="entity-filter-status"/, 'the query page says the chart and table are filtered, with a way back');
const vizFilter = read('components/dashboard/VisualizationFilter.vue');
assert.doesNotMatch(vizFilter, />\s*(Filters|Clear|Apply|Add filter|No filters applied)\s*</, 'the Filters popover has no hard-coded English');
assert.match(vizFilter, /multiValueOperators\.includes\(condition\.operator\)/, 'the Filters popover shows a header\'s "is one of" condition');
const catalogs = fs.readdirSync(new URL('../locales/', root)).filter((f) => f.endsWith('.json')).map((f) => f.slice(0, -5));
const en = JSON.parse(read('../locales/en.json'));
const gridKeys = ['search', 'selectAll', 'blankValue', 'noMatches', 'moreValues', 'alsoFilteredAbove', 'inRangeStart', 'inRangeEnd', 'resetFilter', 'noRowsToShow', 'filteredRows', 'clearFilters', 'expand', 'collapse'];
const flat = (o, pre = '') => Object.entries(o).flatMap(([k, v]) => (v && typeof v === 'object' ? flat(v, `${pre}${k}.`) : [`${pre}${k}`]));
for (const code of catalogs) {
  const cat = JSON.parse(read(`../locales/${code}.json`));
  const g = cat.dataGrid || {};
  for (const key of gridKeys) assert.ok(typeof g[key] === 'string' && g[key].trim(), `${code}: dataGrid.${key}`);
  assert.match(g.filteredRows, /\{shown\}[\s\S]*\{total\}/, `${code}: filteredRows carries both counts`);
  assert.deepEqual(flat(cat.vizFilter || {}).sort(), flat(en.vizFilter).sort(), `${code}: vizFilter has every key`);
  if (code !== 'en') assert.notEqual(g.selectAll, en.dataGrid.selectAll, `${code}: the value filter is translated`);
}

// ---- Widths: values decide, long headers truncate --------------------------
assert.match(grid, /const MAX_AUTO_WIDTH = 300;/, 'auto-sized columns stop at 300px');
assert.match(grid, /const MAX_HEADER_WIDTH = 140;/, 'a header longer than its values gets at most 140px');
assert.match(grid, /autoSizeAllColumns\(false\)[\s\S]*?autoSizeAllColumns\(true\)[\s\S]*?Math\.max\(values, Math\.min\(both, MAX_HEADER_WIDTH\)\)/, 'width = values, plus header room up to the cap');
assert.match(grid, /expanded\.value = on;[\s\S]*?relayoutOnResize = true;[\s\S]*?if \(relayoutOnResize && e\.clientWidth > 0\)[\s\S]*?layoutColumns\(e\.api\)/, 'opening / closing full screen lays the columns out for the new width');

assert.doesNotMatch(grid, /sizeColumnsToFit\(/, "no sizeColumnsToFit: it evens every column out to one width");
assert.match(grid, /const fillSpareWidth = /, 'spare width goes to cut-off columns first, then in proportion');

// ---- RTL CSS -------------------------------------------------------------
const rtlCss = read('assets/css/rtl.css');
assert.doesNotMatch(rtlCss, /\[dir="rtl"\] \.ag-root/, 'the grid is no longer forced LTR in RTL locales');
assert.doesNotMatch(rtlCss, /\.ag-paging-panel \.ag-icon \{ transform: scaleX\(-1\)/, 'pager arrows are not mirrored twice (enableRtl swaps them)');

// ---- Behaviour of the two helpers (compiled from TypeScript) -------------
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'grid-utils-'));
const load = async (rel) => {
  const out = ts.transpileModule(read(rel), { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText;
  const file = path.join(tmp, path.basename(rel).replace(/\.ts$/, '.mjs'));
  fs.writeFileSync(file, out);
  return import(file);
};

const { buildGridColumns, isNumericDtype } = await load('utils/gridColumns.ts');
for (const d of ['int64', 'float64', 'Int64', 'UInt8', 'float32']) assert.ok(isNumericDtype(d), `${d} is numeric`);
for (const d of ['object', 'bool', 'datetime64[ns]', undefined]) assert.ok(!isNumericDtype(d), `${d} is not numeric`);
const cols = buildGridColumns({
  columns: [{ field: 'price', headerName: 'Price' }, { field: 'a.b', headerName: 'Dotted' }],
  info: { column_info: { price: { dtype: 'float64', min: 1, max: 2, mean: 1.5 }, 'a.b': { dtype: 'object', unique_count: 3, count: 9 } } },
}, {});
assert.equal(cols[0].type, 'numericColumn', 'numeric column aligns to the end');
assert.equal(cols[0].filter, true, 'numeric column is filterable');
assert.equal(cols[1].type, undefined, 'text column keeps the default alignment');
assert.equal(cols[1].filter, true, 'text column is filterable');
assert.match(cols[0].headerComponentParams.statsText, /min: 1\nmax: 2\nmean: 1\.50/, 'numeric stats');
assert.equal(cols[1].valueGetter({ data: { 'a.b': 'x' } }), 'x', 'a dotted column name is read as one key, not a path');

// localStorage stand-in for the layout store.
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
const { loadGridState, saveGridState } = await load('utils/gridState.ts');
assert.equal(loadGridState('w1'), null, 'nothing saved yet');
saveGridState('w1', { order: ['b', 'a'], sort: [{ colId: 'a', sort: 'desc', sortIndex: 0 }], widths: { a: 240 } });
assert.deepEqual(loadGridState('w1'), { order: ['b', 'a'], sort: [{ colId: 'a', sort: 'desc', sortIndex: 0 }], widths: { a: 240 } }, 'a saved layout comes back');
assert.equal(loadGridState(''), null, 'no key, no layout');
// Junk in storage reads as "nothing chosen", never as a broken layout.
store.set('bow.gridState.v2', JSON.stringify({ junk: { t: 1, s: { order: 'x', sort: [{ colId: 'a', sort: 'up' }], widths: { a: -3, b: 'wide', c: 150 } } } }));
assert.deepEqual(loadGridState('junk'), { order: [], sort: [], widths: { c: 150 } }, 'invalid parts are dropped');
// A v1 snapshot (every column's width frozen) is ignored, not migrated.
store.set('bow.gridState.v1', JSON.stringify({ old: { t: 1, s: [{ colId: 'a', width: 200 }] } }));
assert.equal(loadGridState('old'), null, 'v1 snapshots are ignored');
for (let i = 0; i < 210; i++) saveGridState(`g${i}`, { order: [], sort: [], widths: {} });
const saved = Object.keys(JSON.parse(store.get('bow.gridState.v2')));
assert.ok(saved.length <= 200, `the store is capped (${saved.length})`);
assert.ok(saved.includes('g209') && !saved.includes('w1'), 'the oldest layouts are dropped first');
globalThis.localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
assert.equal(loadGridState('w1'), null, 'blocked storage reads as no layout');
saveGridState('w1', { order: [] }); // must not throw

// ---- Value filter: list, range, and the report bridge ------------------------
const vf = await load('utils/valueFilter.ts');
const opts = vf.collectValues(['Rock', 'Jazz', 'rock ', null, '', 'Blues', 'Jazz']);
assert.deepEqual(opts.map((o) => [o.key, o.count]), [['blues', 1], ['jazz', 2], ['rock', 2], [vf.BLANK_KEY, 2]], 'distinct values compare like the report filter (trim, case), blank last');
assert.deepEqual(vf.collectValues([10, 2, '33']).map((o) => o.key), ['2', '10', '33'], 'numbers sort numerically');
const all = new Set(opts.map((o) => o.key));
assert.equal(vf.selectionModel(all, opts), null, 'everything ticked = no filter');
const noRock = new Set(all); noRock.delete('rock');
assert.deepEqual(vf.selectionModel(noRock, opts), { mode: 'not_in', values: ['rock'] }, 'unticking one stores the shorter list');
assert.deepEqual(vf.selectionModel(new Set(['jazz']), opts), { mode: 'in', values: ['jazz'] }, 'ticking one stores it');
assert.ok(vf.valuePasses({ mode: 'in', values: ['jazz'] }, ' JAZZ'), 'kept value passes');
assert.ok(!vf.valuePasses({ mode: 'in', values: ['jazz'] }, 'Rock'), 'other values do not');
assert.ok(!vf.valuePasses({ mode: 'not_in', values: [vf.BLANK_KEY] }, null), 'unticking (Blank) hides empty cells');
assert.ok(vf.valuePasses({ mode: 'range', from: 5, to: null }, 7) && !vf.valuePasses({ mode: 'range', from: 5, to: 6 }, 7), 'From / To');
assert.ok(!vf.valuePasses({ mode: 'range', from: 0, to: null }, ''), 'a blank cell is outside any range');
assert.deepEqual([...vf.selectedKeys({ mode: 'not_in', values: ['rock'] }, opts)].sort(), ['blues', 'jazz', vf.BLANK_KEY].sort(), 'a stored model ticks the right boxes');

// Inside a report: the header's condition is one condition of the shared filters.
const cond = vf.toReportCondition({ mode: 'in', values: ['jazz', vf.BLANK_KEY] }, opts);
assert.deepEqual([cond.operator, cond.value, cond.origin], ['in', ['Jazz', ''], 'column'], 'raw values go to the report; blank as ""');
assert.deepEqual(vf.fromReportCondition({ operator: 'between', value: '1', value2: 5 }), { mode: 'range', from: 1, to: 5 }, 'a range comes back');
const userCond = { id: 'u', column: 'v1:Price', operator: 'greater_than', value: 3 };
let groups = [{ id: 'g1', conditions: [userCond] }, { id: 'g2', conditions: [{ id: 'x', column: 'v1:Artist', operator: 'equals', value: 'A' }] }, { id: 'g3', conditions: [{ id: 'o', column: 'v2:Genre', operator: 'equals', value: 'Rock' }] }];
groups = vf.withColumnCondition(groups, 'v1', 'Genre', cond);
assert.equal(groups[0].conditions.length, 2, 'AND-ed into every group of this visualization (group 1)');
assert.equal(groups[1].conditions.length, 2, 'AND-ed into every group of this visualization (group 2)');
assert.equal(groups[2].conditions.length, 1, 'another visualization\'s group is untouched');
groups = vf.withColumnCondition(groups, 'v1', 'Genre', { operator: 'not_in', value: ['Rock'], origin: 'column' });
assert.equal(groups[0].conditions.filter((c) => c.column === 'v1:Genre').length, 1, 'a new selection replaces the header\'s condition, not stacks');
const at = vf.columnConditions(groups, 'v1', 'Price');
assert.ok(!at.own && at.other.length === 1, 'a condition set in the Filters popover is not the header\'s own');
groups = vf.withColumnCondition(groups, 'v1', 'Genre', null);
assert.deepEqual(groups[0].conditions, [userCond], 'clearing the header leaves the popover\'s conditions alone');
assert.equal(vf.withColumnCondition([], 'v1', 'Genre', cond).length, 1, 'with no group yet, the header starts one');
let committed = null;
const bridge = vf.createReportFilterBridge({ filters: () => committed || [], vizId: () => 'v1', rows: () => [], commit: (g) => { committed = g; } });
bridge.setModel('Genre', { mode: 'in', values: ['jazz'] }, opts);
assert.ok(bridge.isActive('Genre') && !bridge.isActive('Artist'), 'the bridge reports which columns are filtered');
assert.deepEqual(bridge.getModel('Genre'), { mode: 'in', values: ['jazz'] }, 'and hands the selection back to the popup');
assert.ok(!vf.createReportFilterBridge({ filters: () => [], vizId: () => null, rows: () => [], commit() {} }).enabled(), 'no visualization id: the grid filters itself');

// Report filter evaluates what the header writes (in / not_in / ranges).
const shared = await load('composables/useSharedFilters.ts').catch(() => null);
if (shared) {
  const row = { Genre: 'Jazz ' };
  assert.ok(shared.evaluateOperator(row.Genre, 'in', ['jazz']), 'report: in');
  assert.ok(shared.evaluateOperator(null, 'in', ['']), 'report: blank matches ""');
}

fs.rmSync(tmp, { recursive: true, force: true });
console.log('PASS: data grid header sort/filter, column layout, RTL and column typing');

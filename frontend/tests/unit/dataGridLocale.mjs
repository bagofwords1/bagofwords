// The ag-grid paging footer ("Page Size", "1 to 50 of 236", "Page 1 of 5") and
// the shared viewer's "No dashboard available" are translated in every locale
// catalog, the grid picks up a language switch, and RTL locales lay the footer
// out right to left.
import assert from 'node:assert/strict';
import fs from 'node:fs';

const root = new URL('../../', import.meta.url);
const component = fs.readFileSync(new URL('components/AgGridComponent.vue', root), 'utf8');
const list = component.match(/const PAGER_KEYS = \[([^\]]+)\]/);
assert.ok(list, 'AgGridComponent lists the pager keys it translates');
const keys = [...list[1].matchAll(/'([A-Za-z]+)'/g)].map(m => m[1]);
assert.ok(keys.includes('pageSizeSelectorLabel') && keys.includes('of') && keys.includes('to'), 'the pager keys are requested');
assert.match(component, /t\(`dataGrid\.\$\{key\}`\)/, 'each key is read from the dataGrid namespace');
assert.match(component, /localeText: pagerText\(\)/, 'the grid options carry the translated pager');
// ag-grid reads localeText only when the grid is created: a language switch
// must rebuild the options and remount the grid.
assert.match(component, /:key="gridKey"/, 'the grid is keyed so it can remount');
assert.match(component, /watch\(locale, \(\) => \{\s*gridOptions\.value = buildOptions\(\);\s*gridKey\.value\+\+;/, 'a language switch rebuilds and remounts the grid');

const catalogs = fs.readdirSync(new URL('../locales/', root)).filter(f => f.endsWith('.json')).map(f => f.slice(0, -5));
assert.ok(catalogs.length >= 10, `found ${catalogs.length} locale catalogs`);
const read = code => JSON.parse(fs.readFileSync(new URL(`../locales/${code}.json`, root), 'utf8'));
const en = read('en');
for (const code of catalogs) {
  const cat = read(code);
  const strings = cat.dataGrid || {};
  for (const key of keys) assert.ok(typeof strings[key] === 'string' && strings[key].trim(), `${code}: dataGrid.${key}`);
  assert.ok(typeof cat.sharedReport?.noDashboard === 'string' && cat.sharedReport.noDashboard.trim(), `${code}: sharedReport.noDashboard`);
  if (code !== 'en') {
    assert.notDeepEqual(keys.map(k => strings[k]), keys.map(k => en.dataGrid[k]), `${code} pager is translated`);
    assert.notEqual(cat.sharedReport.noDashboard, en.sharedReport.noDashboard, `${code} noDashboard is translated`);
  }
}

// In RTL locales the translated footer runs right to left, or the words and
// numbers come out in a scrambled order.
const rtlCss = fs.readFileSync(new URL('assets/css/rtl.css', root), 'utf8');
assert.match(rtlCss, /html\[dir="rtl"\] \.ag-paging-panel \{ direction: rtl; \}/, 'RTL locales lay the paging footer out right to left');
console.log(`PASS: ${keys.length} pager strings and the empty-viewer text in all ${catalogs.length} catalogs; the grid remounts on a language switch`);

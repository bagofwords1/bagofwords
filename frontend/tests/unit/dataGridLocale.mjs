// The ag-grid paging footer ("Page Size", "1 to 50 of 236", "Page 1 of 5") is
// translated: every localeText key AgGridComponent asks for exists in the
// fully maintained locales and both RTL locales, and none is an English copy.
// Other locales fall back to English (fallbackLocale: 'en').
import assert from 'node:assert/strict';
import fs from 'node:fs';

const root = new URL('../../', import.meta.url);
const component = fs.readFileSync(new URL('components/AgGridComponent.vue', root), 'utf8');
const list = component.match(/localeText:\s*Object\.fromEntries\(\s*\[([^\]]+)\]/);
assert.ok(list, 'AgGridComponent passes localeText built from a key list');
const keys = [...list[1].matchAll(/'([A-Za-z]+)'/g)].map(m => m[1]);
assert.ok(keys.includes('pageSizeSelectorLabel') && keys.includes('of') && keys.includes('to'), 'the pager keys are requested');
assert.match(component, /t\(`dataGrid\.\$\{key\}`\)/, 'each key is read from the dataGrid namespace');

const locale = code => JSON.parse(fs.readFileSync(new URL(`../locales/${code}.json`, root), 'utf8')).dataGrid || {};
const en = locale('en');
// Arabic too: the RTL pager layout (assets/css/rtl.css) would scramble English.
for (const code of ['en', 'he', 'es', 'ar']) {
  const strings = locale(code);
  for (const key of keys) assert.ok(typeof strings[key] === 'string' && strings[key].trim(), `${code}: dataGrid.${key}`);
  if (code !== 'en') assert.notDeepEqual(keys.map(k => strings[k]), keys.map(k => en[k]), `${code} is translated`);
}
// In RTL locales the translated footer is laid out right to left, or the
// Hebrew/Arabic words and the numbers come out in a scrambled order.
const rtlCss = fs.readFileSync(new URL('assets/css/rtl.css', root), 'utf8');
assert.match(rtlCss, /html\[dir="rtl"\] \.ag-paging-panel \{ direction: rtl; \}/, 'RTL locales lay the paging footer out right to left');
console.log(`PASS: ${keys.length} pager strings in en/he/es/ar; AgGridComponent reads them from dataGrid.*`);

// Every dashboards.* string of en.json exists in every locale with the same
// {placeholders}, and the count strings follow the plural convention.
//   cd frontend && node tests/unit/dashboardListLocales.mjs
//
// The plural convention: frontend/plugins/i18n.ts sets no pluralRules, so
// vue-i18n's default picks the form (two forms: n = 1 → the first, any other
// n → the second). That fits en, de, es, fr, it, pt, sv and he ("one | other").
// It breaks Russian and Arabic, so there a count string is one form that reads
// right for every n, a label before the number ("Новые: {n}"), with no " | ".
import assert from 'node:assert/strict';
import fs from 'node:fs';

const dir = new URL('../../../locales/', import.meta.url);
const locales = fs.readdirSync(dir).filter(f => f.endsWith('.json')).map(f => f.slice(0, -5)).sort();
const read = loc => JSON.parse(fs.readFileSync(new URL(`${loc}.json`, dir), 'utf8'));

const flatten = (node, prefix, out = {}) => {
  for (const [k, v] of Object.entries(node ?? {})) {
    const path = `${prefix}.${k}`;
    if (v && typeof v === 'object') flatten(v, path, out);
    else out[path] = v;
  }
  return out;
};
// vue-i18n 9 treats any bare "|" as a form separator, spaced or not.
const forms = value => value.split(/\s*\|\s*/);
const placeholders = value => [...new Set([...value.matchAll(/\{(\w+)\}/g)].map(m => m[1]))].sort().join(',');

const ONE_FORM = new Set(['ru', 'ar']);
const en = flatten(read('en').dashboards, 'dashboards');
const countKeys = Object.keys(en).filter(k => forms(en[k]).length > 1);

assert.ok(Object.keys(en).length > 100, 'en.json has the dashboards strings');
assert.ok(countKeys.includes('dashboards.count') && countKeys.includes('dashboards.tabNew'), 'the count keys come from en');
assert.equal(locales.length, 10, `10 locale files, got ${locales.join(', ')}`);

const failures = [];
for (const loc of locales.filter(l => l !== 'en')) {
  const strings = flatten(read(loc).dashboards, 'dashboards');
  for (const [key, source] of Object.entries(en)) {
    const value = strings[key];
    if (typeof value !== 'string') { failures.push(`${loc}: missing ${key}`); continue; }
    if (!value.trim() || forms(value).some(f => !f.trim())) { failures.push(`${loc}: empty ${key}`); continue; }
    if (placeholders(value) !== placeholders(source)) {
      failures.push(`${loc}: ${key} has {${placeholders(value)}}, en has {${placeholders(source)}}`);
    }
    if (countKeys.includes(key)) {
      const n = forms(value).length;
      if (ONE_FORM.has(loc) && value.includes('|')) failures.push(`${loc}: count key ${key} must be one form without "|"`);
      if (!ONE_FORM.has(loc) && n !== 2) failures.push(`${loc}: count key ${key} must have two forms "one | other", has ${n}`);
      // The "other" form is what every n except 1 shows (0 included): it must
      // carry en's placeholders, or the number disappears. The "one" form may
      // spell the number out (he "דשבורד אחד").
      const last = arr => arr[arr.length - 1];
      if (placeholders(last(forms(value))) !== placeholders(last(forms(source)))) {
        failures.push(`${loc}: ${key} last form has {${placeholders(last(forms(value)))}}, en has {${placeholders(last(forms(source)))}}`);
      }
    }
  }
}

if (failures.length) {
  const shown = failures.slice(0, 25).join('\n  ');
  assert.fail(`${failures.length} problem(s):\n  ${shown}${failures.length > 25 ? '\n  …' : ''}`);
}
console.log(`dashboardListLocales: ok (${Object.keys(en).length} keys, ${countKeys.length} count keys, ${locales.length - 1} locales)`);

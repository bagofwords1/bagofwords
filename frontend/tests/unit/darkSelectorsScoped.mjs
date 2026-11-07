// No component's scoped CSS may compile to a bare `.dark { … }` rule.
// `:global(.dark) .x :deep(y)` looks like a dark-mode override, but Vue keeps
// only the :global() part: the rule styles <html class="dark"> itself, and the
// intended override never applies (dark-mode docs showed near-black headings on
// a near-black page). Compiles every SFC style block the way Vite does.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {parse, compileStyle} from '@vue/compiler-sfc';

const root = new URL('../../', import.meta.url).pathname;
const files = [];
const walk = dir => {
  for (const e of fs.readdirSync(dir, {withFileTypes: true})) {
    if (e.name === 'node_modules' || e.name.startsWith('.')) continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p);
    else if (e.name.endsWith('.vue')) files.push(p);
  }
};
for (const d of ['components', 'pages', 'layouts']) walk(path.join(root, d));

const offenders = [];
let scopedBlocks = 0;
for (const file of files) {
  const {descriptor} = parse(fs.readFileSync(file, 'utf8'), {filename: file});
  descriptor.styles.forEach((style, i) => {
    if (!style.scoped || (style.lang && style.lang !== 'css')) return;
    scopedBlocks++;
    const {code} = compileStyle({source: style.content, id: 'data-v-test', scoped: true, filename: file});
    // A selector list (before `{`) whose every member is just `.dark`, at the
    // top level or nested in an at-rule such as `@media screen { … }`.
    for (const m of code.matchAll(/(^|[{}])\s*([^{}@;]+)\{/g)) {
      const selectors = m[2].split(',').map(s => s.trim()).filter(Boolean);
      if (selectors.length && selectors.every(s => s === '.dark')) offenders.push(`${path.relative(root, file)} (style ${i})`);
    }
  });
}
assert.ok(scopedBlocks > 50, `scanned ${scopedBlocks} scoped style blocks — the walk should find the app's components`);
assert.deepEqual([...new Set(offenders)], [], 'scoped dark overrides that compile to a bare `.dark` rule');
console.log(`PASS: ${scopedBlocks} scoped style blocks in ${files.length} SFCs; none compiles to a bare .dark rule`);

#!/usr/bin/env node
/*
 * RTL codemod: convert Tailwind directional utilities to their logical-property
 * equivalents so RTL (Hebrew) mirrors correctly.
 *
 *   ml-*  → ms-*      mr-*  → me-*        (margin)
 *   pl-*  → ps-*      pr-*  → pe-*        (padding)
 *   left-*→ start-*   right-*→ end-*      (positional inset)
 *  -left-*→ -start-*  -right-*→ -end-*    (negative inset)
 *   border-l[-*] → border-s[-*]   border-r[-*] → border-e[-*]
 *   rounded-l[-*]→ rounded-s[-*]  rounded-r[-*]→ rounded-e[-*]
 *   rounded-tl[-*]→rounded-ss[-*] rounded-tr[-*]→rounded-se[-*]
 *   rounded-bl[-*]→rounded-es[-*] rounded-br[-*]→rounded-ee[-*]
 *   text-left → text-start        text-right → text-end
 *
 * Preserves breakpoint / state prefixes (e.g. `md:ml-4`, `hover:-mr-2`).
 * space-x-* and divide-x-* are NOT rewritten — handled via CSS rule instead.
 *
 * Usage: node scripts/rtl-codemod.js [--dry]
 */
const fs = require('fs');
const path = require('path');

const ROOT = '/home/user/bagofwords/frontend';
const DRY = process.argv.includes('--dry');

const EXTENSIONS = ['.vue'];
const EXCLUDE_DIRS = new Set(['node_modules', '.nuxt', '.output', 'dist', 'public', 'tests']);
const EXCLUDE_FILES = new Set([
  'pages/i18n-smoke.vue',
]);

// Order matters: longest prefix first so `rounded-tl-*` is matched before `rounded-l-*`.
const REPLACEMENTS = [
  // rounded corners (two-letter first)
  [/([\w:-])?\brounded-tl\b/g, '$1rounded-ss'],
  [/([\w:-])?\brounded-tr\b/g, '$1rounded-se'],
  [/([\w:-])?\brounded-bl\b/g, '$1rounded-es'],
  [/([\w:-])?\brounded-br\b/g, '$1rounded-ee'],
  // rounded single side
  [/([\w:-])?\brounded-l\b/g, '$1rounded-s'],
  [/([\w:-])?\brounded-r\b/g, '$1rounded-e'],
  // border side
  [/([\w:-])?\bborder-l\b/g, '$1border-s'],
  [/([\w:-])?\bborder-r\b/g, '$1border-e'],
  // margin / padding (positive and negative, preserves breakpoint prefix like `md:`)
  [/\bml-/g, 'ms-'],
  [/\bmr-/g, 'me-'],
  [/\bpl-/g, 'ps-'],
  [/\bpr-/g, 'pe-'],
  [/-ml-/g, '-ms-'],
  [/-mr-/g, '-me-'],
  [/-pl-/g, '-ps-'],
  [/-pr-/g, '-pe-'],
  // text alignment
  [/\btext-left\b/g, 'text-start'],
  [/\btext-right\b/g, 'text-end'],
  // positional inset (must avoid matching `right-arrow` etc. — Tailwind values are digit/half/full/px/auto)
  [/\bleft-(\d+(?:\.\d+)?|px|auto|full|1\/2|1\/3|2\/3|1\/4|3\/4|1\/5|2\/5|3\/5|4\/5)\b/g, 'start-$1'],
  [/\bright-(\d+(?:\.\d+)?|px|auto|full|1\/2|1\/3|2\/3|1\/4|3\/4|1\/5|2\/5|3\/5|4\/5)\b/g, 'end-$1'],
  [/-left-(\d+(?:\.\d+)?|px|full|1\/2|1\/3|2\/3|1\/4|3\/4)\b/g, '-start-$1'],
  [/-right-(\d+(?:\.\d+)?|px|full|1\/2|1\/3|2\/3|1\/4|3\/4)\b/g, '-end-$1'],
];

function walk(dir, out = []) {
  for (const name of fs.readdirSync(dir)) {
    if (EXCLUDE_DIRS.has(name)) continue;
    const full = path.join(dir, name);
    const stat = fs.statSync(full);
    if (stat.isDirectory()) walk(full, out);
    else if (EXTENSIONS.includes(path.extname(name))) {
      const rel = path.relative(ROOT, full);
      if (!EXCLUDE_FILES.has(rel)) out.push(full);
    }
  }
  return out;
}

function transform(src) {
  // Restrict replacements to occurrences inside class="...", :class="...", and
  // "class:..." literal strings. This is approximate but prevents touching
  // unrelated identifiers. We match quoted attribute values and operate there.
  let result = src;
  // Pattern: `class="..."` OR `class='...'` OR `:class="..."` OR `:class='...'`
  //          OR inside arrays of class strings within :class.
  // Simplest safe approach: operate within class-looking attribute values.
  const attrRe = /(\s(?:|:))class\s*=\s*(['"])([\s\S]*?)\2/g;
  result = result.replace(attrRe, (m, pfx, q, body) => {
    let out = body;
    for (const [re, rep] of REPLACEMENTS) out = out.replace(re, rep);
    return `${pfx}class=${q}${out}${q}`;
  });
  // Also rewrite class bindings assigned to variables/arrays by scanning lines
  // with `class:` mapping (e.g. inside :class="{ 'ml-2': ... }"). We already
  // caught the attribute above. Now handle computed class strings in <script>
  // via a more conservative pass on raw literal strings containing "ml-"/"mr-"
  // etc. — stop after transforming single-quoted string literals on lines that
  // also mention "class" somewhere.
  // (Deliberately skipped to keep the codemod minimal and safe.)
  return result;
}

let changed = 0;
let scanned = 0;
const files = walk(ROOT);
for (const f of files) {
  const src = fs.readFileSync(f, 'utf8');
  const out = transform(src);
  scanned++;
  if (out !== src) {
    changed++;
    if (!DRY) fs.writeFileSync(f, out);
    console.log((DRY ? 'would rewrite: ' : 'rewrote: ') + path.relative(ROOT, f));
  }
}
console.log(`${changed} of ${scanned} files ${DRY ? 'would be' : 'were'} rewritten.`);

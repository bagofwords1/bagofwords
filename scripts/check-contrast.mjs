#!/usr/bin/env node
/*
 * Verifies the WCAG contrast ratios asserted in frontend/assets/css/tokens.css.
 *
 * The token comments quote specific ratios as the justification for each
 * hex value. Comments rot, so this parses the real variables out of the
 * stylesheet and recomputes every pairing that matters.
 *
 *   node scripts/check-contrast.mjs
 *
 * Exits non-zero if any pairing marked `aa` falls below 4.5:1, or any
 * marked `aaLarge` falls below 3:1. Pairings marked `decorative` are
 * reported but never fail the run.
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const css = readFileSync(join(root, 'frontend/assets/css/tokens.css'), 'utf8')

/* --- parse ------------------------------------------------------------- */
// tokens.css has exactly two blocks that define color triplets: `:root`
// (light) and `.dark`. Split on the `.dark {` boundary rather than trying to
// brace-match, which is enough for a file we control.
const darkAt = css.indexOf('.dark {')
if (darkAt === -1) throw new Error('could not find .dark block in tokens.css')

function triplets(block) {
  const out = {}
  // --name-rgb: 17 24 39;  ->  { name: [17, 24, 39] }
  for (const m of block.matchAll(/--bow-([a-z0-9-]+)-rgb:\s*(\d+)\s+(\d+)\s+(\d+)\s*;/g)) {
    out[m[1]] = [Number(m[2]), Number(m[3]), Number(m[4])]
  }
  return out
}

const light = triplets(css.slice(0, darkAt))
const dark = triplets(css.slice(darkAt))

/* --- WCAG 2.1 relative luminance + contrast ---------------------------- */
const channel = (c) => {
  const s = c / 255
  return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4)
}
const luminance = ([r, g, b]) =>
  0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
const contrast = (a, b) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}
const hex = ([r, g, b]) =>
  '#' + [r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('')

/* --- the pairings we claim --------------------------------------------- */
// `on` is the surface the foreground is expected to sit on. `level`:
//   aa         -> must be >= 4.5 (body text, labels, meaningful content)
//   aaLarge    -> must be >= 3.0 (>=18.66px bold / >=24px, and UI borders)
//   decorative -> reported only (placeholders duplicating a visible label,
//                 disabled controls, hairline rules)
const PAIRS = [
  { fg: 'ink', on: 'bg', level: 'aa', note: 'headings / primary text' },
  { fg: 'ink-2', on: 'bg', level: 'aa', note: 'body' },
  { fg: 'ink-muted', on: 'bg', level: 'aa', note: 'labels, timestamps, help' },
  { fg: 'ink-subtle', on: 'bg', level: 'decorative', note: 'placeholders, disabled' },
  { fg: 'accent', on: 'bg', level: 'aa', note: 'links, primary fill under small type' },
  { fg: 'accent-hover', on: 'bg', level: 'aa', note: 'link hover' },
  // Muted text also appears on the two recessed surfaces, which is the
  // pairing most likely to silently fail after a surface tweak.
  { fg: 'ink-muted', on: 'bg-subtle', level: 'aa', note: 'muted on page well' },
  { fg: 'ink-muted', on: 'bg-muted', level: 'aa', note: 'muted on hover fill' },
  { fg: 'ink', on: 'bg-elevated', level: 'aa', note: 'text on cards' },
  { fg: 'ink-inverted', on: 'bg-inverted', level: 'aa', note: 'tooltip / toast text' },
  { fg: 'line-strong', on: 'bg', level: 'decorative', note: 'input border' },
  { fg: 'line', on: 'bg', level: 'decorative', note: 'divider' },
]

const MIN = { aa: 4.5, aaLarge: 3.0, decorative: 0 }

let failed = 0
for (const [themeName, tokens] of [
  ['light', light],
  ['dark', dark],
]) {
  console.log(`\n  ${themeName}`)
  console.log('  ' + '-'.repeat(74))
  for (const { fg, on, level, note } of PAIRS) {
    const f = tokens[fg]
    const b = tokens[on]
    if (!f || !b) {
      console.log(`  MISSING  ${fg} on ${on} — token not defined in ${themeName}`)
      failed++
      continue
    }
    const ratio = contrast(f, b)
    const min = MIN[level]
    const ok = ratio >= min
    if (!ok) failed++
    const mark = level === 'decorative' ? '    ' : ok ? ' ok ' : 'FAIL'
    console.log(
      `  ${mark} ${ratio.toFixed(2).padStart(6)}:1  ` +
        `${(fg + ' on ' + on).padEnd(30)} ${hex(f)} / ${hex(b)}  ${note}` +
        (level === 'decorative' ? '  [decorative]' : ` [>=${min}]`)
    )
  }
}

console.log()
if (failed) {
  console.error(`  ${failed} contrast assertion(s) failed.\n`)
  process.exit(1)
}
console.log('  all contrast assertions hold.\n')

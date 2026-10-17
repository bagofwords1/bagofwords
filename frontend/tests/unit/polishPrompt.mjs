import assert from 'node:assert/strict'

import { polishPromptPosition, buildPolishPrompt, parsePolishPrompt, describePolishTarget } from '../../utils/polishPrompt.ts'

// The Polish prompt box sits next to the element picked inside the dashboard
// iframe, inside the pane that iframe fills. Pane 800x600, box 320x120, 8px gap.
const pane = { width: 800, height: 600 }
const box = { width: 320, height: 120 }

assert.deepEqual(
  polishPromptPosition({ top: 100, left: 50, height: 40 }, box, pane),
  { top: 148, left: 50 },
  'room below: box goes just under the element',
)

// Element in the bottom ~150px: below would overflow the pane, above fits.
assert.deepEqual(
  polishPromptPosition({ top: 500, left: 50, height: 40 }, box, pane),
  { top: 372, left: 50 },
  'no room below: box flips above the element',
)

// An element taller than the pane leaves room neither below nor above — keep the
// box inside the pane's bottom edge rather than below it.
assert.deepEqual(
  polishPromptPosition({ top: 50, left: 50, height: 540 }, box, pane),
  { top: 472, left: 50 },
  'no room either side: box clamps to the pane bottom',
)

assert.deepEqual(
  polishPromptPosition({ top: 100, left: 700, height: 40 }, box, pane),
  { top: 148, left: 472 },
  'element near the right edge: box clamps to the pane right',
)

assert.deepEqual(
  polishPromptPosition({ top: 100, left: -30, height: 40 }, box, pane),
  { top: 148, left: 8 },
  'element scrolled off the left: box clamps to the pane left',
)

// The box grows as the instruction gets longer; the same element flips once the
// taller box no longer fits below it.
assert.equal(polishPromptPosition({ top: 380, left: 50, height: 40 }, { width: 320, height: 120 }, pane).top, 428)
assert.equal(polishPromptPosition({ top: 380, left: 50, height: 40 }, { width: 320, height: 200 }, pane).top, 172)

assert.deepEqual(
  polishPromptPosition({ top: 100, left: 50, height: 40 }, box, { width: 200, height: 100 }),
  { top: 8, left: 8 },
  'pane smaller than the box: pin to the top-left corner',
)

// --- Polish chat prompt: build → parse round trip, and the element label ---
const req = {
  title: 'Sales "Q3" overview',
  artifactId: 'a1b2-c3',
  htmlSnippet: '<h2 class="text-xl">Revenue &amp; margin\n  <span>by month</span></h2>',
  instruction: 'Make this bigger\nand bold',
}
assert.deepEqual(parsePolishPrompt(buildPolishPrompt(req)), req, 'built prompt parses back')
assert.equal(parsePolishPrompt('Make the title bigger'), null, 'ordinary message is not a polish request')
assert.equal(parsePolishPrompt(''), null)
assert.equal(
  parsePolishPrompt(buildPolishPrompt({ ...req, instruction: '' })),
  null,
  'no instruction: not a polish request',
)

assert.deepEqual(describePolishTarget(req.htmlSnippet), { tag: 'h2', text: 'Revenue & margin by month' })
assert.deepEqual(describePolishTarget('<DIV><p>x</p><span cla'), { tag: 'div', text: 'x' }, 'cut-off snippet')
assert.equal(describePolishTarget(`<p>${'a'.repeat(100)}</p>`, 10).text, 'aaaaaaaaa…')

console.log('polishPrompt: ok')

import assert from 'node:assert/strict'

import { polishPromptPosition } from '../../utils/polishPrompt.ts'

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

// The chat prompt for a Polish pick.
{
  const { buildPolishPrompt } = await import('../../utils/polishPrompt.ts')
  const plain = buildPolishPrompt('Sales', 'a1', { htmlSnippet: '<td>x</td>' }, 'make it red')
  assert.equal(plain, 'Polish the dashboard "Sales" (artifact_id: a1).\nTarget element:\n```html\n<td>x</td>\n```\nInstruction: make it red', 'regular pick: unchanged wording')
  const info = buildPolishPrompt('Sales', 'a1', { htmlSnippet: '<div>card</div>', info: true }, 'make it red')
  assert.match(info, /Target: the ⓘ info icon of this element\./, 'ⓘ pick names the icon')
  assert.match(info, /infoClassName .* infoSize .* data-bow-info-class .* data-bow-info-size/, 'ⓘ pick names the styling knobs')
  assert.match(info, /cannot be removed/, 'ⓘ pick says it is not removable')
  assert.match(info, /```html\n<div>card<\/div>\n```\nInstruction: make it red$/, 'ⓘ pick carries the owner snippet')
}

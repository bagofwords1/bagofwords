import assert from 'node:assert/strict'

import { launcherClearance } from '../../utils/intercomLauncher.ts'

// The shared dashboard page (/r/{id}) stacks its badge and chat bubble above
// Intercom's launcher when one is on screen. launcherClearance is how much room
// that launcher takes up from the viewport bottom — 0 means "no launcher", and
// the page then keeps its usual corner layout.

assert.equal(launcherClearance([], 800), 0, 'no launcher, no room taken')

// Intercom's default: a 48px launcher 20px above the viewport bottom.
assert.equal(launcherClearance([{ top: 732, height: 48 }], 800), 68, 'room up to the launcher top')

// hide_default_launcher (mobile, Excel) leaves the element behind at 0 height —
// that must not push the page's own corner elements around.
assert.equal(launcherClearance([{ top: 0, height: 0 }], 800), 0, 'a hidden launcher takes no room')

// Both shapes can be in the DOM during the swap from the lightweight
// placeholder to the iframe; clear the taller of the two.
assert.equal(
  launcherClearance([{ top: 740, height: 40 }, { top: 720, height: 60 }], 800),
  80,
  'the taller launcher wins',
)

assert.equal(launcherClearance([{ top: 900, height: 48 }], 800), 0, 'a launcher below the fold takes no room')

import assert from 'node:assert/strict'

import { exportsHidden } from '../../utils/dashboardExportsHidden.ts'

// The export button shows whenever the server would hand this reader a file,
// and the file holds what they see (found in manual testing: one chart they
// may not read hid the button for the whole dashboard).

// /d, a personal dashboard (exports_own_rows): one chart refused, one ran.
assert.equal(exportsHidden({ source: 'dashboard', mode: 'page', snapshotWithheld: true,
  exportsOwnRows: true, hasOwnSuccess: true }), false, 'a partly refused reader still downloads what they see')
// ... no successful run of their own yet: the server would refuse.
assert.equal(exportsHidden({ source: 'dashboard', mode: 'page', snapshotWithheld: true,
  exportsOwnRows: true, hasOwnSuccess: false }), true)
// A personal deck is drawn once, from the owner's rows.
assert.equal(exportsHidden({ source: 'dashboard', mode: 'slides', snapshotWithheld: false,
  exportsOwnRows: true, hasOwnSuccess: true }), true)
// A shared dashboard with one identity's chart: the export holds the rest.
assert.equal(exportsHidden({ source: 'dashboard', mode: 'page', snapshotWithheld: true,
  exportsOwnRows: false, hasOwnSuccess: false }), false)
// The owner, or nothing withheld.
assert.equal(exportsHidden({ source: 'dashboard', mode: 'page', snapshotWithheld: false,
  exportsOwnRows: false, hasOwnSuccess: false }), false)

// /r keeps its rule: any withheld snapshot hides the exports (its server refuses).
assert.equal(exportsHidden({ source: 'report', mode: 'page', snapshotWithheld: true,
  exportsOwnRows: false, hasOwnSuccess: true }), true)
assert.equal(exportsHidden({ source: 'report', mode: 'page', snapshotWithheld: false,
  exportsOwnRows: false, hasOwnSuccess: false }), false)

console.log('the export button shows whenever the server would hand the reader a file')

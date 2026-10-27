import assert from 'node:assert/strict'

import { viewingPath } from '../../utils/dashboardViewing.ts'

// "You're viewing v3, the live version": the tab shows the published page,
// which still has v3 after the conversation v3 was made in is deleted (found
// in manual testing: the draft of v3 was gone, and the tab fell back to
// another person's newest draft under a bar that still said v3).
assert.equal(viewingPath('dash-1', { version: 3, live: true }), '/d/dash-1?embed=1')
assert.equal(viewingPath('dash-1', { version: 3, live: true }, { embed: false }), '/d/dash-1',
  'Open in new tab: the full published page')

// Another conversation's version that is not live: its draft, by number.
assert.equal(viewingPath('dash-1', { version: 4 }), '/d/dash-1/draft?version=4&embed=1')
assert.equal(viewingPath('dash-1', { version: 4, live: false }, { embed: false }), '/d/dash-1/draft?version=4')

// Ids are path segments.
assert.equal(viewingPath('a/b', { version: 1, live: true }), '/d/a%2Fb?embed=1')

console.log('dashboardViewing: ok')

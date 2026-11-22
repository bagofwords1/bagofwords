import assert from 'node:assert/strict'

import { newerDraftVersion, newerPaneVersion } from '../../utils/newerDashboardVersion.ts'

// The bug this pins: a dashboard open outside the run that edits it (its draft
// page in another tab, a conversation pane that missed the stream) never
// learned of the version the agent just saved -- reopening the link was the
// only way to see it in the version picker.

// --- the draft page --------------------------------------------------------

const v7 = { version_id: 'v7', version_number: 7 }
const v8 = { version_id: 'v8', version_number: 8 }

assert.deepEqual(newerDraftVersion(v7, v8), { id: 'v8', number: 8, elsewhere: false },
  'a newer latest is offered')
assert.equal(newerDraftVersion(v8, v8), null, 'the version on screen is not news')
assert.equal(newerDraftVersion(v8, v7), null, 'an older latest (a roll back elsewhere) is not offered as newer')
assert.equal(newerDraftVersion({ version_id: null }, v8), null, 'nothing on screen yet: nothing to compare')
assert.equal(newerDraftVersion(v7, null), null, 'a failed read offers nothing')

// --- the conversation pane -------------------------------------------------

const base = {
  selected: { id: 'a3', version: 3 },
  newestHere: { id: 'a3', version: 3 },
  knownHere: new Set(['a1', 'a2', 'a3']),
  elsewhere: [{ id: 'a3', number: 3, report_id: 'r1' }],
  knownElsewhere: new Set(['a3']),
  reportId: 'r1',
}

assert.equal(newerPaneVersion(base), null, 'nothing new: no offer')

assert.deepEqual(
  newerPaneVersion({ ...base, newestHere: { id: 'a4', version: 4 } }),
  { id: 'a4', number: 4, elsewhere: false },
  'a version saved in this conversation the pane never heard about is offered',
)

assert.equal(
  newerPaneVersion({ ...base, newestHere: { id: 'a4', version: 4 }, knownHere: new Set(['a1', 'a2', 'a3', 'a4']) }),
  null,
  'a version the pane already listed (the reader picked an older one on purpose) is not re-offered',
)

assert.equal(
  newerPaneVersion({ ...base, selected: { id: 'a4', version: 4 }, newestHere: { id: 'a4', version: 4 } }),
  null,
  'the pane already shows the newest (its own run moved it there)',
)

const fromB = { id: 'b5', number: 5, report_id: 'r2' }
assert.deepEqual(
  newerPaneVersion({ ...base, elsewhere: [...base.elsewhere, fromB] }),
  { id: 'b5', number: 5, elsewhere: true },
  'a newer version saved in another conversation is offered, to view read-only',
)

assert.equal(
  newerPaneVersion({ ...base, elsewhere: [...base.elsewhere, fromB], knownElsewhere: new Set() }),
  null,
  'the first history read is a baseline, not news',
)

assert.equal(
  newerPaneVersion({ ...base, elsewhere: [...base.elsewhere, { id: 'a4', number: 4, report_id: 'r1' }] }),
  null,
  "this conversation's own versions come from its list, not the history",
)

assert.equal(newerPaneVersion({ ...base, selected: null }), null, 'nothing selected: nothing to offer')

// A stream or another list read can populate the picker before the poll.
const alreadyListed = { ...base, followLatest: true, newestHere: { id: 'v8', version: 8 }, knownHere: new Set(['v8']) }
assert.equal(newerPaneVersion(alreadyListed)?.id, 'v8')
assert.equal(newerPaneVersion({ ...alreadyListed, selected: alreadyListed.newestHere }), null)
assert.equal(newerPaneVersion({ ...base, followLatest: true, elsewhere: [fromB], knownElsewhere: new Set([fromB.id]) })?.id, fromB.id)

console.log('newerDashboardVersion: ok')

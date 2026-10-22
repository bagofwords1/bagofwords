import assert from 'node:assert/strict'

import { deleteNotes } from '../../utils/deleteNotes.ts'

// What a conversation's delete confirmation says about its dashboards
// (delete_preview outcomes): the never-published ones go with it; a
// published one stays, but changes not yet published in it are lost.
const notes = deleteNotes([
  { title: 'Sales', outcome: 'survives' },
  { title: 'Draft deck', outcome: 'deleted' },
  { title: 'Ops', outcome: 'loses_changes' },
  { title: '', outcome: 'deleted' },
])
assert.deepEqual(notes.deleted, ['Draft deck', 'Untitled'])
assert.deepEqual(notes.losesChanges, ['Ops'])

// Nothing to say: both lines stay hidden.
assert.deepEqual(deleteNotes([{ title: 'Sales', outcome: 'survives' }]), { deleted: [], losesChanges: [] })
assert.deepEqual(deleteNotes([]), { deleted: [], losesChanges: [] })
// Two dashboards with the same name are two dashboards: said with a count.
assert.deepEqual(deleteNotes([{ id: 'a', title: 'Ops', outcome: 'loses_changes' },
  { id: 'b', title: 'Ops', outcome: 'loses_changes' }]).losesChanges, ['Ops (2)'])
// The same dashboard reported twice (two previews) is one.
assert.deepEqual(deleteNotes([{ id: 'a', title: 'Ops', outcome: 'deleted' },
  { id: 'a', title: 'Ops', outcome: 'deleted' }]).deleted, ['Ops'])

console.log('deleteNotes: ok')

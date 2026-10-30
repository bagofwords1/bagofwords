import assert from 'node:assert/strict'

import * as viewerInput from '../../utils/viewerInput.ts'

// Run: node --test tests/unit/viewerInput.mjs
// A published chart that waits for the viewer's input (its query needs a value
// only the app gives) carries viewer_result.needs_input. The page shows the app
// for it instead of the withheld-data gate, but that wait is not a run of the
// reader's own: it never counts as an own success (the export rule).

const { awaitsViewerInput, isOwnSuccess } = viewerInput
assert.equal(typeof awaitsViewerInput, 'function', 'awaitsViewerInput is exported')
assert.equal(awaitsViewerInput({ needs_input: ['customer_id'] }), true)
for (const vr of [undefined, null, {}, { needs_input: [] }, { needs_input: 'customer_id' }, { status: 'success' }]) {
  assert.equal(awaitsViewerInput(vr), false, JSON.stringify(vr))
}
console.log('PASS: only a non-empty needs_input list is a wait for the viewer')

assert.equal(isOwnSuccess({ status: 'success' }), true)
assert.equal(isOwnSuccess({ status: 'success', needs_input: [] }), true)
assert.equal(isOwnSuccess({ status: 'success', needs_input: ['customer_id'] }), false, 'a wait is not a run of theirs')
for (const vr of [{ status: 'error' }, undefined, null]) {
  assert.equal(isOwnSuccess(vr), false, JSON.stringify(vr))
}
console.log('PASS: a wait for input never counts as the reader\'s own successful run')

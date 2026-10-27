import assert from 'node:assert/strict'

import { isReviewConflict, sameSelection } from '../../utils/reviewRetry.ts'

// --- isReviewConflict: only a 409, in any of the shapes useMyFetch surfaces ---
assert.ok(isReviewConflict({ statusCode: 409 }))
assert.ok(isReviewConflict({ status: 409 }))
assert.ok(isReviewConflict({ response: { status: 409 } }))
for (const other of [{ statusCode: 403 }, { statusCode: 404 }, { statusCode: 500 }, {}, null, undefined, 'x']) {
  assert.equal(isReviewConflict(other), false, `not a conflict: ${JSON.stringify(other)}`)
}

// --- sameSelection: the retry is safe only on the identical set of units ---
const u = (b, k) => ({ build_id: b, key: k })
assert.ok(sameSelection([u('b1', 'h1'), u('b1', 'h2')], [u('b1', 'h2'), u('b1', 'h1')]), 'order does not matter')
assert.ok(sameSelection([u('b1', 'field:status:draft')], [u('b1', 'field:status:draft')]))

assert.equal(sameSelection([u('b1', 'h1')], []), false, 'a unit vanished after reload')
assert.equal(sameSelection([u('b1', 'h1')], [u('b1', 'h1'), u('b1', 'h2')]), false, 'a unit appeared after reload')
assert.equal(sameSelection([u('b1', 'h1')], [u('b2', 'h1')]), false, 'same key from another suggestion is a different decision')
assert.equal(sameSelection([u('b1', 'h1')], [u('b1', 'h1x')]), false, 'rebased hunk got a new key')
assert.equal(sameSelection([], []), false, 'nothing to act on is never resent')
assert.equal(sameSelection([u('b1', 'h1'), u('b1', 'h1')], [u('b1', 'h1'), u('b1', 'h2')]), false, 'duplicates never match by count alone')

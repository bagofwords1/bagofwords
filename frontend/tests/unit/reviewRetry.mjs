import assert from 'node:assert/strict'

import { isReviewConflict, sameSelection, selectionStillValid, REVIEW_STALE_MAIN_CODE } from '../../utils/reviewRetry.ts'

// --- isReviewConflict: a 409 AND the stale-main code, in the shapes useMyFetch
// surfaces. A 409 for an invalid selection (resource.conflict) is NOT retryable:
// a reload finds the same bad selection and would resend a guaranteed 409. ---
const stale = REVIEW_STALE_MAIN_CODE
assert.ok(isReviewConflict({ statusCode: 409, data: { error_code: stale } }))
assert.ok(isReviewConflict({ status: 409, data: { error_code: stale } }))
assert.ok(isReviewConflict({ response: { status: 409, _data: { error_code: stale } } }))

assert.equal(isReviewConflict({ statusCode: 409, data: { error_code: 'resource.conflict' } }), false, 'invalid-selection 409 is not retried')
assert.equal(isReviewConflict({ statusCode: 409 }), false, 'a 409 with no code is not retried')
for (const other of [
  { statusCode: 403, data: { error_code: stale } }, // right code, wrong status
  { statusCode: 404 }, { statusCode: 500 }, {}, null, undefined, 'x',
]) {
  assert.equal(isReviewConflict(other), false, `not a retryable conflict: ${JSON.stringify(other)}`)
}

const u = (b, k) => ({ build_id: b, key: k })

// --- sameSelection: "Accept/Reject all" resends only on the identical set ---
assert.ok(sameSelection([u('b1', 'h1'), u('b1', 'h2')], [u('b1', 'h2'), u('b1', 'h1')]), 'order does not matter')
assert.ok(sameSelection([u('b1', 'field:status:draft')], [u('b1', 'field:status:draft')]))

assert.equal(sameSelection([u('b1', 'h1')], []), false, 'a unit vanished after reload')
assert.equal(sameSelection([u('b1', 'h1')], [u('b1', 'h1'), u('b1', 'h2')]), false, 'a unit appeared after reload')
assert.equal(sameSelection([u('b1', 'h1')], [u('b2', 'h1')]), false, 'same key from another suggestion is a different decision')
assert.equal(sameSelection([u('b1', 'h1')], [u('b1', 'h1x')]), false, 'rebased hunk got a new key')
assert.equal(sameSelection([], []), false, 'nothing to act on is never resent')
assert.equal(sameSelection([u('b1', 'h1'), u('b1', 'h1')], [u('b1', 'h1'), u('b1', 'h2')]), false, 'duplicates never match by count alone')

// --- selectionStillValid: a single hunk accept/reject resends when THAT unit is
// still present, even in a pane that shows others (the hover-card Accept) ---
assert.ok(selectionStillValid([u('b1', 'h1')], [u('b1', 'h1')]))
assert.ok(selectionStillValid([u('b1', 'h1')], [u('b1', 'h1'), u('b1', 'h2'), u('b2', 'field:status:draft')]),
  'other units on screen do not change what the one click meant')
assert.equal(selectionStillValid([u('b1', 'h1')], [u('b1', 'h2')]), false, 'the clicked unit vanished')
assert.equal(selectionStillValid([u('b1', 'h1')], [u('b2', 'h1')]), false, 'same key, other build is a different unit')
assert.equal(selectionStillValid([u('b1', 'h1')], [u('b1', 'h1x')]), false, 'rebased hunk got a new key')
assert.equal(selectionStillValid([u('b1', 'h1')], []), false, 'nothing on screen is never resent')
assert.equal(selectionStillValid([], [u('b1', 'h1')]), false, 'an empty selection never retries')

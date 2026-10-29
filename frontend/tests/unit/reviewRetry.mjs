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

// A unit as the pane renders it: (build, key) PLUS the text shown. before/after
// matter because a hunk key is stable as main drifts (text_hunks.Hunk.key),
// while rebased_hunks_against_main re-anchors `before` to main's current text —
// so the same key can describe different on-screen text after a reload.
const u = (b, k, before = 'X', after = 'Y') => ({ build_id: b, key: k, before, after })

// --- sameSelection: "Accept/Reject all" resends only on the identical set ---
assert.ok(sameSelection([u('b1', 'h1'), u('b1', 'h2')], [u('b1', 'h2'), u('b1', 'h1')]), 'order does not matter')
assert.ok(sameSelection([u('b1', 'field:status:draft')], [u('b1', 'field:status:draft')]))

assert.equal(sameSelection([u('b1', 'h1')], []), false, 'a unit vanished after reload')
assert.equal(sameSelection([u('b1', 'h1')], [u('b1', 'h1'), u('b1', 'h2')]), false, 'a unit appeared after reload')
assert.equal(sameSelection([u('b1', 'h1')], [u('b2', 'h1')]), false, 'same key from another suggestion is a different decision')
assert.equal(sameSelection([], []), false, 'nothing to act on is never resent')
assert.equal(sameSelection([u('b1', 'h1'), u('b1', 'h1')], [u('b1', 'h1'), u('b1', 'h2')]), false, 'duplicates never match by count alone')
// The blocker: the key is STABLE across a rebase, but `before` now points at
// main's current text — different text the reviewer never saw. Content-aware
// equality must refuse it even though the (build, key) set is identical.
assert.equal(
  sameSelection([u('b1', 'h1', 'net of tax', 'net of VAT')], [u('b1', 'h1', 'net of levy', 'net of VAT')]),
  false, 'same key but `before` drifted to main\'s current text is not the same decision')

// --- selectionStillValid: a single hunk accept/reject resends when THAT unit is
// still present WITH THE SAME TEXT, even in a pane that shows others ---
assert.ok(selectionStillValid([u('b1', 'h1')], [u('b1', 'h1')]))
assert.ok(selectionStillValid([u('b1', 'h1')], [u('b1', 'h1'), u('b1', 'h2'), u('b2', 'field:status:draft')]),
  'other units on screen do not change what the one click meant')
assert.equal(selectionStillValid([u('b1', 'h1')], [u('b1', 'h2')]), false, 'the clicked unit vanished')
assert.equal(selectionStillValid([u('b1', 'h1')], [u('b2', 'h1')]), false, 'same key, other build is a different unit')
assert.equal(selectionStillValid([u('b1', 'h1')], []), false, 'nothing on screen is never resent')
assert.equal(selectionStillValid([], [u('b1', 'h1')]), false, 'an empty selection never retries')
// The blocker, single-hunk path: same (build, key) back after the reload, but
// the hunk now rewrites DIFFERENT text (its `before` drifted). The retry must
// decline — applying it would route the proposed edit over text never shown.
assert.equal(
  selectionStillValid([u('b1', 'h1', 'net of tax', 'net of VAT')], [u('b1', 'h1', 'net of levy', 'net of VAT')]),
  false, 'same key, drifted `before` is not what was clicked')
// A changed `after` (the proposed side re-rendered) is likewise not a match.
assert.equal(
  selectionStillValid([u('b1', 'h1', 'a', 'b')], [u('b1', 'h1', 'a', 'c')]),
  false, 'same key, drifted `after` is not what was clicked')

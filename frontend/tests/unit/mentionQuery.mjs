import assert from 'node:assert/strict'

import { activeMentionQuery } from '../../utils/mentions.ts'

// The composer's @-menu filters on this string. It is measured from the '@' to
// the caret — never to the end of the field, which is what broke mentions typed
// mid-sentence.

// --- the caret at the end of the line (the case that always worked) ----------

assert.equal(activeMentionQuery('ask @mix', 4, 8), 'mix')
assert.equal(activeMentionQuery('@', 0, 1), '')

// --- the caret mid-line (the bug) --------------------------------------------
//
// "Can you load again your suggestions for @mixa| so I can review it" — the
// query is "mixa", not "mixa so I can review it", which matched nothing and
// left the menu stuck on "No results found".

const line = 'Can you load again your suggestions for @mixa so I can review it'
const at = line.indexOf('@')
assert.equal(activeMentionQuery(line, at, at + 5), 'mixa')

// Still bounded while the user keeps typing into the middle of the line.
assert.equal(activeMentionQuery(line, at, at + 1), '')
assert.equal(activeMentionQuery(line, at, at + 3), 'mi')

// --- degenerate caret positions ----------------------------------------------

// Caret behind the '@' (it was moved out of the token): no query at all,
// rather than the whole rest of the field.
assert.equal(activeMentionQuery(line, at, at), '')
assert.equal(activeMentionQuery(line, at, 0), '')

// No active token.
assert.equal(activeMentionQuery(line, -1, 10), '')

// A caret past the text (a stale measurement) clamps to the text length
// instead of producing undefined-ish slices.
assert.equal(activeMentionQuery('hi @ab', 3, 999), 'ab')

// Empty / missing text is a query of nothing, not a crash.
assert.equal(activeMentionQuery('', 0, 5), '')

console.log('mention query is bounded by the caret, mid-line included')

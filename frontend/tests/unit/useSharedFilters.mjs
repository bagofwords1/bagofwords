import assert from 'node:assert/strict'

import {
  evaluateOperator,
  evaluateCondition,
  evaluateFilters,
} from '../../composables/useSharedFilters.ts'

// --- whitespace normalization ------------------------------------------------

// SQL Server CHAR columns pad values with trailing spaces; the grid renders
// "X707 " and "X707" identically, and the user types what they see.
assert.equal(evaluateOperator('X707 ', 'equals', 'X707'), true, 'trailing-padded cell must equal the typed value')
assert.equal(evaluateOperator('X707', 'equals', ' X707 '), true, 'padded typed value must equal the clean cell')
assert.equal(evaluateOperator('X707 ', 'not_equals', 'X707'), false)
assert.equal(evaluateOperator(' X707', 'starts_with', 'X7'), true)
assert.equal(evaluateOperator('X707 ', 'ends_with', '07'), true)
assert.equal(evaluateOperator('X707 ', 'contains', ' 707 '), true)

// A padded-empty CHAR cell renders as blank, so it must count as empty.
assert.equal(evaluateOperator('   ', 'is_empty', undefined), true)
assert.equal(evaluateOperator('   ', 'is_not_empty', undefined), false)
assert.equal(evaluateOperator(null, 'is_empty', undefined), true)
assert.equal(evaluateOperator('x', 'is_not_empty', undefined), true)

console.log('padded values match what the grid displays')

// --- numeric equality --------------------------------------------------------

// Producing tools disagree on number formatting: pandas emits "7837.0" where
// the user types 7837. Both sides numeric -> compare numerically.
assert.equal(evaluateOperator('7837.0', 'equals', '7837'), true)
assert.equal(evaluateOperator(7837, 'equals', '7837'), true)
assert.equal(evaluateOperator('0100', 'equals', 100), true)
assert.equal(evaluateOperator('7837.5', 'equals', '7837'), false)
// Mixed numeric/text falls back to string comparison, not NaN weirdness.
assert.equal(evaluateOperator('X707', 'equals', 707), false)

// Comparisons trim too, and null is no longer coerced to 0.
assert.equal(evaluateOperator(' 42 ', 'greater_than', '41'), true)
assert.equal(evaluateOperator(null, 'less_than', 5), false, 'null must not compare as 0')
assert.equal(evaluateOperator('', 'gte', 0), false, 'blank must not compare as 0')
assert.equal(evaluateOperator(' 10 ', 'between', 5, 15), true)

console.log('numbers compare numerically regardless of formatting')

// --- booleans ----------------------------------------------------------------

// Python serializes booleans as "True"/"False".
assert.equal(evaluateOperator('True', 'is_true', undefined), true)
assert.equal(evaluateOperator('False', 'is_false', undefined), true)
assert.equal(evaluateOperator(true, 'is_true', undefined), true)
assert.equal(evaluateOperator(0, 'is_false', undefined), true)
assert.equal(evaluateOperator('False', 'is_true', undefined), false)

console.log('python-style booleans are recognized')

// --- in / not_in -------------------------------------------------------------

assert.equal(evaluateOperator('X707 ', 'in', ['X707', 'Y701']), true)
assert.equal(evaluateOperator('Z999', 'not_in', ['X707', 'Y701']), true)

console.log('membership operators normalize like equals')

// --- condition plumbing ------------------------------------------------------

const row = { proj: 'X707 ', amount: '7837.0' }

// Full condition path: viz targeting + case-insensitive column lookup.
assert.equal(
  evaluateCondition(row, { id: '1', column: 'viz1:Proj', operator: 'equals', value: 'X707' }, 'viz1'),
  true,
)
// Conditions for another visualization never filter this one.
assert.equal(
  evaluateCondition(row, { id: '1', column: 'viz2:proj', operator: 'equals', value: 'nope' }, 'viz1'),
  true,
)
// A half-written condition (blank value) is a no-op, not a match-nothing.
assert.equal(
  evaluateCondition(row, { id: '1', column: 'viz1:proj', operator: 'equals', value: '' }, 'viz1'),
  true,
)

// Groups: OR across groups, AND within a group.
const groups = [
  { id: 'g1', conditions: [
    { id: 'c1', column: 'viz1:proj', operator: 'equals', value: 'X707' },
    { id: 'c2', column: 'viz1:amount', operator: 'equals', value: '7837' },
  ] },
]
assert.equal(evaluateFilters(row, groups, 'viz1'), true)
assert.equal(evaluateFilters({ proj: 'Y701', amount: 1 }, groups, 'viz1'), false)

console.log('condition targeting and grouping still hold')

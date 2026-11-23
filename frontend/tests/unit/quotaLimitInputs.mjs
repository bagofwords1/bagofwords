import assert from 'node:assert/strict'

import { dailyExceedsMonthly, limitValue } from '../../utils/quotaLimitInputs.ts'

// The editor's own parsers, mirrored: spend rounds to cents, data is MB → bytes.
const parseUsd = (v) => (v === '' || v == null ? null : Math.round(Number(v) * 100) / 100)
const parseMb = (v) => (v === '' || v == null ? null : Math.floor(Number(v) * 1024 * 1024))

// A name-only edit re-sends every limit. Values the API stores more precisely
// than the inputs show must come back unchanged, not rounded to zero.
assert.equal(limitValue('0', { input: '0', raw: 0.004 }, parseUsd), 0.004, 'untouched $0.004 stays $0.004')
assert.equal(limitValue('0', { input: '0', raw: 1024 }, parseMb), 1024, 'untouched 1,024 B stays 1,024 B')
assert.equal(limitValue('', { input: '', raw: null }, parseMb), null, 'untouched empty stays unlimited')
assert.equal(limitValue(' 0 ', { input: '0', raw: 1024 }, parseMb), 1024, 'whitespace is not an edit')

// A field the admin changed is parsed from what they typed.
assert.equal(limitValue('2.5', { input: '0', raw: 0.004 }, parseUsd), 2.5)
assert.equal(limitValue('', { input: '1', raw: 1048576 }, parseMb), null, 'clearing a field removes the cap')
// New policy: no baseline, always parsed.
assert.equal(limitValue('1', undefined, parseMb), 1048576)

// Daily vs monthly: only same-scope caps are compared.
assert.equal(dailyExceedsMonthly('tokens', '600', '500'), true)
assert.equal(dailyExceedsMonthly('spend', '5.01', '5'), true)
assert.equal(dailyExceedsMonthly('tokens', '500', '500'), false, 'equal is fine')
assert.equal(dailyExceedsMonthly('tokens', '600', ''), false, 'daily only is fine')
// Monthly query/data caps are per connection; daily ones span all connections.
assert.equal(dailyExceedsMonthly('queries', '150', '100'), false)
assert.equal(dailyExceedsMonthly('data', '3', '2'), false)

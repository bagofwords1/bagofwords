import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  FIELDS, QueryError, addTerms, canonical, containsTerms, cursorContext, parse, removeTerms, stripPositions,
} from '../../utils/diagnosisQuery.ts'

// The golden fixture is shared with the Python parser
// (backend/tests/unit/test_diagnosis_grammar.py). Both must agree byte for byte.
const here = dirname(fileURLToPath(import.meta.url))
const fixture = JSON.parse(readFileSync(join(here, '../../../backend/tests/fixtures/diagnosis_queries.json'), 'utf8'))

let checked = 0
for (const c of fixture.cases) {
  if (c.error) {
    let err = null
    try { parse(c.q) } catch (e) { err = e }
    assert.ok(err instanceof QueryError, `expected an error for ${JSON.stringify(c.q)}`)
    assert.deepEqual(err.toDict(), c.error, `error mismatch for ${JSON.stringify(c.q)}`)
  } else {
    const r = parse(c.q)
    assert.deepEqual(stripPositions(r.ast), c.ast, `ast mismatch for ${JSON.stringify(c.q)}`)
    assert.equal(canonical(r.ast), c.canonical, `canonical mismatch for ${JSON.stringify(c.q)}`)
    assert.deepEqual(r.tokens, c.tokens, `tokens mismatch for ${JSON.stringify(c.q)}`)
  }
  checked++
}
assert.ok(checked > 40, 'fixture looks truncated')

// --- editing helpers ---------------------------------------------------------
assert.equal(addTerms('', 'status:error'), 'status:error')
assert.equal(addTerms('user:dana', 'status:error'), 'user:dana status:error')
assert.equal(addTerms('status:error OR user:dana', 'feedback:negative'), '(status:error OR user:dana) feedback:negative')
assert.equal(removeTerms('user:dana status:error tool:x', 'status:error'), 'user:dana tool:x')
assert.equal(removeTerms('user:dana AND status:error', 'status:error'), 'user:dana')
assert.equal(removeTerms('tool:create_data tool.status:error revenue', 'tool:create_data tool.status:error'), 'revenue')
assert.equal(removeTerms('status:error', 'status:error'), '')
assert.ok(containsTerms('revenue status:error tool:create_data', 'status:error'))
assert.ok(containsTerms('tool:create_data tool.status:error', 'tool:create_data tool.status:error'))
assert.ok(!containsTerms('tool:create_data', 'tool:create_data tool.status:error'))
assert.ok(!containsTerms('status:error OR user:dana', 'status:error'), 'an OR root is not a conjunction')
assert.ok(containsTerms('Status:ERROR', 'status:error'), 'chip state ignores spelling')

// --- cursor context ----------------------------------------------------------
assert.deepEqual(cursorContext('revenue dur', 11), { kind: 'field', prefix: 'dur', start: 8 })
assert.equal(cursorContext('status:', 7).kind, 'value')
assert.equal(cursorContext('status:', 7).field.name, 'status')
assert.deepEqual({ ...cursorContext('user:da', 7), field: undefined }, { kind: 'value', prefix: 'da', start: 5, op: 'eq', field: undefined })
assert.equal(cursorContext('cost:>0.', 8).op, '>')
assert.equal(cursorContext('status:(err', 11).op, 'any')
assert.equal(cursorContext('"gross mar', 10).kind, 'none')
assert.equal(cursorContext('status:error ', 13).kind, 'none')
assert.equal(cursorContext('nope:x', 6).kind, 'none')

// --- registry shape ----------------------------------------------------------
assert.ok(FIELDS.length > 30)
assert.ok(FIELDS.every(f => f.name && f.type && f.entity))

console.log(`diagnosisQuery: ${checked} golden cases + helpers ok`)

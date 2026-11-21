import assert from 'node:assert/strict'
import { cacheCandidateFrom, cacheTableNameFor } from '../../utils/cacheCandidate.ts'

// A result is offered for caching only when exactly one query ran, on a
// connection the record names, without error, and not through a cached table.
const timing = (over = {}) => ({ index: 0, rows: 3, connection_id: 'c1', data_source_id: 'd1', client_key: 'Sales:warehouse', ...over })
const result = (...timings) => ({ query_timings: timings })

assert.deepEqual(cacheCandidateFrom(result(timing())), { connectionId: 'c1', dataSourceId: 'd1', clientKey: 'Sales:warehouse' })

for (const [label, value] of [
  ['no result', null],
  ['no timings', {}],
  ['empty timings', result()],
  ['two queries', result(timing(), timing({ index: 1 }))],
  ['failed query', result(timing({ error: 'timeout' }))],
  ['no connection recorded', result(timing({ connection_id: null }))],
  ['no agent recorded', result(timing({ data_source_id: undefined }))],
  ['already read from a cached table', result(timing({ client_key: 'Sales:warehouse::fast' }))],
]) {
  assert.equal(cacheCandidateFrom(value), null, label)
}

// The query text is never needed to decide: a listed result does not carry it.
assert.ok(cacheCandidateFrom({ query_timings: [timing()], executed_queries: undefined }))

// Names follow the backend rule ^[A-Za-z_][A-Za-z0-9_]{0,62}$.
const RULE = /^[A-Za-z_][A-Za-z0-9_]{0,62}$/
for (const title of [
  'Total Sales and Units Sold by Year-Month and Store Type',
  '2024 revenue',
  '  spaced   out  ',
  'Ventas por región (€)',
  'מכירות לפי חנות',
  '',
  null,
  'x'.repeat(200),
  '!!!',
]) {
  const name = cacheTableNameFor(title)
  assert.match(name, RULE, `${JSON.stringify(title)} -> ${name}`)
}
assert.equal(cacheTableNameFor('Total Sales by Store Type'), 'total_sales_by_store_type')
assert.equal(cacheTableNameFor('2024 revenue'), 't_2024_revenue')
assert.equal(cacheTableNameFor('מכירות'), 'cached_table')

console.log('PASS: cache candidate rules and table naming')

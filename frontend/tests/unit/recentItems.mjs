import assert from 'node:assert/strict'

import { splitRecent, RECENT_LIMIT, RECENT_MIN_LIST } from '../../utils/recentItems.ts'

// Run: node frontend/tests/unit/recentItems.mjs

const day = (n) => new Date(Date.UTC(2026, 0, n)).toISOString()
const roster = (n, used = {}) =>
  Array.from({ length: n }, (_, i) => ({ id: `a${i}`, name: `Agent ${i}`, last_used_at: used[i] ?? null }))

// Recent = the most recently used items, newest first, capped.
{
  const items = roster(10, { 7: day(3), 2: day(9), 5: day(1), 9: day(5) })
  const { recent, rest } = splitRecent(items)
  assert.deepEqual(recent.map((x) => x.id), ['a2', 'a9', 'a7'], 'newest first, capped at the limit')
  assert.equal(recent.length, RECENT_LIMIT)
  // Every item appears exactly once across both sections.
  assert.equal(recent.length + rest.length, items.length)
  assert.equal(new Set([...recent, ...rest].map((x) => x.id)).size, items.length)
  // The rest keeps the caller's order — usage never reshuffles it.
  const restIds = rest.map((x) => x.id)
  assert.deepEqual(restIds, [...restIds].sort((a, b) => items.findIndex((x) => x.id === a) - items.findIndex((x) => x.id === b)))
  assert.ok(restIds.includes('a5'), 'a used item past the cap stays in the rest')
}

// Never-used items never enter Recent, so a fresh user sees just the list.
{
  const { recent, rest } = splitRecent(roster(12))
  assert.equal(recent.length, 0)
  assert.equal(rest.length, 12)
}

// Short lists get no Recent head, however they were used.
{
  const short = roster(RECENT_MIN_LIST - 1, { 0: day(1), 1: day(2) })
  assert.equal(splitRecent(short).recent.length, 0)
  assert.equal(splitRecent(roster(RECENT_MIN_LIST, { 0: day(1) })).recent.length, 1)
}

// Bad timestamps are treated as never used rather than sorting as NaN.
{
  const items = roster(8, { 0: 'not-a-date', 1: day(2) })
  assert.deepEqual(splitRecent(items).recent.map((x) => x.id), ['a1'])
}

// Options override the defaults; the input is not mutated.
{
  const items = roster(4, { 0: day(1), 1: day(2), 2: day(3) })
  const before = items.map((x) => x.id)
  assert.deepEqual(splitRecent(items, { limit: 2, minList: 2 }).recent.map((x) => x.id), ['a2', 'a1'])
  assert.deepEqual(items.map((x) => x.id), before)
}

console.log('recentItems: ok')

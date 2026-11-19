import assert from 'node:assert/strict'

import {
  placeSteers,
  steerSlot,
  isSteerApplied,
  groupSteersByParent,
} from '../../composables/useSteerPlacement.ts'

// A turn's steps: loop 0 plans, its tool runs long, loop 1 and 2 follow.
// started_at is when a step's planning finished, not when it began.
const blocks = [
  { id: 'b0', loop_index: 0, started_at: '2026-01-01T10:00:10' },
  { id: 'b1', loop_index: 1, started_at: '2026-01-01T10:02:00' },
  { id: 'b2', loop_index: 2, started_at: '2026-01-01T10:03:00' },
]

const slots = (placement) => ({
  before: Object.fromEntries([...placement.before].map(([i, list]) => [i, list.map(s => s.id)])),
  tail: placement.tail.map(s => s.id),
})

// --- picked up: above the first step that acted on it ------------------------

// Sent during loop 0's long tool call, picked up at loop 1 -> above b1,
// even though by send time it would have gone elsewhere.
assert.equal(steerSlot({ id: 's', created_at: '2026-01-01T10:01:00', steering_loop_index: 1 }, blocks), 1)

// Pickup position wins over send time in both directions.
assert.equal(steerSlot({ id: 's', created_at: '2026-01-01T10:02:30', steering_loop_index: 1 }, blocks), 1)
assert.equal(steerSlot({ id: 's', created_at: '2026-01-01T09:00:00', steering_loop_index: 2 }, blocks), 2)

// The acting step is not in the stream (cancelled skeleton removed, harness
// filtered): the next later step still anchors it.
assert.equal(
  steerSlot({ id: 's', steering_loop_index: 1 }, [blocks[0], blocks[2]]),
  1,
)

// The acting step has not streamed in yet -> tail, never above an older step.
assert.equal(steerSlot({ id: 's', created_at: '2026-01-01T09:00:00', steering_loop_index: 3 }, blocks), -1)

// Blocks without a loop index (unexpected payloads) are never anchors.
assert.equal(steerSlot({ id: 's', steering_loop_index: 0 }, [{ id: 'x' }, blocks[1]]), 1)

// --- pending: by send time ---------------------------------------------------

assert.equal(steerSlot({ id: 's', created_at: '2026-01-01T10:00:00' }, blocks), 0)
assert.equal(steerSlot({ id: 's', created_at: '2026-01-01T10:01:00' }, blocks), 1)
assert.equal(steerSlot({ id: 's', created_at: '2026-01-01T10:02:00' }, blocks), 1)
assert.equal(steerSlot({ id: 's', created_at: '2026-01-01T10:05:00' }, blocks), -1)
// null loop index (pre-migration rows) is the same as pending
assert.equal(steerSlot({ id: 's', created_at: '2026-01-01T10:01:00', steering_loop_index: null }, blocks), 1)

// A freshly streamed skeleton without server timestamps uses its arrival stamp.
assert.equal(
  steerSlot({ id: 's', created_at: '2026-01-01T10:04:00' }, [...blocks, { id: 'sk', _client_arrived_at: '2026-01-01T10:04:01' }]),
  3,
)

// --- placeSteers: exactly one slot per steer --------------------------------

{
  const steers = [
    { id: 'a', created_at: '2026-01-01T10:01:00', steering_loop_index: 1 },
    { id: 'b', created_at: '2026-01-01T10:01:30', steering_loop_index: 1 },
    { id: 'c', created_at: '2026-01-01T10:05:00' },
    { id: 'd', created_at: '2026-01-01T10:00:00' },
  ]
  const placed = placeSteers(steers, blocks)
  assert.deepEqual(slots(placed), { before: { 0: ['d'], 1: ['a', 'b'] }, tail: ['c'] })
  const all = [...[...placed.before.values()].flat(), ...placed.tail].map(s => s.id).sort()
  assert.deepEqual(all, ['a', 'b', 'c', 'd'])
}

// Non-monotonic block starts (planning finished out of order) must not drop
// or duplicate a pending steer.
{
  const skewed = [
    { id: 'x0', loop_index: 0, started_at: '2026-01-01T10:03:00' },
    { id: 'x1', loop_index: 1, started_at: '2026-01-01T10:01:00' },
  ]
  const placed = placeSteers([{ id: 'p', created_at: '2026-01-01T10:02:00' }], skewed)
  assert.deepEqual(slots(placed), { before: { 0: ['p'] }, tail: [] })
}

// No blocks yet: nothing is placed (the page shows the steer above the turn).
assert.deepEqual(slots(placeSteers([{ id: 'a', steering_loop_index: 0 }], [])), { before: {}, tail: [] })
assert.deepEqual(slots(placeSteers([], blocks)), { before: {}, tail: [] })

// --- isSteerApplied: live ack or persisted pickup ----------------------------

assert.equal(isSteerApplied({ id: 's' }), false)
assert.equal(isSteerApplied({ id: 's', steering_loop_index: null, steering_applied_at: null }), false)
assert.equal(isSteerApplied({ id: 's', steering_applied: true }), true)
assert.equal(isSteerApplied({ id: 's', steering_applied_at: '2026-01-01T10:02:00' }), true)
assert.equal(isSteerApplied({ id: 's', steering_loop_index: 0 }), true)

// --- groupSteersByParent: only steering user rows, keyed by their turn ------

{
  const rows = [
    { id: 'u1', role: 'user', message_type: 'table' },
    { id: 'sys1', role: 'system', message_type: 'table', parent_id: 'u1' },
    { id: 's1', role: 'user', message_type: 'steering', parent_id: 'sys1' },
    { id: 's2', role: 'user', message_type: 'steering', parent_id: 'sys1' },
    { id: 's3', role: 'user', message_type: 'steering', parent_id: 'sys2' },
    { id: 'orphan', role: 'user', message_type: 'steering', parent_id: null },
    { id: 'ev', role: 'event', message_type: 'steering', parent_id: 'sys1' },
  ]
  const grouped = groupSteersByParent(rows)
  assert.deepEqual([...grouped.keys()].sort(), ['sys1', 'sys2'])
  assert.deepEqual(grouped.get('sys1').map(s => s.id), ['s1', 's2'])
  assert.deepEqual(grouped.get('sys2').map(s => s.id), ['s3'])
  assert.equal(groupSteersByParent([]).size, 0)
}

console.log('useSteerPlacement: ok')

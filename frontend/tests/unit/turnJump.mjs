import assert from 'node:assert/strict'

import { createMessageJumper } from '../../utils/turnIndex.ts'

// Jump-to-message from the conversation map: a jump to an unloaded turn pages
// older history (one delayed request per round here), then scrolls to it.
// Contract: the latest click wins while a load is in flight — a click on a
// loaded turn scrolls at once and is not undone when the load finishes; a
// click on another unloaded turn retargets the load and only it is scrolled to.

// A fake transcript: `loaded` turns are rendered; each loadOlder() request is
// held until the test releases it, then loads every turn down to `until`.
function harness({ all = ['t1', 't2', 't3', 't4', 't5', 't6'], loaded = ['t5', 't6'] } = {}) {
  const state = { loaded: new Set(loaded), scrolls: [], pending: [], requests: [], held: [] }
  const order = [...all]
  const jump = createMessageJumper({
    isLoaded: id => state.loaded.has(id),
    hasMore: () => order.some(id => !state.loaded.has(id)),
    count: () => state.loaded.size,
    cursor: () => order.find(id => state.loaded.has(id)),
    waitFirstPage: async () => {},
    waitIdle: async () => {},
    loadOlder: until => new Promise(resolve => {
      state.requests.push(until)
      state.held.push(() => {
        for (const id of order.slice(order.indexOf(until))) state.loaded.add(id)
        resolve()
      })
    }),
    scrollTo: id => state.scrolls.push(id),
    setPending: id => state.pending.push(id),
  })
  const release = async () => { state.held.shift()(); await flush() }
  return { state, jump, release }
}
const flush = () => new Promise(r => setTimeout(r, 0))

// Loaded turn: scroll at once, no request.
{
  const { state, jump } = harness()
  await jump('t5')
  assert.deepEqual(state.requests, [])
  assert.deepEqual(state.scrolls, ['t5'])
}

// Unloaded turn: one request reaching back to it, loading state while it
// runs, then a single scroll to it.
{
  const { state, jump, release } = harness()
  const done = jump('t2')
  await flush()
  assert.deepEqual(state.requests, ['t2'])
  assert.equal(state.pending.at(-1), 't2', 'tick shows loading while the request runs')
  assert.deepEqual(state.scrolls, [])
  await release(); await done
  assert.deepEqual(state.scrolls, ['t2'])
  assert.equal(state.pending.at(-1), null, 'loading state cleared')
}

// Unloaded click, then a click on a LOADED turn while the request is in
// flight: the reader goes there at once, and the finishing request must not
// pull them back to the first target.
{
  const { state, jump, release } = harness()
  const first = jump('t1')
  await flush()
  await jump('t6')
  assert.deepEqual(state.scrolls, ['t6'], 'the loaded turn is shown immediately')
  await release(); await first
  assert.deepEqual(state.scrolls, ['t6'], 'the in-flight load does not scroll back to t1')
}

// Unloaded click, then a click on ANOTHER unloaded turn while the request is
// in flight: the second click is not dropped — the load continues toward it
// and only the latest target is scrolled to.
{
  const { state, jump, release } = harness()
  const first = jump('t3')
  await flush()
  const second = jump('t1')
  assert.equal(state.pending.at(-1), 't1', 'the tick now loading is the latest one')
  await release() // first request (until=t3) lands; t1 still missing
  assert.deepEqual(state.requests, ['t3', 't1'], 'the running load retargets to the latest click')
  await release(); await Promise.all([first, second])
  assert.deepEqual(state.scrolls, ['t1'])
}

// A request that makes no progress (failure / stuck cursor) stops the loop
// instead of spinning, and clears the loading state.
{
  const state = { requests: 0, pending: [], scrolls: [] }
  const jump = createMessageJumper({
    isLoaded: () => false, hasMore: () => true, count: () => 2, cursor: () => 'c',
    waitFirstPage: async () => {}, waitIdle: async () => {},
    loadOlder: async () => { state.requests++ },
    scrollTo: id => state.scrolls.push(id), setPending: id => state.pending.push(id),
  })
  await jump('missing')
  assert.equal(state.requests, 1)
  assert.equal(state.pending.at(-1), null)
}

console.log('turnJump: ok')

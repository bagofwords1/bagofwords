import assert from 'node:assert/strict'

import {
  createStreamDeliveryTracker,
  isBufferedDelivery,
  parseServerTimestamp,
} from '../../utils/streamDelivery.ts'

// Run: node tests/unit/streamDelivery.mjs

const headers = (h) => ({ headers: { get: (n) => h[n.toLowerCase()] ?? null } })
const iso = (ms) => new Date(ms).toISOString().replace('Z', '') + '123' // naive UTC + microseconds, like the backend
const T0 = Date.UTC(2026, 9, 18, 20, 0, 0)

// Timestamps: naive UTC with microseconds is how the backend sends them.
assert.equal(parseServerTimestamp('2026-10-18T20:00:00.123456'), T0 + 123)
assert.equal(parseServerTimestamp('2026-10-18T20:00:00'), T0)
assert.equal(parseServerTimestamp('2026-10-18T20:00:00.5Z'), T0 + 500)
assert.equal(parseServerTimestamp('not a date'), null)
assert.equal(parseServerTimestamp(undefined), null)

// Feed `n` tokens produced every `gapMs` on the server; `arrival(i)` says when
// the browser read each one. Browser clock deliberately offset from the server.
function run(n, gapMs, arrival, resp = headers({})) {
  const t = createStreamDeliveryTracker('kickoff', resp)
  t.observe('completion.started', { timestamp: iso(T0) }, 5_000_000)
  for (let i = 0; i < n; i++) {
    t.observe('block.delta.token', { timestamp: iso(T0 + i * gapMs) }, arrival(i))
  }
  t.observe('block.delta.text', { timestamp: iso(T0) }, 9_999_999) // only token events count
  return t.summary()
}

const CLOCK_OFFSET = 7_654_321 // the browser's clock has nothing to do with the server's

// Live: every token arrives ~40 ms after it was produced.
for (const [n, gap] of [[50, 20], [600, 16], [10, 500]]) {
  const s = run(n, gap, (i) => CLOCK_OFFSET + i * gap + 40)
  assert.ok(s.holdback_ms <= 1, `live stream (${n}x${gap}ms) has no hold-back, got ${s.holdback_ms}`)
  assert.equal(s.token_events, n)
  assert.equal(s.network_reads, n)
  assert.equal(isBufferedDelivery(s), false)
}

// Fully buffered: everything arrives in one read at the end.
for (const [n, gap] of [[600, 16], [100, 40], [40, 500]]) {
  const span = (n - 1) * gap
  const s = run(n, gap, () => CLOCK_OFFSET + span + 300)
  assert.equal(s.holdback_ms, span, `buffered stream is held back for the whole span (${n}x${gap}ms)`)
  assert.equal(s.server_span_ms, span)
  assert.equal(s.network_reads, 1)
  assert.equal(isBufferedDelivery(s), span >= 1500)
}

// Held in 64 KB-style chunks: a few reads, each holding back a few seconds.
{
  const s = run(600, 16, (i) => CLOCK_OFFSET + Math.ceil((i + 1) / 200) * 200 * 16)
  assert.equal(s.network_reads, 3)
  assert.ok(s.holdback_ms >= 3000 && s.holdback_ms <= 3300, `chunked hold-back ~3.2s, got ${s.holdback_ms}`)
  assert.equal(isBufferedDelivery(s), true)
}

// A single slow moment in an otherwise live stream is not "buffered".
{
  const s = run(600, 16, (i) => CLOCK_OFFSET + i * 16 + (i < 100 ? 1200 : 40))
  assert.ok(s.holdback_ms >= 1100 && s.holdback_ms <= 1200)
  assert.equal(isBufferedDelivery(s), false)
}

// Too few tokens to judge.
assert.equal(run(1, 10, () => 0), null)
assert.equal(isBufferedDelivery(null), false)

// Response headers are captured, cleaned to printable ASCII and bounded.
{
  const s = run(5, 10, (i) => i, headers({ 'content-encoding': 'gzip', via: '1.1 gw\r\nX-Evil: 1' + 'x'.repeat(400) }))
  assert.equal(s.content_encoding, 'gzip')
  assert.ok(!/[\r\n]/.test(s.via) && s.via.length <= 256 && s.via.startsWith('1.1 gw'))
  const none = run(5, 10, (i) => i, null)
  assert.equal(none.content_encoding, null)
  assert.equal(none.via, null)
}

console.log('streamDelivery: all assertions passed')

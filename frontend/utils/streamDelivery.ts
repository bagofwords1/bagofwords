// Measures whether a live chat stream reached the browser as it was produced,
// or was held back on the way (a buffering proxy, TLS inspection, AV/DLP
// scanner) and arrived all at once.
//
// Each token event carries the server time it was produced. For every token we
// compare "how long before the last token the server produced it" with "how
// long before the last token it arrived here". The largest difference is the
// hold-back. Server and browser clocks never get compared directly, so clock
// skew does not matter. A streamed answer has a hold-back near zero; a fully
// buffered one has a hold-back close to the whole answer's duration.

export type StreamKind = 'kickoff' | 'watch'

export interface StreamDeliveryReport {
  kind: StreamKind
  holdback_ms: number
  server_span_ms: number
  token_events: number
  network_reads: number
  content_encoding: string | null
  via: string | null
}

// Same thresholds as backend/app/streaming/delivery.py.
export const BUFFERED_MIN_HOLDBACK_MS = 1500
export const BUFFERED_MIN_SHARE = 0.25

const TOKEN_EVENT = 'block.delta.token'

// SSE event timestamps are naive UTC ISO strings with microseconds
// ("2026-10-18T20:13:12.123456"). Keep milliseconds and read them as UTC.
export function parseServerTimestamp(ts: unknown): number | null {
  if (typeof ts !== 'string' || !ts) return null
  const m = ts.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d{1,3})?\d*(Z|[+-]\d{2}:?\d{2})?$/)
  if (!m) return null
  const ms = Date.parse(`${m[1]}${m[2] || ''}${m[3] || 'Z'}`)
  return Number.isFinite(ms) ? ms : null
}

function headerValue(v: string | null | undefined, max: number): string | null {
  if (!v) return null
  // Printable ASCII only, as the backend schema requires.
  const clean = v.replace(/[^\x20-\x7E]/g, '').slice(0, max)
  return clean || null
}

export function createStreamDeliveryTracker(kind: StreamKind, response?: { headers?: { get(name: string): string | null } } | null) {
  // holdback = max_i[(lastServer - s_i) - (lastArrival - a_i)]
  //          = max_i(a_i - s_i) - (lastArrival - lastServer)
  // so a running maximum is enough: constant memory however long the turn.
  let count = 0
  let firstServer = 0
  let lastServer = 0
  let lastArrival = 0
  let maxLag = -Infinity
  let reads = 0
  let lastReadAt: number | null = null
  const contentEncoding = headerValue(response?.headers?.get('content-encoding'), 64)
  const via = headerValue(response?.headers?.get('via'), 256)

  return {
    // Call for every SSE event, with the time the network read that carried
    // it completed (not when it was processed: a buffered stream is processed
    // event by event long after it arrived in one piece).
    observe(eventName: string | null, parsed: any, arrivedAt: number) {
      if (eventName !== TOKEN_EVENT) return
      const serverMs = parseServerTimestamp(parsed?.timestamp)
      if (serverMs === null) return
      if (count === 0) firstServer = serverMs
      count++
      lastServer = serverMs
      lastArrival = arrivedAt
      maxLag = Math.max(maxLag, arrivedAt - serverMs)
      if (arrivedAt !== lastReadAt) { reads++; lastReadAt = arrivedAt }
    },

    // The delivery summary, or null when there were too few tokens to judge.
    summary(): StreamDeliveryReport | null {
      if (count < 2) return null
      return {
        kind,
        holdback_ms: Math.max(0, Math.round(maxLag - (lastArrival - lastServer))),
        server_span_ms: Math.max(0, Math.round(lastServer - firstServer)),
        token_events: count,
        network_reads: reads,
        content_encoding: contentEncoding,
        via,
      }
    },
  }
}

export function isBufferedDelivery(r: StreamDeliveryReport | null): r is StreamDeliveryReport {
  return !!r && r.holdback_ms >= BUFFERED_MIN_HOLDBACK_MS && r.holdback_ms >= BUFFERED_MIN_SHARE * r.server_span_ms
}

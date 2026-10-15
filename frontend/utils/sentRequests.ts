// A connector that sent the source something other than the query in the code
// — Priority answers a subform table through its parent's $expand — records
// the request it actually sent on the query's timing record
// (`result_json.query_timings[].sent_request`). The timing records survive in
// the persisted step summary, so these are shown after a refresh too.

function sentRequests(timings: any[]): any[] {
  return (Array.isArray(timings) ? timings : []).filter(
    (t) => t && typeof t.sent_request === 'string' && t.sent_request,
  )
}

// Full Code view: the code as written, then each request actually sent as a
// trailing comment block — display only, never part of the code that runs.
export function withSentRequestsAppended(code: string, timings: any[], label: string): string {
  const sent = sentRequests(timings).map((t) => t.sent_request as string)
  if (!sent.length) return code
  const lines = [...new Set(sent)].map((request) => `# ${request}`)
  return `${code.replace(/\s+$/, '')}\n\n# ${label}:\n${lines.join('\n')}`
}

// Queries view: pair each query with its timing record by the query text the
// record keeps (`sql`, the first 500 chars), not by position — the two lists
// are built separately and need not line up.
export function withSentRequests(queries: string[], timings: any[], label: string): string[] {
  const pending = sentRequests(timings).filter((t) => typeof t.sql === 'string')
  return queries.map((query) => {
    const text = String(query ?? '')
    const i = pending.findIndex((t) => text.startsWith(t.sql))
    if (i === -1) return text
    const [match] = pending.splice(i, 1)
    return `${text}\n\n-- ${label}:\n${match.sent_request}`
  })
}

/**
 * Human-readable text for a tool execution's `result_json.error`.
 *
 * Tools disagree on the shape: a failure that actually ran sets a plain
 * string, but every pre-dispatch refusal in agent_v2 (`_refuse_before_dispatch`
 * — the per-turn artifact budget, tool/plan_type mismatches) sets
 * `{ code, message }`. Rendering that object directly dumped raw JSON into the
 * tool card, so the budget refusal's explanation reached the user as
 * `{ "code": "artifact_budget_exhausted", "message": "Edit limit ..." }`.
 *
 * Always returns a string safe to render; unknown shapes degrade to ''.
 */
export function toolErrorText(error: unknown): string {
  if (!error) return ''
  if (typeof error === 'string') return error
  if (typeof error === 'object') {
    const err = error as Record<string, unknown>
    for (const key of ['message', 'detail', 'error']) {
      const value = err[key]
      if (typeof value === 'string' && value) return value
    }
    return ''
  }
  return String(error)
}

/**
 * Which request refreshes a result card's rows.
 *
 * Most cards show a query's default step, so refreshing means fetching that
 * step. A viewer-run card (run_query) shows ONE parameter slice, and the
 * default step answers the query's saved values — fetching it there swaps the
 * rows out from under a header that still names the requested params.
 *
 * That is not a rare path: the report serializer caps a card's inline rows at
 * 20 and marks the payload `truncated`, and every refresh trigger (expanding
 * a card after reload, exporting to a spreadsheet, the editor broadcasting a
 * new default step) hydrates on that flag. A run returning more than 20 rows
 * therefore reverts to the saved snapshot without the slice ever being wrong
 * on screen first.
 *
 * So a viewer-run card re-requests its own slice instead. That is a lookup,
 * not a re-execution: the server caches per (step, viewer, params
 * fingerprint), so the same values come back from `StepUserResult`.
 */

export interface ViewerRun {
  /** The query whose saved code produced this slice. */
  queryId: string
  /** The values it ran with, as the server resolved them. */
  params: Record<string, any>
}

export interface ParamSpecLike {
  name?: string
  source?: string
}

export interface HydrationRequest {
  url: string
  method: 'GET' | 'POST'
  body?: Record<string, any>
}

/**
 * Identity-sourced values are resolved from the session server-side and are
 * rejected outright when a client submits them (`resolve_param_values`), so
 * they must be stripped before a re-request. `input_identity_default` is an
 * ordinary input and stays.
 */
export function stripIdentityParams(
  params: Record<string, any> | null | undefined,
  specs: ParamSpecLike[] | null | undefined,
): Record<string, any> {
  const identityNames = new Set(
    (specs || [])
      .filter((s) => s && s.source === 'identity' && s.name)
      .map((s) => s.name as string),
  )
  const out: Record<string, any> = {}
  for (const [k, v] of Object.entries(params || {})) {
    if (!identityNames.has(k)) out[k] = v
  }
  return out
}

/**
 * The slice a tool execution represents, read off the execution itself.
 *
 * Derived rather than passed down, because the card is mounted from more than
 * one place: the transcript renders it inline, and "Open in panel" mounts a
 * SECOND, fresh component from stored panel state. A prop threaded through the
 * first path silently fails to reach the second — the panel then falls back to
 * the default step and shows the saved snapshot, which is the same defect by a
 * different door. Reading the run context from the execution closes every
 * mount point at once, including ones added later.
 *
 * Only run_query executions describe a slice: for create_data and
 * describe_entity the query's default step IS their result, so fetching it is
 * correct there and must stay untouched.
 */
export function deriveViewerRun(toolExecution: any): ViewerRun | null {
  const te = toolExecution
  if (!te || te.tool_name !== 'run_query') return null
  const rj = te.result_json
  if (!rj || rj.success !== true) return null
  const qid = rj.query_id
  if (!qid) return null
  return { queryId: String(qid), params: rj.applied_params || {} }
}

/**
 * The request that refreshes this card.
 *
 * `currentApplied` is the slice the card is showing right now — after someone
 * changes values in the card's own parameter bar it diverges from the run the
 * agent performed, and the refresh must follow what is on screen, not the
 * original call.
 *
 * Returns null when there is nothing to fetch, so callers keep their existing
 * fallbacks (a bare step id, or no query at all).
 */
export function cardHydrationRequest(args: {
  viewerRun?: ViewerRun | null
  queryId?: string | null
  currentApplied?: Record<string, any> | null
  paramSpecs?: ParamSpecLike[] | null
}): HydrationRequest | null {
  const { viewerRun, queryId, currentApplied, paramSpecs } = args

  if (viewerRun && viewerRun.queryId) {
    const source =
      currentApplied && Object.keys(currentApplied).length
        ? currentApplied
        : viewerRun.params || {}
    return {
      url: `/api/queries/${viewerRun.queryId}/run`,
      method: 'POST',
      body: { mode: 'viewer', params: stripIdentityParams(source, paramSpecs) },
    }
  }

  if (queryId) {
    return { url: `/api/queries/${queryId}/default_step`, method: 'GET' }
  }

  return null
}

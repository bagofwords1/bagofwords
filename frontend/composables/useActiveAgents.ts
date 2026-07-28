/**
 * Shared access to GET /data_sources/active?include_unconnected=true — the
 * agent list the prompt box's picker and the '@' mention menu both render.
 *
 * Several components need that list at the same moment on the same page: the
 * DataSourceSelector inside PromptBoxV2, and MentionInput (which also refetches
 * whenever the selection changes — and the selector emits its default selection
 * right after its own fetch resolves). Each issuing its own request meant the
 * same payload was fetched two or three times per page load, and again on the
 * new page after creating a report.
 *
 * Callers share one in-flight request and a short freshness window, the same
 * shape as useAgent.initAgent(). Pass { force: true } after something changes
 * the list (connecting an agent), which is exactly when the window must not
 * apply.
 */
let inflight: Promise<any[]> | null = null
// Monotonic id of the newest request. A forced refresh can start while an
// earlier fetch is still open, and without this the loser could finish last and
// write its stale list over the fresh one — which would make an agent the user
// just connected vanish again — or clear `inflight` while the newer request is
// still running, so the next caller starts a third fetch instead of joining.
// Only the newest request touches shared state.
let latestRequestId = 0
let lastFetchedAt = 0
const CACHE_TTL_MS = 10_000

// Module-level so every caller shares one list (mirrors useAgent.ts).
const activeAgents = ref<any[]>([])

export function useActiveAgents() {
  async function fetchActiveAgents(opts: { force?: boolean } = {}): Promise<any[]> {
    if (!opts.force) {
      if (inflight) return inflight
      if (lastFetchedAt && Date.now() - lastFetchedAt < CACHE_TTL_MS) return activeAgents.value
    }

    const requestId = ++latestRequestId
    const request = (async () => {
      try {
        const { data, error } = await useMyFetch<any[]>('/data_sources/active', {
          method: 'GET',
          query: { include_unconnected: true },
          timeout: 8000,
        })
        // Only a successful response refreshes the cache: a failed request must
        // not park an empty list for the length of the window. And only the
        // newest one does, so a slow loser can't overwrite a fresher list.
        if (!error.value && Array.isArray(data.value) && requestId === latestRequestId) {
          activeAgents.value = data.value
          lastFetchedAt = Date.now()
        }
        return activeAgents.value
      } finally {
        if (requestId === latestRequestId) inflight = null
      }
    })()
    inflight = request
    return request
  }

  return { activeAgents, fetchActiveAgents }
}

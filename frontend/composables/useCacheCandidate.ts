/**
 * "Save as cached table" on a cited result.
 *
 * A result the agent produced by running ONE query against ONE connection can
 * be turned into a BOW custom table: the same query, materialized locally and
 * refreshed on a schedule. This decides when that offer makes sense and
 * gathers what the custom-table dialog needs to open pre-filled.
 *
 * The offer is hidden, never disabled, when it cannot work:
 *   - the result came from more than one query, so there is no single
 *     definition to keep
 *   - it was already answered from a cached table (`…::fast` client)
 *   - the connection cannot host custom tables (per-user credentials, or a
 *     connector that cannot run a query)
 *   - the viewer is not an admin of that connection
 *   - custom tables are switched off for the organization
 * The backend enforces every one of these again on preview and save.
 */
import type { CacheCandidate, CacheConnection } from '~/utils/cacheCandidate'

/**
 * The full query text behind a result, from the server. Returns the reason
 * instead when the server refuses (the result changed shape, access was
 * revoked since the page loaded).
 */
export async function fetchCacheDefinition(
  candidate: CacheCandidate, toolExecutionId: string,
): Promise<{ definition?: string; error?: string }> {
  try {
    const { data, error } = await useMyFetch(
      `/connections/${candidate.connectionId}/custom-queries/from-result/${toolExecutionId}`, { method: 'GET' })
    if (error.value) return { error: (error.value as any)?.data?.detail || 'Failed' }
    const definition = (data.value as any)?.definition_sql
    return typeof definition === 'string' && definition ? { definition } : { error: 'Failed' }
  } catch {
    return { error: 'Failed' }
  }
}

/**
 * Connections of an agent that can host custom tables, fetched once per agent
 * per page and shared by every result card that asks.
 */
export function useCacheConnections() {
  const cache = useState<Record<string, CacheConnection[]>>('bow-cache-connections', () => ({}))
  const pending = useState<Record<string, boolean>>('bow-cache-connections-pending', () => ({}))

  async function ensureLoaded(dataSourceId: string) {
    if (!dataSourceId || cache.value[dataSourceId] || pending.value[dataSourceId]) return
    pending.value = { ...pending.value, [dataSourceId]: true }
    let found: CacheConnection[] = []
    try {
      const { data, error } = await useMyFetch(`/data_sources/${dataSourceId}/connections`, { method: 'GET' })
      if (!error.value && Array.isArray(data.value)) {
        found = (data.value as any[])
          .filter((c: any) => c?.custom_queries_supported)
          .map((c: any) => ({ id: String(c.id), name: c.name, type: c.type }))
      }
    } catch {
      /* no offer is the safe outcome */
    }
    cache.value = { ...cache.value, [dataSourceId]: found }
    pending.value = { ...pending.value, [dataSourceId]: false }
  }

  function connectionFor(candidate: CacheCandidate | null): CacheConnection | null {
    if (!candidate) return null
    const list = cache.value[candidate.dataSourceId] || []
    return list.find((c) => c.id === candidate.connectionId) || null
  }

  return { ensureLoaded, connectionFor }
}

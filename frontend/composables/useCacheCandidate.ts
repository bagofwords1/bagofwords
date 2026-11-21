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
import { FAST_CONNECTION_SUFFIX } from '~/composables/useFastTable'

export interface CacheCandidate {
  connectionId: string
  dataSourceId: string
  clientKey: string
}

export interface CacheConnection {
  id: string
  name: string
  type?: string
}

/**
 * Where the one query behind a tool result ran, or null when the result is
 * not a single query against a single connection.
 *
 * Read from the per-query timing records, which every viewer of the result
 * gets. The query text itself is not in a listed result (it is fetched for
 * the dialog, see `fetchCacheDefinition`), so nothing here depends on it.
 */
export function cacheCandidateFrom(resultJson: any): CacheCandidate | null {
  const timings = resultJson?.query_timings
  if (!Array.isArray(timings) || timings.length !== 1) return null
  const timing = timings[0] || {}
  if (timing.error) return null
  const connectionId = timing.connection_id ? String(timing.connection_id) : ''
  const dataSourceId = timing.data_source_id ? String(timing.data_source_id) : ''
  const clientKey = timing.client_key ? String(timing.client_key) : ''
  if (!connectionId || !dataSourceId) return null
  if (clientKey.endsWith(FAST_CONNECTION_SUFFIX)) return null
  return { connectionId, dataSourceId, clientKey }
}

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
 * A custom-table name from a result's title. Names follow the backend rule
 * ^[A-Za-z_][A-Za-z0-9_]{0,62}$; a title with nothing usable falls back to a
 * generic name the admin will rename in the dialog.
 */
export function cacheTableNameFor(title?: string | null): string {
  const base = String(title || '')
    .normalize('NFKD')
    .replace(/[^A-Za-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .toLowerCase()
  if (!base) return 'cached_table'
  const safe = /^[a-z_]/.test(base) ? base : `t_${base}`
  return safe.slice(0, 63).replace(/_+$/g, '')
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

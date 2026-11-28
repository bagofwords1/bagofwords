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
 *   - the result is parameterized, or its card is showing a re-run
 *   - the connection cannot host custom tables (a connector that cannot
 *     run a query). A connection where each person signs in with their own
 *     credentials can: the table is then cached once per person.
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
 * Whether a connection can host a custom table right now, asked of the
 * connection itself and shared by every result card on the page.
 *
 * Asked per connection, not per agent: the action needs `manage_connection`,
 * and someone who has it may hold no more than ordinary access to the agent
 * the result came from. A lookup that failed is not remembered, so a blip does
 * not hide the action for the rest of the page's life.
 */
export function useCacheConnections() {
  const cache = useState<Record<string, CacheConnection | null>>('bow-cache-connections', () => ({}))
  const pending = useState<Record<string, boolean>>('bow-cache-connections-pending', () => ({}))

  async function ensureLoaded(connectionId: string) {
    if (!connectionId || connectionId in cache.value || pending.value[connectionId]) return
    pending.value = { ...pending.value, [connectionId]: true }
    try {
      const { data, error } = await useMyFetch(
        `/connections/${connectionId}/custom-queries/capability`, { method: 'GET' })
      if (!error.value && data.value) {
        const c: any = data.value
        cache.value = {
          ...cache.value,
          [connectionId]: c.supported ? { id: String(c.id), name: c.name, type: c.type } : null,
        }
      }
    } catch {
      /* not remembered: the next card to ask tries again */
    } finally {
      pending.value = { ...pending.value, [connectionId]: false }
    }
  }

  function connectionFor(candidate: CacheCandidate | null): CacheConnection | null {
    if (!candidate) return null
    return cache.value[candidate.connectionId] || null
  }

  return { ensureLoaded, connectionFor }
}

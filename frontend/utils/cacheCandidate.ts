/**
 * The pure rules behind "save as cached table": which tool results have one
 * query worth keeping, and what to call the table. Kept free of Nuxt imports
 * so they run under `node --test` (tests/unit/cacheCandidate.mjs).
 */

// Client key suffix of a connection's cached sibling (`<agent>:<conn>::fast`).
// Mirrors FAST_CONNECTION_SUFFIX in composables/useFastTable.ts.
const FAST_CONNECTION_SUFFIX = '::fast'

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
  // A parameterized result is one viewer's values; its card can be re-run
  // with others and then shows rows this query did not produce.
  if (Array.isArray(resultJson?.parameters) && resultJson.parameters.length) return null
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

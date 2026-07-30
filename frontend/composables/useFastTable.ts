/**
 * Identifying BOW custom queries ("cached" relations) in tool output.
 *
 * A custom query is materialized to a local artifact and served by the
 * connection's `::fast` sibling client, so a tool call that reads one never
 * touched the source database. Badging it tells the user why an answer was
 * instant — and, when a figure looks surprising, that they are looking at a
 * cached copy with an `as_of` rather than live data.
 *
 * Two signals, because tool payloads differ in what they carry:
 *   - `connection_name` ending in `::fast` (describe_tables' top_tables)
 *   - the agent's `cached_tables` (create_data / inspect_data, whose arguments
 *     carry bare table names only)
 */

export const FAST_CONNECTION_SUFFIX = '::fast'

export function isFastConnectionName(name?: string | null): boolean {
  return !!name && name.endsWith(FAST_CONNECTION_SUFFIX)
}

/** Strip the suffix for display — users think in terms of the connection. */
export function baseConnectionName(name?: string | null): string {
  if (!name) return ''
  return name.endsWith(FAST_CONNECTION_SUFFIX)
    ? name.slice(0, -FAST_CONNECTION_SUFFIX.length)
    : name
}

function normalize(name: string): string {
  const t = name.toLowerCase()
  // The model may qualify a name (`public.revenue_summary`) where the catalog
  // holds it bare, so match on both forms.
  return t.includes('.') ? t.split('.').pop()! : t
}

/**
 * Cached relation names across the agents this user can see, fetched once per
 * page and shared. The report payload's `data_sources` are serialized straight
 * off the ORM and don't carry this, so reading it from the agent list keeps the
 * badge working on every surface without a per-report query.
 */
export function useCachedTableNames() {
  const names = useState<Set<string>>('bow-cached-table-names', () => new Set())
  const loaded = useState<boolean>('bow-cached-table-names-loaded', () => false)

  async function ensureLoaded() {
    if (loaded.value) return
    loaded.value = true
    try {
      const { data, error } = await useMyFetch('/data_sources/active', { method: 'GET' })
      if (error.value || !Array.isArray(data.value)) return
      const next = new Set<string>()
      for (const ds of data.value as any[]) {
        for (const n of (ds?.cached_tables || [])) {
          if (typeof n === 'string' && n) {
            next.add(n.toLowerCase())
            next.add(normalize(n))
          }
        }
      }
      names.value = next
    } catch {
      /* badge is cosmetic — never let it break tool rendering */
    }
  }

  function isCachedTable(tableName?: string | null): boolean {
    if (!tableName || !names.value.size) return false
    const t = String(tableName).toLowerCase()
    return names.value.has(t) || names.value.has(normalize(t))
  }

  return { ensureLoaded, isCachedTable, names }
}

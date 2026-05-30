// Shared logic for "what should we call this agent's catalog count?"
//
// Three connection axes drive the answer:
//
//   1. data_shape (tables / files / objects / tools) — what noun to use.
//   2. catalog_ownership (shared / per_user / none) — together with the
//      user's auth state, decides whether the count is even meaningful.
//   3. auth_policy + user_status — when an attached connection is
//      user_required and the current user hasn't signed in, the catalog
//      count is misleading (it's always 0 because no per-user enumeration
//      has happened yet) — suppress the count entirely.
//
// Returns { count, label, shouldShow }.
//   shouldShow=false means render nothing; the catalog is in an
//   indeterminate state and any number would lie.

interface ConnLike {
  type?: string
  table_count?: number
  auth_policy?: string
  user_status?: { has_user_credentials?: boolean }
}
interface AgentLike {
  connections?: ConnLike[]
  tables?: unknown[]
}

interface RegistryEntry {
  type: string
  data_shape?: string
  catalog_ownership?: string
}

export interface CatalogCount {
  count: number
  noun: { sing: string; plural: string }
  label: string         // pre-formatted "N files" / "N tables"
  shouldShow: boolean
}

function nounFor(shape: string | undefined): { sing: string; plural: string } {
  if (shape === 'files') return { sing: 'file', plural: 'files' }
  if (shape === 'objects') return { sing: 'collection', plural: 'collections' }
  if (shape === 'tools') return { sing: 'tool', plural: 'tools' }
  return { sing: 'table', plural: 'tables' }
}

export function useCatalogCount() {
  // Registry index by type. Callers can pass it in pre-fetched (saves a
  // per-card fetch on list pages); if not provided we look at the agent's
  // connections and rely on caller-side mapping.
  function computeFromAgent(
    ds: AgentLike | null | undefined,
    registryByType: Record<string, RegistryEntry> = {},
  ): CatalogCount {
    const connections = ds?.connections || []

    // Suppress the count entirely if any attached connection is
    // user_required AND the current user hasn't signed in yet — the count
    // is always 0 by design and showing "0 tables" reads as broken.
    const hasPendingSignIn = connections.some(
      (c) => c.auth_policy === 'user_required' && !c.user_status?.has_user_credentials
    )

    // Pick a single shape across attached connections. If they all share a
    // shape, use it; mixed → fall back to tables (SQL-style is the default).
    const shapes = new Set(
      connections
        .map((c) => registryByType[c.type || '']?.data_shape)
        .filter(Boolean)
    )
    const shape = shapes.size === 1 ? (Array.from(shapes)[0] as string) : 'tables'
    const noun = nounFor(shape)

    const count = connections.length > 0
      ? connections.reduce((sum, c) => sum + (c.table_count || 0), 0)
      : ds?.tables?.length || 0

    const label = `${count} ${count === 1 ? noun.sing : noun.plural}`

    return {
      count,
      noun,
      label,
      shouldShow: !hasPendingSignIn,
    }
  }

  return { computeFromAgent }
}

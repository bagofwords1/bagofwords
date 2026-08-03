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

export function nounFor(shape: string | undefined): { sing: string; plural: string } {
  if (shape === 'files') return { sing: 'file', plural: 'files' }
  if (shape === 'objects') return { sing: 'collection', plural: 'collections' }
  if (shape === 'tools') return { sing: 'tool', plural: 'tools' }
  return { sing: 'table', plural: 'tables' }
}

/**
 * "11 files" / "3 tools" / "12 tables" for a SINGLE connection.
 *
 * The noun follows the registry `data_shape` the /connections payload already
 * carries — a files-shaped connection (network_dir, SharePoint, S3…) keeps its
 * catalog entries in the same `connection_tables` rows a database does, so
 * `table_count` is really "catalog entries" and only the noun differs. Branching
 * on a hardcoded type list instead got every non-database connection labelled
 * "N tables".
 */
export function connectionCatalogLabel(conn: {
  data_shape?: string
  table_count?: number
  tool_count?: number
} | null | undefined): string {
  const shape = conn?.data_shape || 'tables'
  const noun = nounFor(shape)
  const n = (shape === 'tools' ? conn?.tool_count : conn?.table_count) || 0
  return `${n} ${n === 1 ? noun.sing : noun.plural}`
}

// ── Knowledge kinds ────────────────────────────────────────────────────────
// Which knowledge categories an agent built from a given set of connections
// gets. These are the tabs AgentKnowledgeTabs renders, and the wizard's step-2
// label is named after them, so both derive from here rather than guessing.

export type KnowledgeKind = 'tables' | 'files' | 'tools'

const KIND_LABELS: Record<KnowledgeKind, string> = {
  tables: 'Tables',
  files: 'Files',
  tools: 'Tools',
}

export function knowledgeKindLabel(kind: KnowledgeKind): string {
  return KIND_LABELS[kind]
}

/**
 * Knowledge kinds for a set of connection data_shapes, in tab order.
 *
 * `objects` (Drive-style collections) is selected through the same table
 * picker, so it folds into `tables`. Files is unconditional: an agent can hold
 * uploaded files of its own regardless of what it connects to.
 */
export function knowledgeKindsFor(shapes: Array<string | undefined>): KnowledgeKind[] {
  const kinds: KnowledgeKind[] = []
  if (shapes.some((s) => s === 'tables' || s === 'objects')) kinds.push('tables')
  kinds.push('files')
  if (shapes.some((s) => s === 'tools')) kinds.push('tools')
  return kinds
}

/**
 * Wizard step-2 label: "Select Files", "Select Tables & Files",
 * "Select Tables, Files & Tools". Falls back to a neutral label when the
 * connections aren't known yet (step 1, nothing picked).
 */
export function knowledgeStepLabel(kinds: KnowledgeKind[]): string {
  const names = kinds.map(knowledgeKindLabel)
  if (!names.length) return 'Configure Knowledge'
  const last = names[names.length - 1]
  const rest = names.slice(0, -1)
  return `Select ${rest.length ? `${rest.join(', ')} & ${last}` : last}`
}

const KIND_HINTS: Record<KnowledgeKind, string> = {
  tables: 'pick tables for databases',
  files: 'review the file scope for directories',
  tools: 'enable the tools this agent can call',
}

/** One-line blurb under the step-2 heading, describing only the tabs present. */
export function knowledgeStepHint(kinds: KnowledgeKind[]): string {
  const parts = kinds.map((k) => KIND_HINTS[k]).filter(Boolean)
  if (!parts.length) return ''
  const joined = parts.length > 1
    ? `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`
    : parts[0]
  const sentence = joined.charAt(0).toUpperCase() + joined.slice(1)
  return parts.length > 1 ? `${sentence} — each source its own way.` : `${sentence}.`
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

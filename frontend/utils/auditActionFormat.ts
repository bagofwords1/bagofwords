// Audit log row formatting: action split, verb colour, actor kind, and the
// in-app link for a resource. Pure functions so they are unit-testable
// (tests/unit/auditActionFormat.mjs) and shared by the list and the drawer.

export type AuditActorKind = 'user' | 'agent' | 'system'

export type AuditLike = {
  action: string
  user_id?: string | null
  resource_type?: string | null
  resource_id?: string | null
  details?: Record<string, any> | null
}

const humanize = (s: string) => s.replace(/_/g, ' ')

/**
 * Split "artifact.record.created" into the resource path (["artifact",
 * "record"]) and the verb ("created"). The verb is always the LAST segment,
 * whatever the number of segments; an action without dots is all verb.
 */
export function splitAction(action: string): { path: string[]; verb: string } {
  const parts = String(action || '').split('.').filter(Boolean)
  if (parts.length === 0) return { path: [], verb: '' }
  return { path: parts.slice(0, -1).map(humanize), verb: humanize(parts[parts.length - 1]) }
}

const VERB_CLASSES: Array<[RegExp, string]> = [
  [/^(created|registered|installed|uploaded|granted|assigned|activated|imported|added|approved|promoted|demo installed)$/, 'bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-300'],
  [/^(deleted|removed|revoked|archived|bulk archived|access revoked|failed|login failed|denied|blocked|.* blocked.*|.*failed.*|disabled|paused)$/, 'bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300'],
  [/^(published|exported|shared|share toggled|scheduled|triggered|sent|notification sent)$/, 'bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300'],
  [/^(invited|invite resent|role changed|member upserted)$/, 'bg-purple-50 text-purple-700 dark:bg-purple-950 dark:text-purple-300'],
]
const NEUTRAL = 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300'

/** Chip classes keyed by the action's final verb. */
export function verbClass(action: string): string {
  const { verb } = splitAction(action)
  for (const [re, cls] of VERB_CLASSES) if (re.test(verb)) return cls
  return NEUTRAL
}

/** Same rule as the backend envelope's actor.type (streams/envelope.py). */
export function actorKind(log: AuditLike): AuditActorKind {
  if (log.details && typeof log.details === 'object' && log.details.agent_execution_id) return 'agent'
  if (log.user_id) return 'user'
  return 'system'
}

/** In-app route for the affected resource, when it has a page. */
export function resourceLink(log: AuditLike): string | null {
  const id = log.resource_id
  if (!id) return null
  switch (log.resource_type) {
    case 'report':
      return `/reports/${id}`
    case 'data_source':
      return `/agents/${id}`
    case 'instruction':
      return '/instructions'
    case 'membership':
    case 'user':
      return '/settings/members'
    case 'audit_log_stream':
      return '/settings/audit?tab=streams'
    default:
      return null
  }
}

/** Keys rendered as dedicated fields in the drawer; everything else stays in raw JSON. */
export const KNOWN_DETAIL_KEYS = [
  'title', 'tool', 'data_source', 'connection', 'queries', 'row_count', 'reason', 'error',
  'agent_execution_id', 'execution_mode', 'changed', 'state', 'destination', 'format',
] as const

/** Option shape for the audit filter dropdowns. */
export type FilterOption = { value: string; label: string; sub?: string; group?: string }

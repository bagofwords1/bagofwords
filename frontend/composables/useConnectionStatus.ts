/**
 * Derive a single effective status for a connection from:
 *   - `user_status.connection` — the cached test_connection result
 *   - `indexing.status` — the latest schema indexing run
 *
 * Keeps the UI state machine in one place so badge, dot, and banner render
 * identically across pages.
 */

export type ConnectionEffectiveStatus =
  | 'sign_in_required'
  | 'success'
  | 'indexing'
  | 'indexing_failed'
  | 'error'
  | 'unknown'

export interface ConnectionIndexing {
  id: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | string
  // 'org' = the shared-catalog run; 'user' = the viewer's own per-user catalog
  // sync (OneDrive / personal Drive), which the list endpoint prefers when both
  // exist so the card tracks the run that is actually doing work.
  scope?: 'org' | 'user' | string
  phase?: string | null
  current_item?: string | null
  progress_done: number
  progress_total: number
  started_at?: string | null
  finished_at?: string | null
  // When the source last reported progress. Unlike elapsed time, this tells a
  // slow-but-progressing run from a stuck one.
  last_activity_at?: string | null
  error?: string | null
  events?: Array<{ ts: string; level: string; phase?: string | null; message: string; done?: number; total?: number }>
  stats?: Record<string, any> | null
}

export function isIndexingActive(idx?: ConnectionIndexing | null): boolean {
  if (!idx) return false
  return idx.status === 'pending' || idx.status === 'running'
}

export function isIndexingFailed(idx?: ConnectionIndexing | null): boolean {
  return !!idx && idx.status === 'failed'
}

// Shared predicate for sign-in actions; service-account fallback already has access.
export function needsConnectionSignIn(conn: any): boolean {
  return conn?.auth_policy === 'user_required'
    && !conn?.user_status?.has_user_credentials
    && conn?.user_status?.effective_auth !== 'system'
}

export function getEffectiveStatus(conn: any): ConnectionEffectiveStatus {
  // The API explicitly distinguishes missing personal access from a failed
  // test. Do not let shared health/indexing imply this viewer is connected.
  const user = conn?.user_status
  if (user?.effective_auth === 'none' && user?.has_user_credentials === false
      && ['offline', 'not_connected', 'unknown', ''].includes(String(user.connection || '').toLowerCase())) {
    return 'sign_in_required'
  }
  const idx = conn?.indexing as ConnectionIndexing | undefined
  if (isIndexingActive(idx)) return 'indexing'

  const testStatus = String(conn?.user_status?.connection || conn?.last_connection_status || conn?.last_status || conn?.status || '').toLowerCase()
  if (testStatus === 'success') {
    // Test OK; check whether the most recent indexing failed.
    if (isIndexingFailed(idx)) return 'indexing_failed'
    return 'success'
  }
  if (testStatus === 'not_connected' || testStatus === 'offline' || testStatus === 'error') return 'error'

  if (isIndexingFailed(idx)) return 'indexing_failed'

  return 'unknown'
}

export function hasAnyActiveIndexing(connections?: any[] | null): boolean {
  if (!connections?.length) return false
  return connections.some((c) => isIndexingActive(c?.indexing))
}

export function statusDotClass(status: ConnectionEffectiveStatus): string {
  switch (status) {
    case 'success':
      return 'bg-green-500'
    case 'indexing':
      return 'bg-blue-500 animate-pulse'
    case 'indexing_failed':
      return 'bg-amber-500'
    case 'error':
      return 'bg-red-500'
    default:
      return 'bg-gray-400'
  }
}

export function statusBadgeClass(status: ConnectionEffectiveStatus): string {
  switch (status) {
    case 'success':
      return 'bg-green-50 text-green-700 border-green-200'
    case 'indexing':
      return 'bg-blue-50 text-blue-700 border-blue-200'
    case 'indexing_failed':
      return 'bg-amber-50 text-amber-700 border-amber-200'
    case 'error':
      return 'bg-red-50 text-red-700 border-red-200'
    default:
      return 'bg-gray-50 text-gray-700 border-gray-200'
  }
}

export function statusLabel(status: ConnectionEffectiveStatus): string {
  switch (status) {
    case 'sign_in_required':
      return 'Sign in required'
    case 'success':
      return 'Connected'
    case 'indexing':
      return 'Indexing'
    case 'indexing_failed':
      return 'Indexing failed'
    case 'error':
      return 'Not connected'
    default:
      return 'Unknown'
  }
}

// i18n variant: returns the message key so callers can render the label in
// the user's locale via $t(). statusLabel above stays for legacy callers.
export function statusLabelKey(status: ConnectionEffectiveStatus): string {
  switch (status) {
    case 'sign_in_required':
      return 'data.signInRequired'
    case 'success':
      return 'data.connected'
    case 'indexing':
      return 'data.indexing'
    case 'indexing_failed':
      return 'data.indexingFailed'
    case 'error':
      return 'data.notConnected'
    default:
      return 'data.unknown'
  }
}

export function indexingSummary(idx?: ConnectionIndexing | null): string {
  if (!idx) return ''
  const done = idx.progress_done || 0
  const total = idx.progress_total || 0
  const phase = idx.phase || ''
  if (total > 0) {
    if (idx.current_item) return `${phase || 'indexing'}: ${idx.current_item} (${done}/${total})`
    return `${phase || 'indexing'} ${done}/${total}`
  }
  if (phase) return phase
  return 'discovering…'
}

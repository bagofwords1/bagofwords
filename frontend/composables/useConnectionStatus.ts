/**
 * Derive a single effective status for a connection from:
 *   - `user_status.connection` — the cached test_connection result
 *   - `indexing.status` — the latest schema indexing run
 *
 * Keeps the UI state machine in one place so badge, dot, and banner render
 * identically across pages.
 */

export type ConnectionEffectiveStatus =
  | 'success'
  | 'indexing'
  | 'indexing_failed'
  | 'error'
  | 'unknown'

export interface ConnectionIndexing {
  id: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | string
  phase?: string | null
  current_item?: string | null
  progress_done: number
  progress_total: number
  started_at?: string | null
  finished_at?: string | null
  error?: string | null
  stats?: Record<string, any> | null
}

export function isIndexingActive(idx?: ConnectionIndexing | null): boolean {
  if (!idx) return false
  return idx.status === 'pending' || idx.status === 'running'
}

export function isIndexingFailed(idx?: ConnectionIndexing | null): boolean {
  return !!idx && idx.status === 'failed'
}

export function getEffectiveStatus(conn: any): ConnectionEffectiveStatus {
  const idx = conn?.indexing as ConnectionIndexing | undefined
  if (isIndexingActive(idx)) return 'indexing'

  const testStatus = String(conn?.user_status?.connection || '').toLowerCase()
  if (testStatus === 'success') {
    // Test OK; check whether the most recent indexing failed.
    if (isIndexingFailed(idx)) return 'indexing_failed'
    return 'success'
  }
  if (testStatus === 'not_connected' || testStatus === 'offline') return 'error'

  // No cached test result. If credentials-required with creds present, treat
  // as success per existing behavior; indexing state still overrides.
  if (conn?.auth_policy === 'user_required' && conn?.user_status?.has_user_credentials) {
    if (isIndexingFailed(idx)) return 'indexing_failed'
    return 'success'
  }

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

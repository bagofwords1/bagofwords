// Which confirmation card a tool renders, and when it is live.
//
// The report page copies ANY `tool.confirmation` event onto
// tool_execution.confirmation. Durable confirmations (stream_user_confirmation)
// carry a `kind` and are answered through
// POST /completions/{id}/mcp_tool_confirmations/{confirmation_id}, which checks
// who answers. The legacy Create/Edit artifact card instead POSTs to the
// unauthenticated in-memory /api/artifacts/confirm route, so it must only
// render for legacy payloads.

export const APPROVAL_STAGES = ['awaiting_confirmation', 'awaiting_approval'] as const

// 'awaiting_approval' is the keepalive stage stream_user_confirmation emits
// every 15 s while it waits; the card must stay up through it.
export function isAwaitingApprovalStage(stage: unknown): boolean {
  return typeof stage === 'string' && (APPROVAL_STAGES as readonly string[]).includes(stage)
}

function asObject(value: unknown): Record<string, any> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, any>) : null
}

export function isLegacyArtifactConfirmation(confirmation: unknown): boolean {
  const c = asObject(confirmation)
  if (!c) return false
  return c.kind == null && !('storage_changes' in c)
}

export function isStorageChangeConfirmation(confirmation: unknown): boolean {
  const c = asObject(confirmation)
  return !!c && c.kind === 'builtin_tool' && Array.isArray(c.storage_changes)
}

const CHANGE_KEYS: Record<string, string> = {
  collection_removed: 'tools.storageChange.collectionRemoved',
  collection_readded: 'tools.storageChange.collectionReadded',
  field_removed: 'tools.storageChange.fieldRemoved',
  field_type_changed: 'tools.storageChange.fieldTypeChanged',
  scope_changed: 'tools.storageChange.scopeChanged',
  create_changed: 'tools.storageChange.createChanged',
  field_made_required: 'tools.storageChange.fieldMadeRequired',
}

export interface StorageChangeItem {
  key: string
  params: { collection: string; field: string; before: string; after: string }
  impact: { records: number; users: number }
}

// One i18n key + named params per change; unknown kinds are skipped.
export function storageChangeItems(confirmation: unknown): StorageChangeItem[] {
  const c = asObject(confirmation)
  const changes = c && Array.isArray(c.storage_changes) ? c.storage_changes : []
  const items: StorageChangeItem[] = []
  for (const raw of changes) {
    const change = asObject(raw)
    const key = change ? CHANGE_KEYS[String(change.kind)] : undefined
    if (!change || !key) continue
    items.push({
      key,
      params: {
        collection: String(change.collection ?? ''),
        field: String(change.field ?? ''),
        before: String(change.before ?? ''),
        after: String(change.after ?? ''),
      },
      impact: { records: Number(change.records) || 0, users: Number(change.users) || 0 },
    })
  }
  return items
}

export interface ApprovalOutcome {
  // The decision on record, or null when the POST did not record one.
  decision: boolean | null
  // 'expired' (410): the wait already ended, nothing to retry.
  // 'failed': anything else; the tool may still be waiting, so retry.
  error: 'failed' | 'expired' | null
}

// Interprets the useMyFetch result of the approval POST. The server's
// `approved` wins over the click: an already-resolved confirmation reports
// the first decision, which may differ.
export function approvalOutcome(res: unknown, requested: boolean): ApprovalOutcome {
  const r = asObject(res)
  if (!r) return { decision: null, error: 'failed' }
  const err = asObject(r.error)?.value
  if (err) {
    const status = err?.statusCode ?? err?.status ?? err?.response?.status
    return { decision: null, error: status === 410 ? 'expired' : 'failed' }
  }
  const data = asObject(asObject(r.data)?.value)
  const approved = data && typeof data.approved === 'boolean' ? data.approved : requested
  return { decision: approved, error: null }
}

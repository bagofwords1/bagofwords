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
  field_readded: 'tools.storageChange.fieldReadded',
  field_type_changed: 'tools.storageChange.fieldTypeChanged',
  field_made_required: 'tools.storageChange.fieldMadeRequired',
}

// access_changed: one outcome sentence per `principal.capability.after`
// (backend _artifact_storage._ACCESS_OUTCOMES). Anything else still renders,
// through the generic accessChanged line: an approval never hides a change.
const ACCESS_KEYS: Record<string, string> = {
  'public.read.owner': 'tools.storageChange.access.publicCanRead',
  'public.read.none': 'tools.storageChange.access.publicCannotRead',
  'member.create.yes': 'tools.storageChange.access.membersCanAdd',
  'member.create.no': 'tools.storageChange.access.membersCannotAdd',
  'member.modify_own.yes': 'tools.storageChange.access.membersCanEditOwn',
  'member.modify_own.no': 'tools.storageChange.access.membersCannotEditOwn',
  'member.modify_others.yes': 'tools.storageChange.access.membersCanEditOthers',
  'member.modify_others.no': 'tools.storageChange.access.membersCannotEditOthers',
  'member.read.all': 'tools.storageChange.access.membersSeeAll',
  'member.read.own': 'tools.storageChange.access.membersSeeOwn',
  'owner.read.all': 'tools.storageChange.access.ownerSeesAll',
  'owner.read.own': 'tools.storageChange.access.ownerSeesOwn',
  'owner.modify_others.yes': 'tools.storageChange.access.ownerCanEditOthers',
  'owner.modify_others.no': 'tools.storageChange.access.ownerCannotEditOthers',
}
const ACCESS_FALLBACK_KEY = 'tools.storageChange.accessChanged'

export interface StorageChangeItem {
  key: string
  params: {
    collection: string
    field: string
    before: string
    after: string
    principal: string
    capability: string
    records: number
    users: number
  }
  impact: { records: number; users: number }
}

function changeKey(change: Record<string, any>): string | undefined {
  if (change.kind === 'access_changed') {
    return ACCESS_KEYS[`${change.principal}.${change.capability}.${change.after}`] ?? ACCESS_FALLBACK_KEY
  }
  return CHANGE_KEYS[String(change.kind)]
}

// One i18n key + named params per change; unknown kinds are skipped.
export function storageChangeItems(confirmation: unknown): StorageChangeItem[] {
  const c = asObject(confirmation)
  const changes = c && Array.isArray(c.storage_changes) ? c.storage_changes : []
  const items: StorageChangeItem[] = []
  for (const raw of changes) {
    const change = asObject(raw)
    const key = change ? changeKey(change) : undefined
    if (!change || !key) continue
    const impact = { records: Number(change.records) || 0, users: Number(change.users) || 0 }
    items.push({
      key,
      params: {
        collection: String(change.collection ?? ''),
        field: String(change.field ?? ''),
        before: String(change.before ?? ''),
        after: String(change.after ?? ''),
        principal: String(change.principal ?? ''),
        capability: String(change.capability ?? ''),
        ...impact,
      },
      impact,
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

import assert from 'node:assert/strict'

import {
  approvalOutcome,
  isAwaitingApprovalStage,
  isLegacyArtifactConfirmation,
  isStorageChangeConfirmation,
  storageChangeItems,
} from '../../utils/toolConfirmation.ts'

// --- which confirmation card a Create/Edit artifact tool renders -------------
//
// The host copies ANY `tool.confirmation` event onto tool_execution.confirmation.
// The legacy card POSTs to the unauthenticated in-memory /api/artifacts/confirm
// route, so it must never render for a durable builtin confirmation: clicking
// its Approve would try to answer a destructive storage change through a route
// that does not check who is answering.

const storage = {
  kind: 'builtin_tool',
  confirmation_id: 'c1',
  tool_name: 'edit_artifact',
  timeout_seconds: 150,
  storage_changes: [{ kind: 'collection_removed', collection: 'notes', field: null, before: null, after: null, records: 3, users: 2 }],
  summary: "- Remove collection 'notes': affects 3 record(s) from 2 user(s)",
}

assert.equal(isLegacyArtifactConfirmation({ kind: 'builtin_tool' }), false)
assert.equal(isLegacyArtifactConfirmation(storage), false)
// Any other durable kind is not legacy either.
assert.equal(isLegacyArtifactConfirmation({ kind: 'mcp_tool_policy', confirmation_id: 'x' }), false)
// A payload carrying storage changes is never legacy, even without a kind.
assert.equal(isLegacyArtifactConfirmation({ confirmation_id: 'x', storage_changes: [] }), false)

// Legacy payloads: the old create/edit confirmation (title + visualizations).
assert.equal(isLegacyArtifactConfirmation({ confirmation_id: 'x', title: 'Sales', visualizations: [{ id: 'v', title: 'V' }] }), true)
assert.equal(isLegacyArtifactConfirmation({ confirmation_id: 'x' }), true)

// No confirmation at all.
assert.equal(isLegacyArtifactConfirmation(null), false)
assert.equal(isLegacyArtifactConfirmation(undefined), false)
assert.equal(isLegacyArtifactConfirmation('c1'), false)

assert.equal(isStorageChangeConfirmation(storage), true)
assert.equal(isStorageChangeConfirmation({ kind: 'builtin_tool' }), false)
assert.equal(isStorageChangeConfirmation({ confirmation_id: 'x', title: 'Sales' }), false)
assert.equal(isStorageChangeConfirmation(null), false)

// --- when the approval card is active -----------------------------------------
//
// stream_user_confirmation emits a keepalive tool.progress (stage
// 'awaiting_approval') every 15 s and the host copies the stage into
// progress_stage. A card gated only on 'awaiting_confirmation' would vanish
// after 15 s while the tool is still waiting for the answer.

assert.equal(isAwaitingApprovalStage('awaiting_confirmation'), true)
assert.equal(isAwaitingApprovalStage('awaiting_approval'), true)
assert.equal(isAwaitingApprovalStage('saving_artifact'), false)
assert.equal(isAwaitingApprovalStage(''), false)
assert.equal(isAwaitingApprovalStage(undefined), false)

// --- one localized line per change, with its impact ---------------------------

const P = (over = {}) => ({ collection: 'notes', field: '', before: '', after: '', principal: '', capability: '', records: 0, users: 0, ...over })

assert.deepEqual(storageChangeItems(storage), [{
  key: 'tools.storageChange.collectionRemoved',
  params: P({ records: 3, users: 2 }),
  impact: { records: 3, users: 2 },
}])

const everyKind = storageChangeItems({
  kind: 'builtin_tool',
  storage_changes: [
    { kind: 'field_removed', collection: 'notes', field: 'text', before: 'string', records: 0, users: 0 },
    { kind: 'field_type_changed', collection: 'notes', field: 'text', before: 'string', after: 'json', records: 1, users: 1 },
    { kind: 'field_readded', collection: 'notes', field: 'text', before: 'string', after: 'string', records: 4, users: 2 },
    { kind: 'field_made_required', collection: 'notes', field: 'text', records: 5, users: 2 },
  ],
})
assert.deepEqual(everyKind.map((i) => i.key), [
  'tools.storageChange.fieldRemoved',
  'tools.storageChange.fieldTypeChanged',
  'tools.storageChange.fieldReadded',
  'tools.storageChange.fieldMadeRequired',
])
assert.deepEqual(everyKind[1].params, P({ field: 'text', before: 'string', after: 'json', records: 1, users: 1 }))
assert.deepEqual(everyKind[3].impact, { records: 5, users: 2 })

// --- access changes: one outcome sentence per (principal, capability, after) ---

const access = (principal, capability, before, after, records = 0, users = 0) =>
  ({ kind: 'access_changed', collection: 'notes', principal, capability, before, after, records, users })
const accessKeys = storageChangeItems({
  storage_changes: [
    access('public', 'read', 'none', 'owner', 2, 1),
    access('public', 'read', 'owner', 'none'),
    access('member', 'create', 'yes', 'no'),
    access('member', 'create', 'no', 'yes'),
    access('member', 'modify_own', 'no', 'yes'),
    access('member', 'modify_own', 'yes', 'no'),
    access('member', 'modify_others', 'no', 'yes'),
    access('member', 'modify_others', 'yes', 'no'),
    access('member', 'read', 'own', 'all'),
    access('member', 'read', 'all', 'own'),
    access('owner', 'read', 'own', 'all'),
    access('owner', 'read', 'all', 'own'),
    access('owner', 'modify_others', 'no', 'yes'),
    access('owner', 'modify_others', 'yes', 'no'),
    // Not in the table (e.g. outsiders and anonymous differ): a generic line, never dropped.
    access('anonymous', 'create', 'no', 'yes'),
  ],
})
assert.deepEqual(accessKeys.map((i) => i.key), [
  'tools.storageChange.access.publicCanRead',
  'tools.storageChange.access.publicCannotRead',
  'tools.storageChange.access.membersCannotAdd',
  'tools.storageChange.access.membersCanAdd',
  'tools.storageChange.access.membersCanEditOwn',
  'tools.storageChange.access.membersCannotEditOwn',
  'tools.storageChange.access.membersCanEditOthers',
  'tools.storageChange.access.membersCannotEditOthers',
  'tools.storageChange.access.membersSeeAll',
  'tools.storageChange.access.membersSeeOwn',
  'tools.storageChange.access.ownerSeesAll',
  'tools.storageChange.access.ownerSeesOwn',
  'tools.storageChange.access.ownerCanEditOthers',
  'tools.storageChange.access.ownerCannotEditOthers',
  'tools.storageChange.accessChanged',
])
assert.deepEqual(accessKeys[0].params, P({ principal: 'public', capability: 'read', before: 'none', after: 'owner', records: 2, users: 1 }))

// Unknown kinds (a newer backend) are skipped rather than rendered as a raw key;
// malformed payloads yield nothing. Retired kinds are unknown now.
assert.deepEqual(storageChangeItems({ storage_changes: [{ kind: 'something_new', collection: 'x', records: 0, users: 0 }] }), [])
assert.deepEqual(storageChangeItems({ storage_changes: [{ kind: 'create_changed', collection: 'x', records: 0, users: 0 }] }), [])
assert.deepEqual(storageChangeItems({ kind: 'builtin_tool' }), [])
assert.deepEqual(storageChangeItems(null), [])

// A collection declared again over orphaned records has its own line.
assert.deepEqual(storageChangeItems({
  storage_changes: [{ kind: 'collection_readded', collection: 'notes', before: 'orphaned records', after: 'shared/owner', records: 3, users: 2 }],
}), [{
  key: 'tools.storageChange.collectionReadded',
  params: P({ before: 'orphaned records', after: 'shared/owner', records: 3, users: 2 }),
  impact: { records: 3, users: 2 },
}])

// --- every key the card can render exists in every locale catalog -------------

import fs from 'node:fs'
const LOCALES = ['en', 'es', 'he', 'fr', 'sv', 'ar', 'ru', 'de', 'pt', 'it']
const renderable = [...new Set([...everyKind, ...accessKeys].map((i) => i.key)),
  'tools.storageChange.collectionRemoved', 'tools.storageChange.collectionReadded', 'tools.storageChange.impact']
for (const loc of LOCALES) {
  const catalog = JSON.parse(fs.readFileSync(new URL(`../../../locales/${loc}.json`, import.meta.url), 'utf8'))
  for (const key of renderable) {
    const value = key.split('.').reduce((o, k) => (o == null ? undefined : o[k]), catalog)
    assert.equal(typeof value, 'string', `${loc}: ${key}`)
  }
  assert.equal(catalog.tools.storageChange.scopeChanged, undefined, `${loc}: retired scopeChanged`)
  assert.equal(catalog.tools.storageChange.createChanged, undefined, `${loc}: retired createChanged`)
}

// --- what the approval card shows after the POST -------------------------------
//
// A failed POST must be visible (not just console.error): the tool keeps
// waiting, so the user can retry. 410 = the wait already ended: nothing to
// retry. On success the server's `approved` wins (already_resolved reports the
// decision on record, which may differ from the click).

assert.deepEqual(approvalOutcome({ data: { value: { status: 'ok', approved: true } }, error: { value: null } }, true),
  { decision: true, error: null })
assert.deepEqual(approvalOutcome({ data: { value: { status: 'ok', approved: false, already_resolved: true } }, error: { value: null } }, true),
  { decision: false, error: null })
// No decision in the body: fall back to what was requested.
assert.deepEqual(approvalOutcome({ data: { value: { status: 'ok' } }, error: { value: null } }, false),
  { decision: false, error: null })
assert.deepEqual(approvalOutcome({ data: { value: null }, error: { value: { statusCode: 410 } } }, true),
  { decision: null, error: 'expired' })
assert.deepEqual(approvalOutcome({ data: { value: null }, error: { value: { statusCode: 500 } } }, true),
  { decision: null, error: 'failed' })
assert.deepEqual(approvalOutcome({ data: { value: null }, error: { value: new Error('network') } }, false),
  { decision: null, error: 'failed' })
assert.deepEqual(approvalOutcome(undefined, true), { decision: null, error: 'failed' })

console.log('toolConfirmation: ok')

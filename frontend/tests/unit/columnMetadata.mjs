import assert from 'node:assert/strict'
import {
  additiveReason, additiveState, columnEditsPayload, descriptionAfterSuggestion, descriptionToSave,
  driftOf, editsFromColumns, hasColumnMetadata, inheritedAdditive, inheritedDescription, isEdited,
  showsAdditivity, showsSource, shownAdditive, shownDescription, sourceLabel, undecidedColumns, withEdit,
} from '../../utils/columnMetadata.ts'

// Columns as the backend stores them on a cached table.
const copied = {
  name: 'region', dtype: 'string', description: 'Sales region',
  metadata: { source: { kind: 'column', table: 'public.stores', column: 'region' }, additive: false, additive_reason: 'grouping_key' },
}
const computed = {
  name: 'customers', dtype: 'int64', description: 'COUNT(DISTINCT o.customer_id)',
  metadata: {
    source: { kind: 'derived', expression: 'COUNT(DISTINCT o.customer_id)', reads: [{ table: 'public.orders', column: 'customer_id' }] },
    additive: false, additive_reason: 'distinct_count',
  },
}
const measure = {
  name: 'Total Sales', dtype: 'double', description: 'SUM(Sales[Amount])',
  metadata: { role: 'measure', source: { kind: 'measure', table: 'Retail/Sales', column: 'Total Sales' }, additive: true, additive_reason: 'sum' },
}
const unknown = { name: 'score', dtype: 'double', metadata: { additive: 'unknown', additive_reason: 'complex' } }
const bare = { name: 'legacy', dtype: 'int64' }
const edited = {
  name: 'revenue', dtype: 'double', description: 'Billed revenue',
  metadata: {
    additive: false, additive_reason: 'set_by_admin', edited: ['description', 'additive'],
    inherited: { description: 'SUM(o.amount)', additive: true, additive_reason: 'sum' },
    drift: { detected_at: '2026-01-02T03:04:05', previous_expression: 'SUM(amount)' },
  },
}

// --- reading a column -------------------------------------------------------
assert.equal(additiveState(measure), 'additive')
assert.equal(additiveState(computed), 'notAdditive')
assert.equal(additiveState(unknown), 'unknown')
assert.equal(additiveState(bare), null, 'a column that predates the metadata has no flag')
assert.equal(additiveState(null), null)
assert.equal(additiveReason(computed), 'distinct_count')
assert.equal(additiveReason(bare), null)
assert.equal(hasColumnMetadata(bare), false)
assert.equal(hasColumnMetadata(copied), true)

// The flag is shown where someone might add the numbers up, not on labels and keys.
assert.equal(showsAdditivity(computed), true)
assert.equal(showsAdditivity(measure), true)
assert.equal(showsAdditivity(unknown), true)
assert.equal(showsAdditivity(copied), false)
for (const reason of ['not_numeric', 'key']) {
  assert.equal(showsAdditivity({ name: 'x', metadata: { additive: false, additive_reason: reason } }), false, reason)
}
assert.equal(showsAdditivity(bare), false)

assert.deepEqual(sourceLabel(copied), { kind: 'column', text: 'public.stores.region', reads: [] })
assert.deepEqual(sourceLabel(measure), { kind: 'measure', text: 'Retail/Sales.Total Sales', reads: [] })
assert.deepEqual(sourceLabel(computed), { kind: 'derived', text: 'COUNT(DISTINCT o.customer_id)', reads: ['public.orders.customer_id'] })
assert.equal(sourceLabel(bare), null)
assert.equal(sourceLabel({ name: 'x', metadata: { source: { kind: 'column' } } }), null, 'half a source is no source')

// A computed column's description starts with its expression; the origin line
// is shown only when it would add something.
assert.equal(showsSource(computed, computed.description), false)
assert.equal(showsSource(computed, 'Customers who bought'), true)
assert.equal(showsSource(computed, ''), true)
assert.equal(showsSource(copied, copied.description), true)
assert.equal(showsSource(bare, 'x'), false)

assert.equal(isEdited(edited, 'description'), true)
assert.equal(isEdited(copied, 'description'), false)
assert.deepEqual(driftOf(edited), { detectedAt: '2026-01-02T03:04:05', previousExpression: 'SUM(amount)' })
assert.equal(driftOf(copied), null)

// What the source says survives an edit, so the edit can be undone.
assert.equal(inheritedDescription(edited), 'SUM(o.amount)')
assert.equal(inheritedDescription(copied), 'Sales region')
assert.equal(inheritedAdditive(edited), true)
assert.equal(inheritedAdditive(unknown), 'unknown')
assert.equal(inheritedAdditive(bare), null)

// --- editing ----------------------------------------------------------------
assert.deepEqual(editsFromColumns([copied, edited, bare]), { revenue: { description: 'Billed revenue', additive: false } })
assert.deepEqual(editsFromColumns(null), {})

let edits = withEdit({}, copied, 'description', 'Where the store is')
assert.deepEqual(edits, { region: { description: 'Where the store is' } })
assert.equal(shownDescription(edits, copied), 'Where the store is')
assert.equal(shownDescription({}, copied), 'Sales region')
// An emptied description is an edit too: the admin wants no description.
assert.deepEqual(withEdit({}, copied, 'description', ''), { region: { description: '' } })
// Typing the source's own words back is not an edit.
assert.deepEqual(withEdit(edits, copied, 'description', ' Sales region '), {})
// One field's edit leaves the other alone.
edits = withEdit(withEdit({}, unknown, 'additive', true), unknown, 'description', 'A score')
assert.deepEqual(edits, { score: { additive: true, description: 'A score' } })
assert.deepEqual(withEdit(edits, unknown, 'additive', 'unknown'), { score: { description: 'A score' } })
assert.equal(shownAdditive(edits, unknown), true)
assert.equal(shownAdditive({}, unknown), 'unknown')
assert.equal(shownAdditive({ customers: { additive: true } }, computed), true)
// The input is never mutated.
const frozen = Object.freeze({ region: Object.freeze({ description: 'a' }) })
assert.deepEqual(withEdit(frozen, copied, 'description', 'b'), { region: { description: 'b' } })
assert.deepEqual(frozen, { region: { description: 'a' } })
// Reverting a stored edit compares against the source's value, not the edit.
assert.deepEqual(withEdit(editsFromColumns([edited]), edited, 'description', 'SUM(o.amount)'), { revenue: { additive: false } })

// --- what is sent -----------------------------------------------------------
assert.deepEqual(
  columnEditsPayload([copied, computed, unknown], { region: { description: ' Where ' }, score: { additive: false }, gone: { additive: true } }, ['customers']),
  [{ name: 'region', description: 'Where' }, { name: 'customers', acknowledge_drift: true }, { name: 'score', additive: false }],
  'only columns the table has, only fields that were set',
)
assert.deepEqual(columnEditsPayload([copied], {}), [], 'no edits is an empty list, which clears stored ones')
assert.deepEqual(columnEditsPayload([unknown], { score: { additive: 'unknown' } }), [{ name: 'score', additive: 'unknown' }])

assert.deepEqual(undecidedColumns([copied, computed, unknown, bare]), ['score'])
assert.deepEqual(undecidedColumns([unknown], { score: { additive: false } }), [])
assert.deepEqual(undecidedColumns([measure], { 'Total Sales': { additive: 'unknown' } }), ['Total Sales'])

// --- the table description --------------------------------------------------
const generated = 'One row per region. Measures: revenue (additive).'
assert.equal(descriptionToSave(generated, generated), '', 'an untouched generated description stays generated')
assert.equal(descriptionToSave(` ${generated} `, generated), '')
assert.equal(descriptionToSave('Curated by finance', generated), 'Curated by finance')
assert.equal(descriptionToSave('', generated), '')
assert.equal(descriptionToSave('Mine', null), 'Mine')

assert.equal(descriptionAfterSuggestion('', null, generated), generated, 'fills an empty description')
assert.equal(descriptionAfterSuggestion('Mine', null, generated), 'Mine', 'never replaces what the admin wrote')
assert.equal(descriptionAfterSuggestion('old generated', 'old generated', generated), generated, 'a stale suggestion is replaced')
assert.equal(descriptionAfterSuggestion('old generated', 'old generated', null), '', 'and cleared when there is none')
assert.equal(descriptionAfterSuggestion('', null, null), '')

console.log('columnMetadata: ok')

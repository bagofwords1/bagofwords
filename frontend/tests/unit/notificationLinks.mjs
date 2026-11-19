import assert from 'node:assert/strict'

import { linkFor, actionsFor, displayTitle, displayBody, iconFor } from '../../utils/notifications.ts'

// A review notification must take the admin to where they act on it — never
// the generic agent overview (/agents/<id>), which says nothing about the
// change. Rows stored before the backend computed per-type links still carry
// that generic link, so the client derives the right one from the subject.

const AGENT = 'ds-1'
const ITEM = 'item-9'
const review = (type, subject = {}, link = `/agents/${AGENT}`) => ({
  source: 'review', type, title: 'x', link,
  subject: { kind: 'review_item', review_item_id: ITEM, review_type: type, data_source_id: AGENT, ...subject },
})

for (const [type, expected] of [
  ['schema_changed', `/agents/${AGENT}/tables`],
  ['slow_query', `/agents/${AGENT}/queries`],
  ['low_confidence', `/agents/${AGENT}/evals`],
  ['query_error', `/agents/${AGENT}/review/${ITEM}`],
]) {
  // Legacy row: the stored link is the generic overview.
  assert.equal(linkFor(review(type)), expected, `${type}: legacy row is re-routed`)
}

assert.equal(
  linkFor(review('instruction_suggestion', { instruction_id: 'ins-1' })),
  '/agents/instructions/ins-1',
  'a suggestion opens the instruction',
)

// A global suggestion (no agent) used to have no link at all.
const globalSuggestion = {
  source: 'review', type: 'instruction_suggestion', title: 'x', link: null,
  subject: { kind: 'review_item', review_item_id: ITEM, review_type: 'instruction_suggestion', data_source_id: null, instruction_id: 'ins-2' },
}
assert.equal(linkFor(globalSuggestion), '/agents/instructions/ins-2')
const globalOther = { ...globalSuggestion, subject: { ...globalSuggestion.subject, instruction_id: undefined } }
assert.equal(linkFor(globalOther), `/agents/review/${ITEM}`, 'a global item without a target opens the Review feed')

// Non-review notifications keep their own link untouched.
assert.equal(linkFor({ source: 'share', type: 'share_artifact', title: 'x', link: '/r/abc' }), '/r/abc')

// Actions: the primary button goes where the work is; the item in the Review
// feed is always reachable; a schema change also points at the checks it ran.
const schema = review('schema_changed', { reliability_check: { started_at: '2026-11-18T10:00:00' } })
const acts = actionsFor(schema)
assert.equal(acts[0].to, `/agents/${AGENT}/tables`)
assert.equal(acts[0].primary, true)
assert.ok(acts.some(a => a.to === `/agents/${AGENT}/evals`), 'schema change links its reliability check')
assert.ok(acts.some(a => a.to === `/agents/${AGENT}/review/${ITEM}`), 'item is reachable in the Review feed')
assert.equal(acts.filter(a => a.primary).length, 1, 'exactly one primary action')

// An item whose only destination is the feed gets that as its primary action.
const qe = actionsFor(review('query_error'))
assert.deepEqual(qe.map(a => [a.to, !!a.primary]), [[`/agents/${AGENT}/review/${ITEM}`, true]])

// Text: a title variant re-renders from the catalogue in the viewer's locale,
// falling back to the default title, then to the stored English.
const catalog = {
  'notifications.content.schema_changed.title': 'Schema changed',
  'notifications.content.schema_changed.title_named': '{agent} @ {connection}',
  'notifications.content.schema_changed.body_counts': '+{added} -{removed} ~{modified}',
}
const te = k => k in catalog
const t = (k, p = {}) => catalog[k].replace(/\{(\w+)\}/g, (_, x) => String(p[x]))
const named = { type: 'schema_changed', title: 'stored', subject: { i18n: { title_variant: 'named', agent: 'A', connection: 'C', variant: 'counts', added: 1, removed: 2, modified: 3 } } }
const strip = s => s.replace(/[\u2068\u2069]/g, '')
assert.equal(strip(displayTitle(named, t, te)), 'A @ C')
assert.equal(displayTitle(named, t, te), '\u2068A\u2069 @ \u2068C\u2069', 'names are bidi-isolated')
assert.equal(displayBody(named, t, te), '+1 -2 ~3')
const unknownVariant = { type: 'schema_changed', title: 'stored', subject: { i18n: { title_variant: 'nope' } } }
assert.equal(displayTitle(unknownVariant, t, te), 'Schema changed', 'unknown title variant falls back to the default title')
assert.equal(displayTitle({ type: 'other', title: 'stored', subject: {} }, t, te), 'stored', 'no params → stored text')
assert.equal(displayBody({ type: 'schema_changed', title: 's', body: 'stored body', subject: { i18n: { variant: 'custom' } } }, t, te), 'stored body')

// Icons: the types that used to fall back to the plain bell.
for (const type of ['connection_down', 'connection_restored', 'delivery_skipped', 'prompt_run_for']) {
  assert.notEqual(iconFor({ type, source: 'x', title: '' }), 'i-heroicons-bell', `${type} has its own icon`)
}
assert.notEqual(iconFor({ type: 'unknown', source: 'connection', title: '' }), 'i-heroicons-bell', 'connection source has an icon')

// "Handled" says how and by whom; unknown or missing reasons show nothing.
{
  const { handledText } = await import('../../utils/notifications.ts')
  const cat = { 'notifications.handled.marked': '{name} marked it as handled', 'notifications.handled.someone': 'Someone' }
  const tt = (k, p = {}) => cat[k].replace(/\{(\w+)\}/g, (_, x) => String(p[x]))
  const strip = s => s.replace(/[⁨⁩]/g, '')
  assert.equal(strip(handledText({ how: 'marked', by_name: 'Dana' }, tt)), 'Dana marked it as handled')
  assert.equal(handledText({ how: 'marked', by_name: null }, tt), 'Someone marked it as handled')
  assert.equal(handledText({ how: 'unknown_reason' }, tt), null)
  assert.equal(handledText(null, tt), null)
}

// "Improve in training mode" prefills a concrete prompt from the item: the
// schema diff, or the examples behind it; with none, a generic brief.
{
  const { trainingPrompt } = await import('../../utils/notifications.ts')
  const en = JSON.parse(await import('node:fs').then(fs => fs.readFileSync(new URL('../../../locales/en.json', import.meta.url), 'utf8')))
  const get = k => k.split('.').reduce((o, x) => o?.[x], en)
  const tt = (k, p = {}) => {
    const msg = get(k)
    assert.equal(typeof msg, 'string', `missing catalogue key ${k}`)
    return msg.replace(/\{(\w+)\}/g, (_, x) => String(p[x]))
  }
  const schema = trainingPrompt({
    type: 'schema_changed',
    subject: {
      connection: { name: 'Warehouse' },
      diff: {
        added_tables: ['refunds'], removed_tables: ['legacy_orders'],
        changed: [{ table: 'customers', added_cols: [{ name: 'tier', type: 'text' }], removed_cols: ['fax'], type_changes: [{ col: 'zip', from: 'int', to: 'text' }] }],
      },
    },
  }, tt)
  assert.match(schema, /Warehouse/)
  assert.match(schema, /New table: refunds/)
  assert.match(schema, /Removed table: legacy_orders/)
  assert.match(schema, /customers: added columns tier; removed columns fax; zip changed type from int to text/)

  const low = trainingPrompt({ type: 'low_confidence', examples: [{ question: 'Revenue by region?' }, { question: 'Top customers' }] }, tt)
  assert.match(low, /- "Revenue by region\?"\n- "Top customers"/)
  assert.match(trainingPrompt({ type: 'low_confidence', groupCount: 6, examples: [] }, tt), /^6 answers/)

  assert.match(trainingPrompt({ type: 'slow_query', examples: [{ question: 'All orders', seconds: 120 }] }, tt), /- "All orders" \(120s\)/)
  assert.match(trainingPrompt({ type: 'query_error', examples: [{ question: 'Churn', error: 'column x does not exist' }] }, tt), /- "Churn": column x does not exist/)
  for (const type of ['slow_query', 'query_error', 'other']) assert.ok(trainingPrompt({ type, examples: [] }, tt).length > 20, `${type} has a generic brief`)
  assert.match(trainingPrompt({ type: 'schema_changed', subject: { connection: { name: 'W' }, diff: {} } }, tt), /connection changed\./)
}
{
  const { trainingPrompt } = await import('../../utils/notifications.ts')
  const tt = (k) => k
  assert.equal(trainingPrompt({ type: 'schema_changed', subject: {} }, tt), 'reviewFeed.train.schemaUnnamed', 'a legacy item without a connection name gets the unnamed brief')
}
{
  const { trainingPrompt } = await import('../../utils/notifications.ts')
  const tt = (k, p = {}) => `${k}${p.n ?? ''}|${p.changes ?? ''}`
  const changed = Array.from({ length: 35 }, (_, i) => ({ table: `t${i}`, added_cols: ['c'], removed_cols: [], type_changes: [] }))
  const out = trainingPrompt({ type: 'schema_changed', subject: { connection: { name: 'W' }, diff: { changed } } }, tt)
  assert.equal(out.split('\n').length, 21, 'a huge diff is listed in part (20 lines + "and N more")')
  assert.match(out, /reviewFeed\.train\.more15/)
}

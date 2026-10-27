import assert from 'node:assert/strict'

import { promptMentionsToRefs } from '../../utils/mentions.ts'

// An instruction mention in a chat prompt must resolve to type 'instruction',
// not the 'entity' fallback — the fallback made InstructionText draw the
// table-cells glyph on an instruction chip.
const refs = promptMentionsToRefs([
  { name: 'INSTRUCTIONS', items: [{ id: 'i1', name: 'How to query mixpanel events' }] },
  { name: 'TABLES', items: [{ id: 't1', name: 'events', connection_name: 'Mixpanel', icon_type: 'bigquery' }] },
  { name: 'DATA SOURCES', items: [{ id: 'a1', name: 'Analytics', icon_token: 'tok' }] },
])

const byId = Object.fromEntries(refs.map(r => [r.id, r]))

assert.equal(byId.i1.type, 'instruction')
assert.equal(byId.i1.name, 'How to query mixpanel events')
// No data-source hint on an instruction: DataSourceIcon must not win over the
// cube in InstructionText's icon chain.
assert.equal(byId.i1.data_source_type, undefined)
assert.equal(byId.i1.data_source_icon_token, undefined)

// Unchanged behavior for the other groups.
assert.equal(byId.t1.type, 'datasource_table')
assert.equal(byId.t1.name, 'Mixpanel / events')
assert.equal(byId.t1.data_source_type, 'bigquery')
assert.equal(byId.a1.type, 'data_source')
assert.equal(byId.a1.data_source_icon_token, 'tok')

// An unknown group still falls back to 'entity'.
const [other] = promptMentionsToRefs([{ name: 'SOMETHING ELSE', items: [{ id: 'x', name: 'x' }] }])
assert.equal(other.type, 'entity')

console.log('promptMentionRefs: ok')

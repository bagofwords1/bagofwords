import assert from 'node:assert/strict'
import { resolveToolAgent } from '../../utils/toolAgentIcon.ts'

const search = { id: 'search', icon_token: 'type:elasticsearch' }
const api = { id: 'api', icon_token: 'emoji:🧭', connections: [{ id: 'conn', name: 'Catalog' }] }
const agents = [search, api]
const create = { arguments_json: { tables_by_source: [{ data_source_id: search.id }] } }
const describe = { result_json: { top_tables: [{ data_source_id: search.id }] } }
const mcp = { result_json: { connection_id: 'conn' } }

assert.equal(resolveToolAgent([create, describe], agents), search)
assert.equal(resolveToolAgent([mcp], agents), api)
assert.equal(resolveToolAgent([{ result_json: { via_agent: { id: api.id } } }], agents), api)
assert.equal(resolveToolAgent([create, mcp], agents), null)
assert.equal(resolveToolAgent([{}, create], agents), null)
assert.equal(resolveToolAgent([{}], [api]), null)
assert.equal(resolveToolAgent([mcp], [api, { ...api, id: 'another' }]), null)
assert.equal(resolveToolAgent([{ ...create, data_sources: [search] }], [api]), search)
console.log('Tool group agent attribution passed')

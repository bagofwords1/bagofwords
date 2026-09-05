import assert from 'node:assert/strict'
import { tableGraph, visibleTableIds, tableId, matchesTable } from '../../utils/tableGraph.ts'
const table = (id, name, connection_id = 'a', schema = 'public', fks = []) => ({ id, name, connection_id, metadata_json: { schema }, fks, is_active: false })
const fk = (target, col = 'foreign_id') => ({ references_name: target, column: { name: col }, references_column: { name: 'id' } })
const rows = [table('o', 'orders', 'a', 'public', [fk('customers'), fk('customers', 'billing_id'), fk('missing')]), table('c', 'customers'), table('other', 'customers', 'b'), table('i', 'items', 'a', 'public', [fk('orders'), fk('products')]), table('p', 'products'), table('s', 'staff', 'a', 'public', [fk('staff')])]
const graph = tableGraph(rows)
assert.deepEqual([...graph.neighbors.get('o')].sort(), ['c', 'i'])
assert.equal(graph.links.find(l => l.source === 'o').columns.length, 2)
assert.equal(graph.unresolved.get('o'), 1)
assert.deepEqual([...visibleTableIds(new Set(['o']), graph.neighbors, new Set())].sort(), ['c', 'i', 'o'])
assert.deepEqual([...visibleTableIds(new Set(['o', 'i']), graph.neighbors, new Set())].sort(), ['c', 'i', 'o', 'p'])
assert.deepEqual([...visibleTableIds(new Set(['o']), graph.neighbors, new Set(['p']))].sort(), ['c', 'i', 'o', 'p'])
assert.equal(graph.links.filter(l => l.source === 's' && l.target === 's').length, 1)
const collision = tableGraph([table('x', 'logs', 'a', 'public', [fk('customers')]), table('c1', 'customers'), table('c2', 'customers', 'a', 'billing')])
assert.equal(collision.links[0].target, 'c1')
assert.notEqual(tableId({...rows[0],id:undefined}), tableId({...rows[0],id:undefined,connection_id:'b'}))
assert.equal(matchesTable(rows[0], {search:'ord_rs', selected_state:'selected'}, true), true)
assert.equal(matchesTable(rows[0], {connection:['b']}), false)
console.log('PASS: graph identity, relationships, cycles, three states, and shared filters')

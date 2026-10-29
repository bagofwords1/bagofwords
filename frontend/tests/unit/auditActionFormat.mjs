import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { splitAction, verbClass, actorKind, resourceLink } from '../../utils/auditActionFormat.ts'

// The audit list once printed any action that was not exactly two segments in
// full ("artifact.record.created") inside a fixed-width chip, overflowing into
// the resource column, and coloured it by the SECOND segment ("record") rather
// than the verb. The invariant: the verb is the last segment and drives the
// colour, for every shape of action — including every one the backend emits.

const here = dirname(fileURLToPath(import.meta.url))
const backendApp = join(here, '..', '..', '..', 'backend', 'app')

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (name === '__pycache__') continue
    if (statSync(p).isDirectory()) walk(p, out)
    else if (p.endsWith('.py')) out.push(p)
  }
  return out
}

const emitted = new Set()
for (const f of walk(backendApp)) {
  for (const m of readFileSync(f, 'utf8').matchAll(/action\s*=\s*"([a-z_]+(?:\.[a-z_]+)+)"/g)) emitted.add(m[1])
}
assert.ok(emitted.size > 50, `expected to discover backend actions, found ${emitted.size}`)

const shapes = [
  'login',
  'report.updated',
  'artifact.record.created',
  'connection.custom_query.rls_changed',
  'a.b.c.deleted',
  'tool.web_fetch_blocked_unsafe_host',
  ...emitted,
]

for (const action of shapes) {
  const parts = action.split('.')
  const { path, verb } = splitAction(action)
  assert.equal(verb, parts[parts.length - 1].replace(/_/g, ' '), `verb of ${action}`)
  assert.equal(path.length, parts.length - 1, `path of ${action}`)
  assert.ok(!verb.includes('.'), `verb of ${action} has no dots`)
  assert.equal(verbClass(action), verbClass(`x.${parts[parts.length - 1]}`), `colour of ${action} follows its verb only`)
}

// Colour families follow the verb, never a middle segment.
assert.match(verbClass('artifact.record.created'), /green/)
assert.match(verbClass('connection.custom_query.deleted'), /red/)
assert.match(verbClass('api_key.revoked'), /red/)
assert.match(verbClass('report.published'), /blue/)
assert.match(verbClass('member.invited'), /purple/)
assert.match(verbClass('report.updated'), /gray/)
assert.equal(verbClass('artifact.created.updated'), verbClass('report.updated'), 'a middle "created" does not colour the chip')

// Actor kind mirrors backend envelope.actor_type.
assert.equal(actorKind({ action: 'x', user_id: 'u', details: { agent_execution_id: 'r' } }), 'agent')
assert.equal(actorKind({ action: 'x', user_id: 'u', details: {} }), 'user')
assert.equal(actorKind({ action: 'x', user_id: null, details: null }), 'system')

// Links only where a page exists, and only with an id.
assert.equal(resourceLink({ action: 'report.updated', resource_type: 'report', resource_id: 'r1' }), '/reports/r1')
assert.equal(resourceLink({ action: 'report.updated', resource_type: 'report', resource_id: null }), null)
assert.equal(resourceLink({ action: 'x.y', resource_type: 'artifact_resource', resource_id: 'a' }), null)

console.log(`audit action formatting holds for ${shapes.length} action shapes (${emitted.size} emitted by the backend)`)

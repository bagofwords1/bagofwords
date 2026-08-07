import assert from 'node:assert/strict'

import { resolveAgentAuto } from '../../utils/agentSelection.ts'

// Auto is a MODE — "every agent I can access", resolved per run by the backend
// against the running user. It is encoded as the absence of a pin, matching
// agent_focus_common.report_selection_is_auto. A pinned selection is a hard
// scope. The two are never inferred from each other's shape.

assert.equal(resolveAgentAuto({ selectedIds: [] }).isAuto, true, 'no pin is Auto')

// The regression this pins: selecting every agent by hand used to READ BACK as
// Auto, so a deliberate full-roster scope was indistinguishable from delegating
// the choice — and it froze today's roster, never picking up an agent added
// tomorrow.
assert.equal(
  resolveAgentAuto({ selectedIds: ['chinook', 'salesforce'] }).isAuto,
  false,
  'selecting every agent is a manual scope, not Auto',
)

// The customer-visible case: an org with exactly one agent. "That agent" and
// "whatever I can access" coincide today and diverge the moment a second agent
// exists, so picking it must not collapse into Auto.
assert.equal(
  resolveAgentAuto({ selectedIds: ['chinook'] }).isAuto,
  false,
  'the only agent in the org is still an explicit pin',
)

// A report created inside a project holds the project's default agents — the
// backend copies them at creation. That is an ordinary pin and must render as
// the named agents, not as a generic Auto bolt.
assert.equal(
  resolveAgentAuto({ selectedIds: ['project-default'] }).isAuto,
  false,
  "a project's defaults are a selection the report really carries",
)

// Defensive: callers pass through un-normalized state during load.
assert.equal(resolveAgentAuto({ selectedIds: undefined }).isAuto, true)

console.log('auto is the absence of a pin; every selection is a manual scope')

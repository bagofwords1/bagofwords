import assert from 'node:assert/strict'

import { resolveAgentAuto } from '../../utils/agentSelection.ts'

// --- a report created inside a project ---------------------------------------
//
// The regression this pins: the backend copies the project's default agents
// onto every new report created in the project, so the report opens holding
// exactly those agents. Treating that as "Auto" made the prompt box show a
// generic bolt and the blank-report picker highlight nothing — the project's
// agent was invisible in its own report.

const inProject = resolveAgentAuto({
  selectedIds: ['chinook'],
  visibleIds: ['chinook', 'salesforce', 'gsheets'],
  projectDefaultIds: ['chinook'],
})
assert.equal(inProject.isAuto, true, 'matching the project defaults is still the auto mode')
assert.equal(inProject.isWorkspaceAuto, false, "a project's defaults are a nameable selection")
assert.deepEqual(inProject.effectiveDefaultIds, ['chinook'])

// --- no project: auto means the whole roster ---------------------------------

const everything = resolveAgentAuto({
  selectedIds: ['chinook', 'salesforce'],
  visibleIds: ['chinook', 'salesforce'],
})
assert.equal(everything.isAuto, true)
assert.equal(everything.isWorkspaceAuto, true, 'every visible agent is the nameless kind of auto')

const handPicked = resolveAgentAuto({
  selectedIds: ['chinook'],
  visibleIds: ['chinook', 'salesforce'],
})
assert.equal(handPicked.isAuto, false)
assert.equal(handPicked.isWorkspaceAuto, false)

// --- partial / superset selections inside a project --------------------------

// Picking something else in a project is a real choice, not the defaults.
const divergedInProject = resolveAgentAuto({
  selectedIds: ['salesforce'],
  visibleIds: ['chinook', 'salesforce'],
  projectDefaultIds: ['chinook'],
})
assert.equal(divergedInProject.isAuto, false)
assert.equal(divergedInProject.isWorkspaceAuto, false)

// Defaults plus one more is a superset, not the defaults.
const supersetInProject = resolveAgentAuto({
  selectedIds: ['chinook', 'salesforce'],
  visibleIds: ['chinook', 'salesforce'],
  projectDefaultIds: ['chinook'],
})
assert.equal(supersetInProject.isAuto, false)
assert.equal(
  supersetInProject.isWorkspaceAuto,
  false,
  'inside a project, selecting everything must not fall back to workspace auto',
)

// Clearing the selection in a project is not auto — auto would re-apply the
// defaults, and the two must stay distinguishable.
const clearedInProject = resolveAgentAuto({
  selectedIds: [],
  visibleIds: ['chinook'],
  projectDefaultIds: ['chinook'],
})
assert.equal(clearedInProject.isAuto, false)

// --- a default the user cannot see -------------------------------------------
//
// Project defaults never widen access: the backend drops one the creator can't
// see, so the report never held it and it must not count toward the match.
const hiddenDefault = resolveAgentAuto({
  selectedIds: ['chinook'],
  visibleIds: ['chinook'],
  projectDefaultIds: ['chinook', 'private-warehouse'],
})
assert.deepEqual(hiddenDefault.effectiveDefaultIds, ['chinook'])
assert.equal(hiddenDefault.isAuto, true, 'an invisible default must not hold the match open')

// Every default invisible: falls back to workspace-wide auto semantics.
const allDefaultsHidden = resolveAgentAuto({
  selectedIds: ['chinook'],
  visibleIds: ['chinook'],
  projectDefaultIds: ['private-warehouse'],
})
assert.deepEqual(allDefaultsHidden.effectiveDefaultIds, [])
assert.equal(allDefaultsHidden.isWorkspaceAuto, true)

// --- empty roster ------------------------------------------------------------

const noAgents = resolveAgentAuto({ selectedIds: [], visibleIds: [] })
assert.equal(noAgents.isAuto, false, 'no agents at all is not "all agents"')
assert.equal(noAgents.isWorkspaceAuto, false)

console.log("project defaults read as a selection, workspace-wide auto stays nameless")

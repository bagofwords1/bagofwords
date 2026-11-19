import assert from 'node:assert/strict'

import { projectDefaultSelection } from '../../utils/agentSelection.ts'

// A new report opened inside a project starts with the project's default
// agents checked, instead of opening on Auto and leaving the user to guess
// what the report will run against.

const chinook = { id: 'chinook', name: 'Chinook' }
const sales = { id: 'sales', name: 'Sales' }
const hr = { id: 'hr', name: 'HR' }

assert.deepEqual(
  projectDefaultSelection(['chinook', 'sales'], [chinook, sales, hr]),
  [chinook, sales],
  'the project defaults become the starting selection',
)

// A default the user can't see is dropped, never pinned: a project default
// must not widen access.
assert.deepEqual(
  projectDefaultSelection(['chinook', 'secret'], [chinook, sales]),
  [chinook],
  'defaults the user cannot see are dropped',
)

// No defaults, or none visible, stays empty — which is Auto.
assert.deepEqual(projectDefaultSelection([], [chinook]), [], 'no defaults stays Auto')
assert.deepEqual(projectDefaultSelection(['secret'], [chinook]), [], 'no visible default stays Auto')

// Ids compare as strings (the project API and the agent list can disagree on
// number vs string), and a repeated id is pinned once.
assert.deepEqual(
  projectDefaultSelection([7, '7'], [{ id: '7', name: 'Seven' }]),
  [{ id: '7', name: 'Seven' }],
  'ids match as strings and are de-duplicated',
)

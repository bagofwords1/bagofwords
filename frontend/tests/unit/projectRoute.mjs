import assert from 'node:assert/strict'

import { projectIdFromPath } from '../../utils/projectRoute.ts'

// --- which routes count as "inside a project" --------------------------------
//
// This is what decides whether a new report inherits the project's default
// agents: the backend copies them only when the POST /reports body carries a
// project_id, so a route this helper misreads silently produces a report with
// the workspace-wide agent fallback instead.

assert.equal(
  projectIdFromPath('/projects/e80a5d30-b37d-4395-b024-df9b8b1dfc34'),
  'e80a5d30-b37d-4395-b024-df9b8b1dfc34',
)

// Query and hash belong to the page's own state (the settings view is
// ?view=settings), not to the id.
assert.equal(projectIdFromPath('/projects/abc?view=settings'), 'abc')
assert.equal(projectIdFromPath('/projects/abc#members'), 'abc')
assert.equal(projectIdFromPath('/projects/abc/'), 'abc')

// The project index is workspace-level — no project to inherit from.
assert.equal(projectIdFromPath('/projects'), null)
assert.equal(projectIdFromPath('/projects/'), null)

// Everything else is outside a project, including a report that lives in one:
// the sidebar's "New report" there starts a fresh workspace-level report.
assert.equal(projectIdFromPath('/reports/abc'), null)
assert.equal(projectIdFromPath('/'), null)
assert.equal(projectIdFromPath('/settings/general'), null)
assert.equal(projectIdFromPath(''), null)
assert.equal(projectIdFromPath(null), null)
assert.equal(projectIdFromPath(undefined), null)

// A path that only starts with the word "projects" is not a project route.
assert.equal(projectIdFromPath('/projects-archive/abc'), null)

console.log('project routes resolve to an id, everything else to null')

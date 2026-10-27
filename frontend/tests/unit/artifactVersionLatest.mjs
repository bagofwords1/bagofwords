import assert from 'node:assert/strict'

import { isNewestVersion, newestVersionOf } from '../../utils/artifactVersionLatest.ts'

// "Use this version" shows only on a version that is not the newest of its
// dashboard. The list is newest first, as the editor fetches it.

// Two dashboards in one conversation, one version each (found in manual
// testing): each is the newest of its own dashboard, whatever the other's age.
const twoDashboards = [
  { id: 'v-songs-1', artifact_id: 'songs', mode: 'page' },
  { id: 'v-singers-1', artifact_id: 'singers', mode: 'page' },
]
assert.equal(isNewestVersion(twoDashboards, 'v-singers-1'), true, 'the older dashboard is not an old version')
assert.equal(isNewestVersion(twoDashboards, 'v-songs-1'), true)

// Versions of one dashboard: only the newest is the newest.
const oneDashboard = [
  { id: 'v3', artifact_id: 'sales', mode: 'page' },
  { id: 'other-1', artifact_id: 'other', mode: 'page' },
  { id: 'v2', artifact_id: 'sales', mode: 'page' },
]
assert.equal(isNewestVersion(oneDashboard, 'v3'), true)
assert.equal(isNewestVersion(oneDashboard, 'v2'), false, 'an older version of the same dashboard offers the button')

// Versions from before versions were tied to their dashboard carry their own
// id as artifact_id: keep the rule by mode, so the button stays on them.
const legacy = [
  { id: 'old-2', artifact_id: 'old-2', mode: 'page' },
  { id: 'old-doc', artifact_id: 'old-doc', mode: 'doc' },
  { id: 'old-1', artifact_id: 'old-1', mode: 'page' },
]
assert.equal(isNewestVersion(legacy, 'old-1'), false, 'an older legacy page still offers the button')
assert.equal(isNewestVersion(legacy, 'old-2'), true)
assert.equal(isNewestVersion(legacy, 'old-doc'), true, 'a doc is not older than a newer page')

// Nothing selected, or not in the list: no button.
assert.equal(isNewestVersion(oneDashboard, null), true)
assert.equal(isNewestVersion(oneDashboard, 'missing'), true)

// Edit from a dashboard's draft opens the editor on THAT dashboard: its newest
// version, not the conversation's newest (found in manual testing).
assert.equal(newestVersionOf(twoDashboards, 'singers'), 'v-singers-1', 'the older dashboard, not the newest one')
assert.equal(newestVersionOf(oneDashboard, 'sales'), 'v3', 'its newest version')
assert.equal(newestVersionOf(oneDashboard, 'gone'), null, 'not in this conversation: no choice')
assert.equal(newestVersionOf(oneDashboard, null), null)

console.log('"Use this version" only on an older version of the same dashboard; Edit opens that dashboard')

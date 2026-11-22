import assert from 'node:assert/strict'

import {
  PALETTE_TABS, groupsForTab, fetchesForTab, actionsForTab, limitFor, stepTab,
} from '../../utils/commandPaletteTabs.ts'

const plain = { inReport: false, scoped: false }
const onReport = { inReport: true, scoped: false }
const scoped = { inReport: true, scoped: true }

// "All" leads and is the mixed list; the four categories follow in the agreed order.
assert.deepEqual(PALETTE_TABS, ['all', 'reports', 'agents', 'instructions', 'dashboards'])

// Every category is reachable from "All", and each focused tab shows its own group.
const allGroups = groupsForTab('all', plain)
for (const g of ['actions', 'reports', 'prompts', 'agents', 'instructions', 'dashboards']) {
  assert.ok(allGroups.includes(g), `All shows ${g}`)
}
assert.ok(groupsForTab('reports', plain).includes('reports'))
assert.deepEqual(groupsForTab('agents', plain), ['agents'])
assert.ok(groupsForTab('instructions', plain).includes('instructions'))
assert.deepEqual(groupsForTab('dashboards', plain), ['dashboards'])

// A focused tab never leaks another category's results.
for (const tab of ['agents', 'dashboards']) {
  for (const other of ['reports', 'prompts', 'instructions', 'actions']) {
    assert.ok(!groupsForTab(tab, plain).includes(other), `${tab} hides ${other}`)
  }
}

// "In this report" only exists on a report page, and only under All / Reports.
assert.ok(!groupsForTab('all', plain).includes('inReport'))
assert.equal(groupsForTab('all', onReport)[0], 'inReport')
assert.equal(groupsForTab('reports', onReport)[0], 'inReport')
for (const tab of ['agents', 'instructions', 'dashboards']) {
  assert.ok(!groupsForTab(tab, onReport).includes('inReport'), `${tab} has no inReport`)
}
// An explicit `report:` scope wins over whichever tab is active.
for (const tab of PALETTE_TABS) assert.deepEqual(groupsForTab(tab, scoped), ['inReport'])

// Searches follow what is shown: a tab never queries a category it hides.
for (const tab of PALETTE_TABS) {
  for (const ctx of [plain, onReport, scoped]) {
    const shown = groupsForTab(tab, ctx)
    for (const f of fetchesForTab(tab, ctx)) assert.ok(shown.includes(f), `${tab} fetches only shown (${f})`)
  }
}
assert.deepEqual(fetchesForTab('agents', plain), [], 'agents filter client-side')
assert.deepEqual(fetchesForTab('dashboards', plain), ['dashboards'])
assert.deepEqual(fetchesForTab('reports', scoped), ['inReport'])

// Actions: both under All, only the fitting one elsewhere.
assert.deepEqual(actionsForTab('all'), ['newReport', 'newInstruction'])
assert.deepEqual(actionsForTab('reports'), ['newReport'])
assert.deepEqual(actionsForTab('instructions'), ['newInstruction'])
assert.deepEqual(actionsForTab('agents'), [])
assert.deepEqual(actionsForTab('dashboards'), [])

// A focused tab shows at least as many rows as All.
for (const g of ['reports', 'instructions', 'dashboards', 'agents']) {
  assert.ok(limitFor('reports', g) >= limitFor('all', g), g)
}

// Arrow keys walk the row and wrap; RTL mirrors the direction.
assert.equal(stepTab('all', 'ArrowRight'), 'reports')
assert.equal(stepTab('all', 'ArrowLeft'), 'dashboards')
assert.equal(stepTab('dashboards', 'ArrowRight'), 'all')
assert.equal(stepTab('all', 'ArrowLeft', true), 'reports')
assert.equal(stepTab('reports', 'ArrowRight', true), 'all')
for (const tab of PALETTE_TABS) {
  assert.equal(stepTab(stepTab(tab, 'ArrowRight'), 'ArrowLeft'), tab, 'right then left returns')
}

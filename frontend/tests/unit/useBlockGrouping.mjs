import assert from 'node:assert/strict'

import {
  computeBlockGroups,
  isGroupableBlock,
  MIN_GROUP_RUN,
} from '../../composables/useBlockGrouping.ts'

let id = 0
function chip(tool, extra = {}) {
  id += 1
  return {
    id: `b${id}`,
    status: 'completed',
    tool_execution: {
      tool_name: tool,
      status: 'success',
      duration_ms: 1000,
      arguments_json: { title: extra.title ?? `${tool} title` },
      ...extra.te,
    },
    ...extra.block,
  }
}

// --- isGroupableBlock -------------------------------------------------------

// research chips group
assert.equal(isGroupableBlock(chip('read_file')), true)
assert.equal(isGroupableBlock(chip('search_agents')), true)
assert.equal(isGroupableBlock(chip('edit_note')), true)

// deliverables / actions never group (not on the allowlist)
assert.equal(isGroupableBlock(chip('create_data')), false)
assert.equal(isGroupableBlock(chip('create_artifact')), false)
assert.equal(isGroupableBlock(chip('clarify')), false)
assert.equal(isGroupableBlock(chip('set_report_agents')), false)
assert.equal(isGroupableBlock(chip('create_note')), false)
assert.equal(isGroupableBlock(chip('route_model')), false)
// unknown tools default to visible
assert.equal(isGroupableBlock(chip('some_future_tool')), false)

// failures never group; in-flight blocks of groupable tools DO fold (the
// header becomes the live status line)
assert.equal(isGroupableBlock(chip('read_file', { te: { status: 'error' } })), false)
assert.equal(isGroupableBlock(chip('read_file', { block: { status: 'error' } })), false)
assert.equal(isGroupableBlock(chip('read_file', { block: { status: 'in_progress' }, te: { status: 'running' } })), true)
// final-answer blocks are content
assert.equal(
  isGroupableBlock(chip('read_file', { block: { plan_decision: { final_answer: 'done' } } })),
  false,
)

// --- computeBlockGroups -----------------------------------------------------

// a lone chip never groups; two consecutive chips DO (ticker grammar for
// every run of 2+)
{
  const single = computeBlockGroups([chip('read_file')])
  assert.deepEqual(single.groupOf, {})
  assert.deepEqual(single.headerAt, {})

  const pair = [chip('read_file'), chip('read_file')]
  const g = computeBlockGroups(pair)
  assert.equal(Object.keys(g.headerAt).length, 1)
  assert.equal(g.headerAt[pair[0].id].count, 2)
}

// a run of 3+ groups, and the header anchors at the first block
{
  const blocks = [chip('search_files'), chip('read_file'), chip('read_file')]
  const g = computeBlockGroups(blocks)
  assert.equal(Object.keys(g.headerAt).length, 1)
  const group = g.headerAt[blocks[0].id]
  assert.ok(group)
  assert.equal(group.count, 3)
  assert.equal(group.durationMs, 3000)
  assert.equal(group.blockIds.length, 3)
  // every member resolves to the same group
  for (const b of blocks) assert.equal(g.groupOf[b.id], group)
  // deterministic header material
  assert.match(group.verbSummary, /2 reads/)
  assert.match(group.verbSummary, /1 search/)
  assert.equal(group.lastTitle, 'read_file title')
  assert.deepEqual(group.toolNames, ['search_files', 'read_file'])
}

// a deliverable splits runs: 3 chips, create_data, 2 chips -> two groups
// (3 and 2); the deliverable itself never folds
{
  const a = [chip('read_file'), chip('read_file'), chip('read_file')]
  const mid = chip('create_data')
  const b = [chip('read_file'), chip('read_file')]
  const g = computeBlockGroups([...a, mid, ...b])
  assert.equal(Object.keys(g.headerAt).length, 2)
  assert.equal(g.groupOf[a[0].id].count, 3)
  assert.equal(g.groupOf[mid.id], undefined)
  assert.equal(g.groupOf[b[0].id].count, 2)
}

// a HANDLED error (run continued past it) is ABSORBED, counted as an issue
{
  const blocks = [
    chip('read_file'),
    chip('read_file'),
    chip('read_file', { te: { status: 'error', result_summary: 'does not support search_files' } }),
    chip('read_file'),
    chip('read_file'),
  ]
  const g = computeBlockGroups(blocks)
  assert.equal(Object.keys(g.headerAt).length, 1)
  const group = g.headerAt[blocks[0].id]
  assert.equal(group.count, 5)
  assert.equal(group.issueCount, 1)
  assert.equal(group.active, false) // a failed member is settled, not running
}

// an ACTIONABLE error (sign-in/auth) is never absorbed — it breaks the run
// into two groups with the CTA row visible between them
{
  const blocks = [
    chip('read_file'),
    chip('read_file'),
    chip('search_email', { te: { status: 'error', result_summary: "'Gmail' needs the user to sign in — its access token is missing or expired." } }),
    chip('read_file'),
    chip('read_file'),
  ]
  const g = computeBlockGroups(blocks)
  assert.equal(Object.keys(g.headerAt).length, 2)
  assert.equal(g.groupOf[blocks[2].id], undefined) // the CTA row stays visible
}

// a FATAL error (last block, nothing recovered) is never absorbed
{
  const blocks = [
    chip('read_file'),
    chip('read_file'),
    chip('read_file'),
    chip('read_file', { te: { status: 'error', result_summary: 'upstream exploded' } }),
  ]
  const g = computeBlockGroups(blocks)
  const group = g.headerAt[blocks[0].id]
  assert.equal(group.count, 3)
  assert.equal(group.issueCount, 0)
  assert.equal(g.groupOf[blocks[3].id], undefined)
}

// plural fix: "1 note update", not "1 note updat"
{
  const blocks = [chip('read_file'), chip('read_file'), chip('edit_note')]
  const g = computeBlockGroups(blocks)
  const group = g.headerAt[blocks[0].id]
  assert.match(group.verbSummary, /1 note update(?!s)/)
}

// duration completeness: any member missing duration_ms -> durationComplete false
{
  const withDur = [chip('read_file'), chip('read_file'), chip('read_file')]
  const g1 = computeBlockGroups(withDur)
  assert.equal(g1.headerAt[withDur[0].id].durationComplete, true)
  const missing = [chip('read_file'), chip('read_file'), chip('read_file', { te: { status: 'success', duration_ms: 0 } })]
  const g2 = computeBlockGroups(missing)
  assert.equal(g2.headerAt[missing[0].id].durationComplete, false)
}

// breakBefore forces a boundary (steering interleave): 2 + 2 -> two groups
// instead of one of four
{
  const blocks = [
    chip('read_file'),
    chip('read_file'),
    chip('read_file'),
    chip('read_file'),
  ]
  const steerBefore = blocks[2].id
  const g = computeBlockGroups(blocks, { breakBefore: (b) => b.id === steerBefore })
  assert.equal(Object.keys(g.headerAt).length, 2)
  assert.equal(g.headerAt[blocks[0].id].count, 2)
  assert.equal(g.headerAt[blocks[2].id].count, 2)
}

// an in-flight member makes the group active and supplies the running label
{
  const done = [chip('describe_tables'), chip('inspect_data')]
  const live = chip('inspect_data', { block: { status: 'in_progress' }, te: { status: 'running', duration_ms: 0, arguments_json: {} } })
  const g = computeBlockGroups([...done, live])
  const group = g.headerAt[done[0].id]
  assert.ok(group)
  assert.equal(group.count, 3)
  assert.equal(group.active, true)
  assert.equal(group.runningLabel, 'Inspecting data')
  // settled groups are inactive with no running label
  const g2 = computeBlockGroups([chip('read_file'), chip('read_file'), chip('read_file')])
  const group2 = g2.headerAt[Object.keys(g2.headerAt)[0]]
  assert.equal(group2.active, false)
  assert.equal(group2.runningLabel, '')
}

// sanity: policy constant is "two or more" (ticker grammar for every run)
assert.equal(MIN_GROUP_RUN, 2)

console.log('useBlockGrouping: all assertions passed')

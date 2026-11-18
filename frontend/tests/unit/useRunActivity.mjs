import assert from 'node:assert/strict'

import {
  computeRunActivity,
  describeDataOutput,
  findFinalBlock,
} from '../../composables/useRunActivity.ts'

// Run: node tests/unit/useRunActivity.mjs

let id = 0
const t = (s) => new Date(Date.UTC(2026, 0, 1, 0, 0, s)).toISOString()
function tool(name, args = {}, extra = {}) {
  id += 1
  return {
    id: `b${id}`,
    status: 'completed',
    started_at: t(id),
    completed_at: t(id + 1),
    tool_execution: { id: `te${id}`, tool_name: name, status: 'success', arguments_json: args, ...extra.te },
    ...extra.block,
  }
}
function prose(text, extra = {}) {
  id += 1
  return { id: `b${id}`, status: 'completed', started_at: t(id), completed_at: t(id + 1), content: text, ...extra }
}
const final = (text = '## Done') => prose(text, { plan_decision: { analysis_complete: true } })
const invoice = { tables_by_source: [{ data_source_id: 'ds', tables: ['Invoice'] }] }
const table = (title, rows) => tool('create_data', { title }, {
  te: { created_step_id: `s-${title}`, created_step: { title, data: { rows } }, result_json: { data_model: { type: 'table' } } },
})

// --- when a run folds --------------------------------------------------------

// Still working: the last block is a tool call, so there is nothing to fold.
assert.equal(computeRunActivity([prose('Checking'), tool('inspect_data', invoice)], { runStatus: 'in_progress' }), null)

// Prose that the planner has not marked final is a progress message, not a
// conclusion — it must not fold the run while it is still in progress.
assert.equal(findFinalBlock([tool('inspect_data', invoice), prose('Looking further')], 'in_progress'), null)

// The planner marks the conclusion before the run's status flips: fold then.
{
  const blocks = [tool('inspect_data', invoice), final()]
  assert.equal(computeRunActivity(blocks, { runStatus: 'in_progress' })?.finalBlockId, blocks[1].id)
}

// Stored rows carry no analysis_complete on old runs; a successful status is enough.
{
  const blocks = [tool('inspect_data', invoice), prose('All done')]
  assert.equal(computeRunActivity(blocks, { runStatus: 'success' })?.finalBlockId, blocks[1].id)
}

// A run that stopped or errored has no conclusion: its failure stays in view.
for (const runStatus of ['stopped', 'error']) {
  assert.equal(computeRunActivity([tool('inspect_data', invoice), prose('Partial')], { runStatus }), null)
}
assert.equal(computeRunActivity([tool('inspect_data', invoice), prose('boom', { status: 'error' })], { runStatus: 'success' }), null)

// A run that ended on a question keeps the question visible.
assert.equal(computeRunActivity([
  tool('inspect_data', invoice),
  tool('clarify', {}, { block: { content: 'Which year?' } }),
], { runStatus: 'success' }), null)

// A direct answer has no steps to view; reasoning alone still counts as activity.
assert.equal(computeRunActivity([final()], { runStatus: 'success' }), null)
assert.ok(computeRunActivity([{ ...final(), reasoning: 'thinking…' }], { runStatus: 'success' }))

// --- steps -------------------------------------------------------------------

// Three inspections of the same table are one step, each call a member.
{
  const blocks = [
    prose("I'll check the Invoice table."),
    tool('describe_tables', { query: ['Invoice'] }),
    tool('inspect_data', { user_prompt: 'row count', ...invoice }),
    tool('inspect_data', { user_prompt: 'nulls', ...invoice }),
    final(),
  ]
  const a = computeRunActivity(blocks, { runStatus: 'success' })
  assert.equal(a.steps.length, 1)
  const [step] = a.steps
  assert.equal(step.kind, 'inspect')
  assert.equal(step.labelKey, 'inspectedTable')
  assert.deepEqual(step.labelParams, { name: 'Invoice' })
  assert.equal(step.count, 3)
  assert.deepEqual(step.blockIds, blocks.slice(1, 4).map((b) => b.id))
  // The header anchors on the first member; every member maps to the step.
  assert.equal(a.headerAt[blocks[1].id], step)
  assert.equal(a.stepOf[blocks[3].id], step)
  // An inspection creates no artifact and is never a deliverable.
  assert.deepEqual(a.deliverableIds, [])
}

// A different table is a different purpose.
{
  const a = computeRunActivity([
    tool('inspect_data', invoice),
    tool('inspect_data', { tables_by_source: [{ data_source_id: 'ds', tables: ['Customer'] }] }),
    tool('inspect_data', { tables_by_source: [{ data_source_id: 'ds', tables: ['Customer', 'Invoice'] }] }),
    final(),
  ], { runStatus: 'success' })
  assert.deepEqual(a.steps.map((s) => [s.labelKey, s.labelParams.name ?? s.labelParams.count]), [
    ['inspectedTable', 'Invoice'], ['inspectedTable', 'Customer'], ['inspectedTables', 2],
  ])
}

// A pattern query is a search, not a named table; no tables at all is "the data".
{
  const a = computeRunActivity([
    tool('describe_tables', { query: 'inv.*' }),
    tool('inspect_data', { user_prompt: 'preview the upload' }),
    final(),
  ], { runStatus: 'success' })
  assert.deepEqual(a.steps.map((s) => s.labelKey), ['lookedForTables', 'inspectedData'])
}

// A progress message between two calls keeps its place: it splits the step.
{
  const a = computeRunActivity([
    tool('inspect_data', invoice),
    tool('inspect_data', invoice, { block: { content: 'One more check.' } }),
    final(),
  ], { runStatus: 'success' })
  assert.equal(a.steps.length, 2)
}

// Reasoning does not split a step; a steering bubble before a call does.
{
  const blocks = [
    tool('inspect_data', invoice),
    tool('inspect_data', invoice, { block: { reasoning: 'hmm' } }),
    tool('inspect_data', invoice),
    final(),
  ]
  assert.equal(computeRunActivity(blocks, { runStatus: 'success' }).steps.length, 1)
  assert.equal(computeRunActivity(blocks, { runStatus: 'success', breakBefore: (b) => b === blocks[2] }).steps.length, 2)
}

// Other tools merge by tool and prefer the model's own title for the label.
{
  const a = computeRunActivity([
    tool('search_files', { title: 'Looking for the budget file' }),
    tool('search_files', { title: 'Found the 2025 budget' }),
    tool('read_file', {}),
    final(),
  ], { runStatus: 'success' })
  assert.deepEqual(a.steps.map((s) => [s.count, s.labelText || s.labelKey]), [[2, 'Found the 2025 budget'], [1, 'read']])
}

// A failed member stays in its step and is counted on it.
{
  const a = computeRunActivity([
    tool('inspect_data', invoice, { te: { status: 'error' } }),
    tool('inspect_data', invoice),
    final(),
  ], { runStatus: 'success' })
  assert.equal(a.steps.length, 1)
  assert.equal(a.steps[0].issueCount, 1)
}

// --- create_data -------------------------------------------------------------

// Each create_data is its own step, described by what it actually produced.
{
  const rows = Array.from({ length: 24 }, (_, i) => ({ country: `c${i}`, revenue: i }))
  const blocks = [
    table('Revenue by country', rows),
    table('Total revenue', [{ total_revenue: 2328.6 }]),
    final(),
  ]
  const a = computeRunActivity(blocks, { runStatus: 'success' })
  assert.deepEqual(a.steps.map((s) => [s.kind, s.labelKey, s.output.kind]), [
    ['data', 'createdTable', 'table'], ['data', 'calculated', 'number'],
  ])
  assert.equal(a.steps[0].output.rowCount, 24)
  assert.equal(a.steps[1].output.value, (2328.6).toLocaleString(undefined, { maximumFractionDigits: 2 }))
  // Neither is appended under the answer or kept inline: a table and a number
  // fold into their steps and are reached through the answer's sources.
  assert.deepEqual(a.deliverableIds, [])
  assert.deepEqual(a.inlineIds, [])
}

// Output kind comes from the result, not from the requested view.
assert.equal(describeDataOutput(tool('create_data', { title: 'x' }, {
  te: { created_step: { data: { rows: [{ a: 1 }, { a: 2 }] } }, created_visualizations: [{ view: { view: { type: 'bar_chart' } } }] },
}).tool_execution).kind, 'chart')
assert.equal(describeDataOutput(tool('create_data', { title: 'x' }, {
  te: { created_step: { data: { rows: [{ a: 1, b: 2 }] } }, result_json: { data_model: { type: 'metric_card' } } },
}).tool_execution).kind, 'number')
assert.equal(describeDataOutput(tool('create_data', { title: 'x' }, { te: { status: 'error' } }).tool_execution).kind, 'none')
// A truncated preview reports the server's total, not the preview length.
assert.equal(describeDataOutput(tool('create_data', {}, {
  te: { created_step: { data: { rows: [{ a: 1, b: 1 }, { a: 2, b: 2 }], truncated: true, total_rows: 500 } } },
}).tool_execution).rowCount, 500)

// A failed create_data is a step but delivers nothing.
{
  const a = computeRunActivity([tool('create_data', { title: 'Churn' }, { te: { status: 'error' } }), final()], { runStatus: 'success' })
  assert.equal(a.steps[0].labelKey, 'dataFailed')
  assert.deepEqual(a.deliverableIds, [])
}

// A chart is never folded: it is not a step, it keeps its place, and the calls
// around it do not merge across it.
{
  const chart = (title) => tool('create_data', { title }, {
    te: { created_step: { title, data: { rows: [{ a: 1, b: 2 }, { a: 3, b: 4 }] } }, result_json: { data_model: { type: 'bar_chart' } } },
  })
  const blocks = [tool('inspect_data', invoice), chart('Revenue by country'), tool('inspect_data', invoice), final()]
  const a = computeRunActivity(blocks, { runStatus: 'success' })
  assert.deepEqual(a.inlineIds, [blocks[1].id])
  assert.equal(a.stepOf[blocks[1].id], undefined)
  assert.deepEqual(a.steps.map((s) => s.count), [1, 1])
  assert.deepEqual(a.deliverableIds, [])
  // A chart that failed to build is a step like any other failure.
  const failed = [tool('create_data', { title: 'x' }, { te: { status: 'error', result_json: { data_model: { type: 'bar_chart' } } } }), final()]
  assert.deepEqual(computeRunActivity(failed, { runStatus: 'success' }).inlineIds, [])
}

// --- deliverables ------------------------------------------------------------

// A re-edited doc or dashboard is one deliverable: the latest wins,
// in the position of its latest revision.
{
  const doc = (id) => tool('create_doc', {}, { te: { result_json: { doc_id: id } } })
  const blocks = [
    doc('d1'),
    tool('create_artifact', {}, { te: { result_json: { artifact_id: 'art1' } } }),
    tool('edit_doc', { doc_id: 'd1' }, { te: { result_json: { doc_id: 'd1' } } }),
    tool('edit_artifact', { artifact_id: 'art1' }, { te: { result_json: { artifact_id: 'art1' } } }),
    final(),
  ]
  const a = computeRunActivity(blocks, { runStatus: 'success' })
  assert.deepEqual(a.deliverableIds, [blocks[2].id, blocks[3].id])
}

// Research tools and unknown tools are activity, never promoted; blocks the
// page already hides behind another card are skipped.
{
  const blocks = [
    tool('read_file', {}),
    tool('some_future_tool', {}),
    tool('edit_instruction', {}),
    tool('edit_instruction', {}),
    final(),
  ]
  const a = computeRunActivity(blocks, { runStatus: 'success', isSuperseded: (b) => b === blocks[3] })
  assert.deepEqual(a.deliverableIds, [blocks[2].id])
}

// --- elapsed span ------------------------------------------------------------
{
  const blocks = [tool('inspect_data', invoice), tool('inspect_data', invoice), final()]
  const a = computeRunActivity(blocks, { runStatus: 'success' })
  assert.equal(a.spanMs, Date.parse(blocks[2].started_at) - Date.parse(blocks[0].started_at))
  // No timestamps: no invented duration.
  const bare = blocks.map(({ started_at, completed_at, ...b }) => b)
  assert.equal(computeRunActivity(bare, { runStatus: 'success' }).spanMs, null)
}

console.log('useRunActivity: all assertions passed')

// --- shared-link payload -------------------------------------------------------
// No stored step there: the result's preview alone must still say what was made.
{
  const preview = (rows, row_count) => tool('create_data', { title: 'T' }, {
    te: { result_json: { data_preview: { rows, row_count }, stats: { total_rows: row_count }, data_model: { type: 'table' } } },
  })
  const big = describeDataOutput(preview([{ a: 1, b: 2 }, { a: 3, b: 4 }], 24).tool_execution)
  assert.deepEqual([big.kind, big.rowCount], ['table', 24])
  assert.equal(describeDataOutput(preview([{ total: 7 }], 1).tool_execution).kind, 'number')
  const blocks = [preview([{ a: 1, b: 2 }, { a: 3, b: 4 }], 24), preview([{ total: 7 }], 1), final()]
  assert.deepEqual(computeRunActivity(blocks, { runStatus: 'success' }).steps.map((s) => s.labelKey), ['createdTable', 'calculated'])
}

console.log('useRunActivity: shared-link payload assertions passed')

// --- the answer shown while folded --------------------------------------------

// A run that writes its analysis and ends on a short deliverable call keeps the
// analysis on screen with it, not behind the fold.
{
  const analysis = prose('## Revenue\nThe USA leads with $523.')
  const build = tool('create_artifact', { title: 'Dashboard' }, { block: { content: 'Building the dashboard now…' } })
  const blocks = [tool('inspect_data', invoice), analysis, build]
  const fold = computeRunActivity(blocks, { runStatus: 'success' })
  assert.equal(fold?.finalBlockId, build.id)
  assert.deepEqual(fold?.answerIds, [analysis.id, build.id])
}
// A prose-only final block is the whole answer; earlier prose stays folded.
{
  const note = prose('Looking at invoices')
  const blocks = [note, tool('inspect_data', invoice), final()]
  assert.deepEqual(computeRunActivity(blocks, { runStatus: 'success' })?.answerIds, [blocks[2].id])
}

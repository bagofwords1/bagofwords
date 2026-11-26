import assert from 'node:assert/strict'

import {
  computeRunActivity,
  computeRunToolGroups,
  describeDataOutput,
  isPendingWait,
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

// Still working: everything folds behind a live row; nothing is the answer
// yet, and the row reads the latest progress note.
{
  const blocks = [prose('Checking the invoices'), tool('inspect_data', invoice)]
  const live = computeRunActivity(blocks, { runStatus: 'in_progress' })
  assert.equal(live?.live, true)
  assert.equal(live?.finalBlockId, '')
  assert.deepEqual(live?.answerIds, [])
  assert.equal(live?.status, 'Checking the invoices')
  assert.deepEqual(live?.steps.map((s) => s.kind), ['inspect'])
}

// A create_data card folds while live too, chart or table alike.
{
  const blocks = [tool('inspect_data', invoice), table('Revenue', [{ a: 1 }, { a: 2 }])]
  const live = computeRunActivity(blocks, { runStatus: 'in_progress' })
  assert.deepEqual(live?.answerIds, [])
  assert.equal(live?.stepOf[blocks[1].id]?.kind, 'data')
}

// Text being streamed with no tool call yet stays open below the row; as soon
// as its block starts a tool call it folds and becomes the row's status.
{
  const writing = prose('**Revenue** grew')
  const blocks = [tool('inspect_data', invoice), writing]
  const live = computeRunActivity(blocks, { runStatus: 'in_progress' })
  assert.deepEqual(live?.answerIds, [writing.id])
  assert.equal(live?.status, '')
  writing.tool_execution = { id: 'k', tool_name: 'create_data', status: 'running', arguments_json: { title: 'Revenue' } }
  const next = computeRunActivity(blocks, { runStatus: 'in_progress' })
  assert.deepEqual(next?.answerIds, [])
  assert.equal(next?.status, 'Revenue grew')
}

// A create_data still running reads as in progress, by its title.
{
  const running = tool('create_data', { title: 'Revenue' }, { te: { status: 'running' } })
  const step = computeRunActivity([tool('inspect_data', invoice), running], { runStatus: 'in_progress' })?.stepOf[running.id]
  assert.equal(step?.running, true)
  assert.equal(step?.labelKey, 'creatingData')
  assert.deepEqual(step?.labelParams, { title: 'Revenue' })
}

// The same block is the answer while streaming and once final: nothing moves
// when the run concludes.
{
  const answer = prose('## Done')
  const blocks = [tool('inspect_data', invoice), answer]
  assert.deepEqual(computeRunActivity(blocks, { runStatus: 'in_progress' })?.answerIds, [answer.id])
  answer.plan_decision = { analysis_complete: true }
  const done = computeRunActivity(blocks, { runStatus: 'in_progress' })
  assert.equal(done?.live, false)
  assert.deepEqual(done?.answerIds, [answer.id])
}

// A card waiting on the user stays open while live.
{
  const ask = tool('send_email', {}, { te: { status: 'running', progress_stage: 'awaiting_confirmation' } })
  const blocks = [tool('inspect_data', invoice), ask]
  const live = computeRunActivity(blocks, { runStatus: 'in_progress', keepOpen: (b) => b.id === ask.id })
  assert.deepEqual(live?.openIds, [ask.id])
}

// Running work with its own Stop (an eval, a sub-agent) is never hidden: open
// in place while live — run_eval is a deliverable, so it is its own row rather
// than a member of the search group — and kept under the answer once the run
// folds, after the outputs. Finished work folds as usual.
{
  const search = tool('search_evals')
  const evalRun = tool('run_eval', {}, { te: { status: 'running' } })
  const keepVisible = (b) => b.id === evalRun.id
  const live = computeRunActivity([search, evalRun], { runStatus: 'in_progress', keepVisible })
  assert.deepEqual(live?.openIds, [evalRun.id])
  assert.deepEqual(live?.toolGroups.map((g) => g.blockIds), [[search.id], [evalRun.id]])
  assert.deepEqual(live?.deliverableIds, [])

  const dash = tool('create_dashboard', {}, { te: { result_json: { artifact_id: 'a1' } } })
  const delegate = tool('delegate', { goal: 'why' }, { te: { result_json: { sub_agent_id: 'sa1' } } })
  const blocks = [dash, delegate, final()]
  const done = computeRunActivity(blocks, { runStatus: 'success', keepVisible: (b) => b.id === delegate.id })
  assert.deepEqual(done?.openIds, [])
  assert.deepEqual(done?.deliverableIds, [dash.id, delegate.id])
  assert.deepEqual(computeRunActivity(blocks, { runStatus: 'success', keepVisible: () => false })?.deliverableIds, [dash.id])
}

// A fan-out of several sub-agents: every one still running keeps its card —
// in place inside the merged live group, under the answer once folded — and
// the ones already finished fold like any other step.
{
  const fan = ['sa1', 'sa2', 'sa3'].map((sid) => tool('delegate', { goal: sid }, { te: { result_json: { sub_agent_id: sid } } }))
  const running = new Set(['sa1', 'sa2'])
  const keepVisible = (b) => running.has(b.tool_execution?.result_json?.sub_agent_id)
  const live = computeRunActivity([prose('Splitting this three ways'), ...fan], { runStatus: 'in_progress', keepVisible })
  assert.deepEqual(live?.toolGroups.map((g) => g.blockIds), [fan.map((b) => b.id)])
  assert.deepEqual(live?.openIds, [fan[0].id, fan[1].id])

  const done = computeRunActivity([prose('Splitting this three ways'), ...fan, final()], { runStatus: 'success', keepVisible })
  assert.deepEqual(done?.deliverableIds, [fan[0].id, fan[1].id])
  running.clear()
  assert.deepEqual(computeRunActivity([...fan, final()], { runStatus: 'success', keepVisible })?.deliverableIds, [])
}

// A live run keeps its row from the first block on, so a first progress note
// does not drop it and bring it back when its tool call starts. Once a lone
// answer is final there are no steps to view and the row goes.
{
  const first = prose('Hi')
  assert.equal(computeRunActivity([first], { runStatus: 'in_progress' })?.live, true)
  first.plan_decision = { analysis_complete: true }
  assert.equal(computeRunActivity([first], { runStatus: 'success' }), null)
}

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

// So does a run that ended asking the user to sign in to an agent: its Connect
// card must not fold into the steps — even as the run's only block.
for (const before of [[], [tool('search_agents', { query: ['sales'] })]]) {
  assert.equal(computeRunActivity([
    ...before,
    tool('request_agent_signin', { agent_ids: ['a1'] },
      { block: { content: 'Connect Sales below.', plan_decision: { analysis_complete: true } } }),
  ], { runStatus: 'success' }), null)
}

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

// A chart folds like any other result: it is a step, its calls do not merge
// with neighbours, and nothing is kept open or appended under the answer.
{
  const chart = (title) => tool('create_data', { title }, {
    te: { created_step: { title, data: { rows: [{ a: 1, b: 2 }, { a: 3, b: 4 }] } }, result_json: { data_model: { type: 'bar_chart' } } },
  })
  const blocks = [tool('inspect_data', invoice), chart('Revenue by country'), chart('Revenue by genre'), tool('inspect_data', invoice), final()]
  const a = computeRunActivity(blocks, { runStatus: 'success' })
  assert.deepEqual(a.steps.map((s) => [s.kind, s.labelKey]), [
    ['inspect', 'inspectedTable'], ['data', 'createdChart'], ['data', 'createdChart'], ['inspect', 'inspectedTable'],
  ])
  assert.equal(a.stepOf[blocks[1].id].output.kind, 'chart')
  assert.deepEqual(a.deliverableIds, [])
  assert.equal('inlineIds' in a, false)
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

// --- describe_entity: a saved query materialized is a data result ----------

// describe_entity in create mode materializes the saved query as its own step
// and visualization: it is a data step ("Created the table …"), named after the
// saved query, not an inspection. A plain lookup stays an inspection.
{
  const materialized = tool('describe_entity', { name_or_id: 'Tracks wide', should_create: true }, {
    te: {
      created_step_id: 's-tracks',
      result_json: { title: 'Tracks wide', data: { rows: [{ a: 1, b: 2 }, { a: 3, b: 4 }] }, created_visualization_ids: ['v-tracks'] },
    },
  })
  const lookup = tool('describe_entity', { name_or_id: 'Tracks wide' }, { te: { result_json: { title: 'Tracks wide' } } })
  const a = computeRunActivity([lookup, materialized, final()], { runStatus: 'success' })
  assert.deepEqual(a.steps.map((s) => [s.kind, s.labelKey, s.output?.kind ?? null]), [
    ['inspect', 'inspectedEntity', null],
    ['data', 'createdTable', 'table'],
  ])
  assert.equal(a.steps[1].labelParams.title, 'Tracks wide')
}

// A tool group counts failed calls even when the run recovered: the row's
// warning icon follows only the latest call, the count keeps earlier failures.
{
  const failedThenOk = [
    tool('inspect_data', {}, { te: { status: 'error', error_message: 'boom' } }),
    tool('inspect_data', {}, { te: { result_json: { success: false } } }),
    tool('inspect_data'),
  ]
  const [g] = computeRunToolGroups(failedThenOk)
  assert.equal(g.failedCount, 2)
  assert.equal(g.failed, false)
  const [clean] = computeRunToolGroups([tool('inspect_data'), tool('inspect_data')])
  assert.equal(clean.failedCount, 0)
  const [stillRunning] = computeRunToolGroups([tool('inspect_data', {}, { te: { status: 'running' } })], undefined, false)
  assert.equal(stillRunning.failedCount, 0)
  console.log('useRunActivity: tool-group failure count passed')
}

// Deliverables never fold into a live tool group: a dashboard, an instruction
// or a doc is its own row (a one-call group renders as a plain tool row), and
// the groupable calls around it keep their own groups on either side.
{
  const ids = (blocks) => computeRunToolGroups(blocks, undefined, false).map((g) => g.blockIds)

  const a = tool('inspect_data'), b = tool('create_data', { title: 'Sales' }), dash = tool('create_artifact')
  assert.deepEqual(ids([a, b, dash]), [[a.id, b.id], [dash.id]])

  const s = tool('search_instructions'), r = tool('read_instruction'), ed = tool('edit_instruction')
  const cr = tool('create_instruction'), after = tool('inspect_data')
  assert.deepEqual(ids([s, r, ed, cr, after]), [[s.id, r.id], [ed.id], [cr.id], [after.id]])

  const d1 = tool('create_data', { title: 'A' }), edit = tool('edit_artifact'), d2 = tool('create_data', { title: 'B' }), d3 = tool('inspect_data')
  assert.deepEqual(ids([d1, edit, d2, d3]), [[d1.id], [edit.id], [d2.id, d3.id]])

  // Every listed deliverable (and submit_*) stands alone; a plain read still groups.
  for (const name of ['create_doc', 'edit_doc', 'create_dashboard', 'create_scheduled_task', 'send_email', 'submit_review', 'execute_sql']) {
    const x = tool('read_file'), y = tool(name)
    assert.deepEqual(ids([x, y]), [[x.id], [y.id]], name)
  }
  const x = tool('read_file'), y = tool('grep_files')
  assert.deepEqual(ids([x, y]), [[x.id, y.id]])
  console.log('useRunActivity: deliverables stay out of tool groups passed')
}

// A wait still armed keeps its card (countdown + Cancel) in view: the page
// passes it as keepVisible, so a run that ends on it lists the card under the
// conclusion instead of folding it away. Cancelled, elapsed or failed waits
// fold like any other step.
{
  const now = Date.UTC(2026, 0, 1, 12, 0, 0)
  const iso = (ms) => new Date(ms).toISOString()
  const te = (rj) => ({ tool_name: 'wait', status: 'success', result_json: rj })
  assert.equal(isPendingWait(te({ status: 'scheduled', wake_at: iso(now + 60000) }), now), true)
  assert.equal(isPendingWait(te({ status: 'scheduled', wake_at: iso(now - 1000) }), now), false)
  assert.equal(isPendingWait(te({ status: 'cancelled', wake_at: iso(now + 60000) }), now), false)
  assert.equal(isPendingWait(te({ status: 'scheduled' }), now), true, 'no wake time yet: still armed')
  assert.equal(isPendingWait(te({}), now), false)
  assert.equal(isPendingWait({ tool_name: 'run_eval', result_json: { status: 'scheduled' } }, now), false)

  const data = tool('create_data', { title: 'Revenue' })
  const wait = tool('wait', { delay_minutes: 10 }, {
    te: { result_json: { status: 'scheduled', wake_at: iso(Date.now() + 600000) } },
    block: { content: 'Waiting 10 minutes, then I\'ll pick this back up automatically.', plan_decision: { analysis_complete: true } },
  })
  const keepVisible = (b) => isPendingWait(b.tool_execution)
  const done = computeRunActivity([data, wait], { runStatus: 'success', keepVisible })
  assert.equal(done?.finalBlockId, wait.id, 'the wait ends the turn with its message')
  assert.deepEqual(done?.deliverableIds, [wait.id])
  assert.deepEqual(computeRunActivity([data, wait], { runStatus: 'success', keepVisible: () => false })?.deliverableIds, [])
  console.log('useRunActivity: an armed wait stays in view passed')
}

// Approved or undone actions are deliverables too: their outcome stays under
// the conclusion, and they never join a live tool group.
{
  for (const name of ['delete_list_rows', 'set_report_agents', 'cancel_wait', 'cancel_scheduled_task', 'stop_eval_run', 'stop_sub_agent']) {
    const read = tool('read_file'), act = tool(name)
    assert.deepEqual(computeRunToolGroups([read, act], undefined, false).map((g) => g.blockIds), [[read.id], [act.id]], name)
    const done = computeRunActivity([read, act, final()], { runStatus: 'success' })
    assert.deepEqual(done?.deliverableIds, [act.id], name)
  }
  console.log('useRunActivity: approved and undone actions stay in view passed')
}

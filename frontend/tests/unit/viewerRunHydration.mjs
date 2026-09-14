import assert from 'node:assert/strict'

import {
  cardHydrationRequest,
  deriveViewerRun,
  stripIdentityParams,
} from '../../utils/viewerRunHydration.ts'

// The regression this pins (PR #1135 review, P1): a run_query card shows ONE
// parameter slice, but every refresh path used to fetch the query's DEFAULT
// step — which answers the saved values. A synthetic step id did not prevent
// it, because the preview derives its query id from `result_json.query_id`.
//
// It only bites past the preview cap: the report serializer ships 20 rows and
// marks the payload `truncated`, and each refresh trigger hydrates on that
// flag. So a run returning 21+ rows reverted to the saved snapshot on reload,
// on expand, and on export — while the header still named the requested
// params. Under 20 rows nothing refetches and the bug is invisible, which is
// exactly how it was missed.
//
// All three triggers (expand-after-reload, the default-step broadcast, and
// addToSpreadsheet) route through cardHydrationRequest, so these assertions
// cover all of them.

const SPECS = [
  { name: 'region', source: 'input' },
  { name: 'viewer_email', source: 'identity' },
  { name: 'owner', source: 'input_identity_default' },
]

// --- a viewer-run card re-requests ITS slice, never the default step --------

const run = cardHydrationRequest({
  viewerRun: { queryId: 'q1', params: { region: 'DE' } },
  queryId: 'q1',
  currentApplied: null,
  paramSpecs: SPECS,
})
assert.equal(run.method, 'POST')
assert.equal(run.url, '/api/queries/q1/run', 'must not fetch default_step')
assert.equal(run.body.mode, 'viewer', 'viewer mode: no new Step, per-viewer cache')
assert.deepEqual(run.body.params, { region: 'DE' })
assert.ok(!run.url.includes('default_step'))

// --- an ordinary card keeps the default-step path ---------------------------

const plain = cardHydrationRequest({
  viewerRun: null,
  queryId: 'q1',
  currentApplied: { region: 'US' },
  paramSpecs: SPECS,
})
assert.equal(plain.method, 'GET')
assert.equal(plain.url, '/api/queries/q1/default_step',
  'non-run cards must be untouched by this change')

// --- nothing to fetch -------------------------------------------------------

assert.equal(
  cardHydrationRequest({ viewerRun: null, queryId: null }), null,
  'callers keep their own fallbacks (a bare step id, or nothing)',
)
assert.equal(
  cardHydrationRequest({ viewerRun: { queryId: '', params: {} }, queryId: null }), null,
)

// --- what is on screen wins over the agent's original call ------------------

// After someone edits values in the card's own parameter bar, the card shows a
// different slice than the agent ran. A refresh must follow the screen.
const edited = cardHydrationRequest({
  viewerRun: { queryId: 'q1', params: { region: 'DE' } },
  queryId: 'q1',
  currentApplied: { region: 'FR' },
  paramSpecs: SPECS,
})
assert.deepEqual(edited.body.params, { region: 'FR' })

// An empty applied set is not a choice — fall back to the run's own values.
const emptyApplied = cardHydrationRequest({
  viewerRun: { queryId: 'q1', params: { region: 'DE' } },
  queryId: 'q1',
  currentApplied: {},
  paramSpecs: SPECS,
})
assert.deepEqual(emptyApplied.body.params, { region: 'DE' })

// --- identity values are never resubmitted ----------------------------------

// The server resolves identity params from the session and REJECTS any the
// client sends (resolve_param_values raises), so replaying a resolved value
// back would turn every refresh into an error.
const withIdentity = cardHydrationRequest({
  viewerRun: {
    queryId: 'q1',
    params: { region: 'DE', viewer_email: 'someone@example.com', owner: 'me@example.com' },
  },
  queryId: 'q1',
  currentApplied: null,
  paramSpecs: SPECS,
})
assert.deepEqual(
  withIdentity.body.params,
  { region: 'DE', owner: 'me@example.com' },
  'identity stripped; input_identity_default is an ordinary input and stays',
)

// --- stripIdentityParams on its own -----------------------------------------

assert.deepEqual(stripIdentityParams({ a: 1 }, null), { a: 1 })
assert.deepEqual(stripIdentityParams(null, SPECS), {})
assert.deepEqual(
  stripIdentityParams({ region: null }, SPECS), { region: null },
  'a null value is "all rows", not an absent param — it must survive',
)
assert.deepEqual(
  stripIdentityParams({ viewer_email: 'x' }, [{ name: 'viewer_email', source: 'identity' }]),
  {},
)
// Malformed spec rows must not throw or drop good params.
assert.deepEqual(
  stripIdentityParams({ region: 'DE' }, [null, {}, { source: 'identity' }]),
  { region: 'DE' },
)

// --- deriveViewerRun: the slice is read off the execution --------------------

// The second regression (PR #1135 review, follow-up): the fix above only
// worked where a prop was threaded in. "Open in panel" mounts a SECOND,
// fresh ToolWidgetPreview from stored panel state — openInPanel emitted only
// {toolExecution, title, visual}, panelData had no field for the run context,
// and the panel component was mounted without it. So the panel fell back to
// the default step: the same bug through a different door, and export from
// the panel with it.
//
// Deriving from the execution closes every mount point at once, including any
// added later, which is why it is not a prop.

const runExec = {
  id: 'te1',
  tool_name: 'run_query',
  result_json: {
    success: true,
    query_id: 'q1',
    applied_params: { region: 'DE' },
    data: { rows: [], columns: [] },
  },
}

assert.deepEqual(
  deriveViewerRun(runExec),
  { queryId: 'q1', params: { region: 'DE' } },
  'an inline run_query card describes its slice',
)

// The panel mounts the very same execution object out of panelData, with no
// prop: it must reach the identical conclusion.
const fromPanelState = { toolExecution: runExec, title: 'Sales by Region', visual: false, key: 'te1' }
assert.deepEqual(
  deriveViewerRun(fromPanelState.toolExecution),
  { queryId: 'q1', params: { region: 'DE' } },
  'the side panel resolves the same slice from stored state',
)
const panelReq = cardHydrationRequest({
  viewerRun: deriveViewerRun(fromPanelState.toolExecution),
  queryId: 'q1',
  currentApplied: null,
  paramSpecs: SPECS,
})
assert.equal(panelReq.url, '/api/queries/q1/run')
assert.ok(!panelReq.url.includes('default_step'),
  'the panel must not fetch the saved snapshot — it would replace the slice')

// --- tools whose default step IS their result stay on the old path ----------

for (const tool of ['create_data', 'describe_entity', 'read_query']) {
  assert.equal(
    deriveViewerRun({ tool_name: tool, result_json: { success: true, query_id: 'q1' } }),
    null,
    `${tool}: the default step is its result — fetching it is correct`,
  )
}

// --- nothing to derive ------------------------------------------------------

assert.equal(deriveViewerRun(null), null)
assert.equal(deriveViewerRun({}), null)
assert.equal(deriveViewerRun({ tool_name: 'run_query' }), null, 'no result_json yet (still running)')
assert.equal(
  deriveViewerRun({ tool_name: 'run_query', result_json: { success: false, error: 'boom', query_id: 'q1' } }),
  null,
  'a failed run has no slice to re-request',
)
assert.equal(
  deriveViewerRun({ tool_name: 'run_query', result_json: { success: true } }),
  null,
  'no query id, nothing to run',
)

// A defaults-only run still describes a slice: {} means "the saved defaults",
// which is exactly what the viewer-run endpoint should be asked for.
assert.deepEqual(
  deriveViewerRun({ tool_name: 'run_query', result_json: { success: true, query_id: 'q1' } }),
  { queryId: 'q1', params: {} },
)

console.log('viewerRunHydration: all assertions passed')

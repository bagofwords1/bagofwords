import assert from 'node:assert/strict'

import {
  cardHydrationRequest,
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

console.log('viewerRunHydration: all assertions passed')

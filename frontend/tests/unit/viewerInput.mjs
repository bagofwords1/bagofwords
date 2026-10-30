import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import ts from 'typescript'

import * as viewerInput from '../../utils/viewerInput.ts'

// Run: node --test tests/unit/viewerInput.mjs
// A published chart that waits for the viewer's input (its query needs a value
// only the app gives) carries viewer_result.needs_input. The page shows the app
// for it instead of the withheld-data gate, but that wait is not a run of the
// reader's own: it never counts as an own success (the export rule).

const { awaitsViewerInput, isOwnSuccess } = viewerInput
assert.equal(typeof awaitsViewerInput, 'function', 'awaitsViewerInput is exported')
assert.equal(awaitsViewerInput({ needs_input: ['customer_id'] }), true)
for (const vr of [undefined, null, {}, { needs_input: [] }, { needs_input: 'customer_id' }, { status: 'success' }]) {
  assert.equal(awaitsViewerInput(vr), false, JSON.stringify(vr))
}
console.log('PASS: only a non-empty needs_input list is a wait for the viewer')

assert.equal(isOwnSuccess({ status: 'success' }), true)
assert.equal(isOwnSuccess({ status: 'success', needs_input: [] }), true)
assert.equal(isOwnSuccess({ status: 'success', needs_input: ['customer_id'] }), false, 'a wait is not a run of theirs')
for (const vr of [{ status: 'error' }, undefined, null]) {
  assert.equal(isOwnSuccess(vr), false, JSON.stringify(vr))
}
console.log('PASS: a wait for input never counts as the reader\'s own successful run')

// ── the stored values a filter run starts from (finding 3) ─────────────────
// The served step of each query carries the values it ran with
// (applied_params). An input the viewer did not change must keep that stored
// value in the next filter run, or the run sends only what the viewer set and
// a second required input is missing (a 400). seedParamValues gives the
// starting values: the stored one, else the declared default, else nothing.
// Identity inputs are never seeded: the server resolves them for the viewer.
{
  const { seedParamValues } = viewerInput
  assert.equal(typeof seedParamValues, 'function', 'seedParamValues is exported')

  // T1.1: the stored value wins over the default; a null stored value falls
  // back to the default; an absent value with no default stays absent.
  const specs = { q1: [
    { name: 'region', source: 'input', default: 'US' },
    { name: 'year', source: 'input', default: 2025 },
    { name: 'segment', source: 'input' },
    { name: 'status', source: 'input', default: null },
  ] }
  const applied = { q1: { region: 'EU', year: null } }
  const frozenSpecs = JSON.stringify(specs)
  const frozenApplied = JSON.stringify(applied)
  const seeded = seedParamValues(specs, applied)
  assert.deepEqual(seeded, { region: 'EU', year: 2025 })
  assert.ok(!('segment' in seeded) && !('status' in seeded), 'no stored value and no default: absent')
  assert.equal(JSON.stringify(specs), frozenSpecs, 'the specs are not mutated')
  assert.equal(JSON.stringify(applied), frozenApplied, 'the stored values are not mutated')
  assert.deepEqual(seedParamValues({}, {}), {})
  assert.deepEqual(seedParamValues({ q1: [{ name: 'region', source: 'input', default: 'US' }] }, { q1: null }), { region: 'US' })
  assert.deepEqual(seedParamValues({ q1: [{ name: 'region', source: 'input' }] }, {}), {})
  console.log('PASS: a stored value seeds its input; else the default; else nothing')

  // T1.2: two queries share a name: the first non-null stored value wins, in
  // query order. E8: two required inputs, one stored: the stored one is there.
  assert.deepEqual(
    seedParamValues(
      { q1: [{ name: 'region', source: 'input' }], q2: [{ name: 'region', source: 'input', default: 'US' }] },
      { q1: { region: null }, q2: { region: 'APAC' } }),
    { region: 'APAC' })
  assert.deepEqual(
    seedParamValues(
      { q1: [{ name: 'region', source: 'input' }], q2: [{ name: 'region', source: 'input' }] },
      { q1: { region: 'EU' }, q2: { region: 'APAC' } }),
    { region: 'EU' })
  // A stored value anywhere beats an earlier query's default (q1 withheld or
  // waiting, so served no values, shares the control with a served q2).
  assert.deepEqual(
    seedParamValues(
      { q1: [{ name: 'region', source: 'input', default: 'US' }], q2: [{ name: 'region', source: 'input' }] },
      { q1: {}, q2: { region: 'APAC' } }),
    { region: 'APAC' })
  const e8 = seedParamValues(
    { q1: [{ name: 'customer_id', source: 'input', required: true }, { name: 'region', source: 'input', required: true }] },
    { q1: { region: 'EMEA' } })
  assert.deepEqual(e8, { region: 'EMEA' })
  console.log('PASS: the first stored value across queries wins; E8 keeps the stored input')

  // T1.3 (A4): identity inputs are never seeded, even when a value was served.
  assert.deepEqual(
    seedParamValues(
      { q1: [
        { name: 'viewer_email', source: 'identity', default: 'x@example.com' },
        { name: 'owner', source: 'input_identity_default', default: 'someone' },
        { name: 'region', source: 'input' },
      ] },
      { q1: { viewer_email: 'alice@example.com', owner: 'alice', region: 'EU' } }),
    { region: 'EU' })
  console.log('PASS: identity inputs are never seeded from a served value')
}

// ── the view seeds from what it was served, once (finding 3) ───────────────
// Source pins on SharedDashboardView.vue, and its real initParamValues run
// against the E8 shape (a source-extracted call).
{
  const { seedParamValues } = viewerInput
  const view = readFileSync(new URL('../../components/dashboard/SharedDashboardView.vue', import.meta.url), 'utf8')
  const fnSource = (name) => {
    const start = view.indexOf(`function ${name}(`)
    assert.ok(start >= 0, `${name} is defined`)
    const end = view.indexOf('\n}\n', start)
    return view.slice(start, end + 2)
  }
  const init = fnSource('initParamValues')
  const load = fnSource('loadVisualizationData')

  // T1.4: initParamValues seeds through seedParamValues from what was served,
  // and each load rebuilds servedAppliedParams beside appliedByQuery.set(.
  assert.match(init, /paramValues\.value = seedParamValues\(queryParamSpecs\.value, servedAppliedParams\)/,
    'initParamValues seeds from the served applied_params')
  assert.match(view, /\nlet servedAppliedParams: Record<string, Record<string, any>> = \{\};/, 'servedAppliedParams is component state')
  const setLine = load.split('\n').findIndex((l) => l.includes('appliedByQuery.set('))
  assert.ok(setLine >= 0, 'loadVisualizationData records applied params')
  const near = load.split('\n').slice(setLine - 1, setLine + 2).join('\n')
  assert.match(near, /nextServedApplied\[String\(\(query as any\)\.id\)\] = /, 'the served value is recorded beside appliedByQuery.set(')
  assert.match(load, /\n\s*servedAppliedParams = nextServedApplied;/, 'each load replaces servedAppliedParams (never merged)')
  assert.doesNotMatch(view, /Object\.assign\(servedAppliedParams|\.\.\.servedAppliedParams/, 'never merged across loads')

  // T1.3b: seeding happens only at mount. seedParamValues( is called only in
  // initParamValues, and initParamValues( only from onMounted, so a later load
  // (refresh, viewer run) never overwrites what the viewer has set.
  const seedCalls = [...view.matchAll(/seedParamValues\(/g)].map((m) => m.index)
  assert.equal(seedCalls.length, 1, 'seedParamValues( is called once')
  const initStart = view.indexOf('function initParamValues(')
  assert.ok(seedCalls[0] > initStart && seedCalls[0] < initStart + init.length, 'inside initParamValues')
  const initCalls = [...view.matchAll(/(?<!function )initParamValues\(/g)].map((m) => m.index)
  assert.equal(initCalls.length, 1, 'initParamValues( is called once')
  const mountStart = view.indexOf('\nonMounted(async () => {')
  const mountEnd = view.indexOf('\n});\n', mountStart)
  assert.ok(mountStart >= 0 && initCalls[0] > mountStart && initCalls[0] < mountEnd, 'from onMounted')

  // E8 through the real initParamValues: customer_id has no stored value,
  // region has one. After the viewer sets customer_id=14, the run body (built
  // from paramValues for every declared non-identity input it holds) carries both.
  const queryParamSpecs = { value: { q1: [
    { name: 'customer_id', source: 'input', required: true },
    { name: 'region', source: 'input', required: true },
    { name: 'viewer_email', source: 'identity' },
  ] } }
  const servedAppliedParams = { q1: { region: 'EMEA', viewer_email: 'alice@example.com' } }
  const paramValues = { value: {} }
  const body = ts.transpileModule(`${init}\ninitParamValues();`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  new Function('queryParamSpecs', 'servedAppliedParams', 'paramValues', 'seedParamValues', body)(
    queryParamSpecs, servedAppliedParams, paramValues, seedParamValues)
  paramValues.value = { ...paramValues.value, customer_id: 14 }
  const runParams = {}
  for (const spec of queryParamSpecs.value.q1) {
    if (spec.source === 'identity') continue
    if (Object.prototype.hasOwnProperty.call(paramValues.value, spec.name)) runParams[spec.name] = paramValues.value[spec.name]
  }
  assert.deepEqual(runParams, { customer_id: 14, region: 'EMEA' }, 'E8: the run sends both inputs')
  console.log('PASS: the view seeds once, at mount, from the values it was served (E8)')
}

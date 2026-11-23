import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { ref } from 'vue'
import { computeRunActivity } from '../../composables/useRunActivity.ts'

const source = readFileSync(new URL('../../composables/useRunFold.ts', import.meta.url), 'utf8')
const compiled = ts.transpile(source.replace(/^import .*$/gm, ''), { module: ts.ModuleKind.CommonJS })
const exports = {}
new Function('exports', 'ref', 'computeRunActivity', compiled)(exports, ref, computeRunActivity)
const blocks = [
  { id: 'tool', tool_execution: { tool_name: 'create_data', status: 'success' } },
  { id: 'answer', content: 'Done', plan_decision: { analysis_complete: true } },
]
const activity = computeRunActivity(blocks, { runStatus: 'success' })
const fold = exports.useRunFold({ foldFor: () => activity, blocksFor: () => blocks, keyFor: () => 'run' })
assert.equal(fold.isRunToolShown({}, blocks[0]), false)
fold.toggleRun({})
assert.equal(fold.toolGroupHeaderFor({}, blocks[0]), undefined, 'Single calls have no duplicate group header')
assert.equal(fold.isGroupedTool({}, blocks[0]), false)
assert.equal(fold.isRunToolShown({}, blocks[0]), true)
assert.equal(fold.isRunBlockShown({}, blocks[1]), true)
fold.toggleRun({})
assert.equal(fold.isRunToolShown({}, blocks[0]), false)
console.log('useRunFold: grouped tool disclosure passed')

// A running eval keeps its card (and its Stop) visible while the run works.
// run_eval is a deliverable, so it is its own row and never joins the search
// before it; that lone search is a plain one-call row too.
{
  const live = [
    { id: 'search', tool_execution: { tool_name: 'search_evals', status: 'success' } },
    { id: 'eval', tool_execution: { tool_name: 'run_eval', status: 'running' } },
  ]
  const activity = computeRunActivity(live, { runStatus: 'in_progress', keepVisible: (b) => b.id === 'eval' })
  const liveFold = exports.useRunFold({ foldFor: () => activity, blocksFor: () => live, keyFor: () => 'live' })
  assert.equal(liveFold.isToolGroupExpanded('search'), false)
  assert.equal(liveFold.isGroupedTool({}, live[1]), false)
  assert.equal(liveFold.isRunToolShown({}, live[0]), true)
  assert.equal(liveFold.isRunToolShown({}, live[1]), true)
  console.log('useRunFold: running stop controls stay visible passed')
}

// Several running sub-agents in one closed live group: each keeps its card
// (and its Stop); a finished sibling stays inside the closed group.
{
  const live = ['sa1', 'sa2', 'sa3'].map((sid) => ({ id: sid, tool_execution: { tool_name: 'delegate', status: 'success', result_json: { sub_agent_id: sid } } }))
  const activity = computeRunActivity(live, { runStatus: 'in_progress', keepVisible: (b) => b.id !== 'sa3' })
  const fold = exports.useRunFold({ foldFor: () => activity, blocksFor: () => live, keyFor: () => 'fan' })
  assert.deepEqual(live.map((b) => fold.isRunToolShown({}, b)), [true, true, false])
  console.log('useRunFold: several running sub-agents stay visible passed')
}

// Live: a deliverable already showing as its own row in the open chain is not
// repeated in the deliverables list below it. Settled: the list is back under
// the conclusion, whether or not the steps are open.
{
  const live = [
    { id: 'inspect', tool_execution: { tool_name: 'inspect_data', status: 'success' } },
    { id: 'data', tool_execution: { tool_name: 'create_data', status: 'success' } },
    { id: 'instr', tool_execution: { tool_name: 'create_instruction', status: 'success' } },
    { id: 'dash', tool_execution: { tool_name: 'create_artifact', status: 'success', result_json: { artifact_id: 'a1' } } },
  ]
  const activity = computeRunActivity(live, { runStatus: 'in_progress' })
  const fold = exports.useRunFold({ foldFor: () => activity, blocksFor: () => live, keyFor: () => 'deliv' })
  assert.deepEqual(activity.toolGroups.map((g) => g.blockIds), [['inspect', 'data'], ['instr'], ['dash']])
  assert.deepEqual(live.map((b) => fold.isRunToolShown({}, b)), [false, false, true, true])
  assert.deepEqual(fold.runDeliverables({}).map((b) => b.id), [])
  // The user closes the live steps: the cards come back below.
  fold.toggleRun({})
  assert.deepEqual(fold.runDeliverables({}).map((b) => b.id), ['instr', 'dash'])

  const settledBlocks = [...live, { id: 'answer', content: 'Done', plan_decision: { analysis_complete: true } }]
  const settled = computeRunActivity(settledBlocks, { runStatus: 'success' })
  const done = exports.useRunFold({ foldFor: () => settled, blocksFor: () => settledBlocks, keyFor: () => 'deliv2' })
  assert.deepEqual(done.runDeliverables({}).map((b) => b.id), ['instr', 'dash'])
  done.toggleRun({})
  assert.deepEqual(done.runDeliverables({}).map((b) => b.id), ['instr', 'dash'])
  console.log('useRunFold: live deliverables are not repeated below the chain passed')
}

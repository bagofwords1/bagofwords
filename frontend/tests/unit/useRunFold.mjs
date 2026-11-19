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

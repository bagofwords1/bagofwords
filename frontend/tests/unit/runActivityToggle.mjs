import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import ts from 'typescript'
const nodeRequire = createRequire(import.meta.url)
function loadSfc(file, id) {
  const { descriptor } = parse(readFileSync(new URL(`../../components/${file}`, import.meta.url), 'utf8'))
  const script = compileScript(descriptor, { id, inlineTemplate: true })
  const out = {}
  new Function('require', 'exports', ts.transpile(script.content, { module: ts.ModuleKind.CommonJS }))(require, out)
  return out
}
function require(spec) {
  if (spec === '~/components/RunLiveIndicator.vue') return loadSfc('RunLiveIndicator.vue', 'run-live')
  return nodeRequire(spec)
}
const exports = loadSfc('RunActivityToggle.vue', 'run-activity')
async function render(props) {
  const app = createSSRApp(exports.default, { expanded: false, duration: '2s', translate: key => key, ...props })
  app.component('Icon', { render: () => null })
  return renderToString(app)
}
const thinking = await render({ live: true, hasSteps: false })
assert.match(thinking, /data-testid="run-thinking"/)
assert.doesNotMatch(thinking, /<button|2s|run-fold-toggle/)
// The live dot carries the elapsed time once there is one.
const ticking = await render({ live: true, hasSteps: false, elapsed: '12s' })
assert.match(ticking, /data-testid="run-live-elapsed"[^>]*>12s</)
assert.match(ticking, /run-live-dot/)
const working = await render({ live: true, hasSteps: true })
assert.match(working, /run-fold-toggle/)
assert.doesNotMatch(working, /run-thinking|workingFor|viewSteps|2s/)
assert.match(await render({ live: false, hasSteps: true }), /workedFor/)
assert.doesNotMatch(await render({ live: false, hasSteps: false }), /<button|run-thinking/)
console.log('runActivityToggle: empty, live (with elapsed), completed and direct-answer states passed')

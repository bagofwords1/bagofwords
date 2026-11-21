import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import ts from 'typescript'
const require = createRequire(import.meta.url)
const { descriptor } = parse(readFileSync(new URL('../../components/RunActivityToggle.vue', import.meta.url), 'utf8'))
const script = compileScript(descriptor, { id: 'run-activity', inlineTemplate: true })
const compiled = ts.transpile(script.content, { module: ts.ModuleKind.CommonJS })
const exports = {}
new Function('require', 'exports', compiled)(require, exports)
async function render(props) {
  const app = createSSRApp(exports.default, { expanded: false, duration: '2s', translate: key => key, ...props })
  app.component('Icon', { render: () => null })
  return renderToString(app)
}
const thinking = await render({ live: true, hasSteps: false })
assert.match(thinking, /data-testid="run-thinking"/)
assert.doesNotMatch(thinking, /<button|2s|run-fold-toggle/)
const working = await render({ live: true, hasSteps: true })
assert.match(working, /run-fold-toggle/)
assert.doesNotMatch(working, /run-thinking|workingFor|viewSteps|2s/)
assert.match(await render({ live: false, hasSteps: true }), /workedFor/)
assert.doesNotMatch(await render({ live: false, hasSteps: false }), /<button|run-thinking/)
console.log('runActivityToggle: empty, live, completed and direct-answer states passed')

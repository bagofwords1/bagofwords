import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import ts from 'typescript'
import { nextTick, ref, watch } from 'vue'

// Run: node --test tests/unit/embedViewerRefresh.mjs
// Inside a host application, a silent renewal for the same viewer keeps the
// view (no remount: EmbeddedArtifactGate only swaps the credential), but the
// session's viewer may come back with another role or other groups. The
// running artifact gets the new ARTIFACT_DATA.current_user, by the view's
// usual data push; its data is not re-run and its frame not rebuilt.
//
// The real SharedDashboardView.vue code: every top-level watch over the embed
// credential, with the view's functions it calls (pushArtifactData, ...).

const view = readFileSync(new URL('../../components/dashboard/SharedDashboardView.vue', import.meta.url), 'utf8')
const script = view.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)[1]
const ast = ts.createSourceFile('view.ts', script, ts.ScriptTarget.Latest, true)
const functions = new Map(ast.statements
  .filter((s) => ts.isFunctionDeclaration(s) && s.name)
  .map((s) => [s.name.text, s.getText(ast)]))
const watches = ast.statements
  .filter((s) => ts.isExpressionStatement(s) && ts.isCallExpression(s.expression)
    && s.expression.expression.getText(ast) === 'watch' && /\bembedCredential\b/.test(s.getText(ast)))
  .map((s) => s.getText(ast))
// The functions those watches reach, transitively (by name), but for the
// ones stubbed below (the payload's parts and the iframe's postMessage).
const STUBBED = ['paramsPayload', 'artifactRuntime', 'postToArtifactIframe', 'useMyFetch']
const used = new Set(STUBBED)
const reach = (text) => {
  for (const [name, body] of functions) {
    if (!used.has(name) && new RegExp(`\\b${name}\\b`).test(text)) { used.add(name); reach(body) }
  }
}
watches.forEach(reach)
const code = ts.transpileModule([...used].filter((n) => !STUBBED.includes(n)).map((n) => functions.get(n)).concat(watches).join('\n'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

const ALICE = { id: 'u-alice', name: 'Alice', role: 'viewer', groups: ['sales'] }
const credential = ref({ token: 'bow_embed_a1', organizationId: 'org', hostApplication: 'host_1', viewer: ALICE })
const viewerContext = ref(ALICE)  // what fetchViewerContext() read at load
const posts = []
const fetched = []
const seed = { current_user: ALICE }
const scope = {
  watch, ref, nextTick,
  embedCredential: credential,
  viewerContext,
  props: { hostContext: { record: 'r-1' } },
  report: ref({ id: 'rep-1', title: 'Pipeline', theme_name: 'default' }),
  visualizationsData: ref([{ id: 'viz-1', rows: [{ amount: 3 }] }]),
  filesData: ref([]),
  paramsPayload: () => ({ values: {} }),
  artifactRuntime: () => ({ nonce: 'n' }),
  postToArtifactIframe: (message) => posts.push(message),
  artifactIframeLoaded: ref(true),
  artifactPushPending: false,
  srcdocSeed: ref(seed),
  useMyFetch: (url) => { fetched.push(url); return { data: ref(null), error: ref(null) } },
}
new Function(...Object.keys(scope), code)(...Object.values(scope))
const seedBefore = scope.srcdocSeed.value
const flush = async () => { for (let i = 0; i < 5; i++) await nextTick() }

// A renewal for the same viewer, with the same payload: nothing to tell.
credential.value = { ...credential.value, token: 'bow_embed_a2', viewer: { ...ALICE, groups: ['sales'] } }
await flush()
assert.equal(posts.length, 0, 'a renewal that changes nothing about the viewer pushes nothing')

// A renewal for the same viewer, now a manager in another group.
const promoted = { ...ALICE, role: 'manager', groups: ['sales', 'emea'] }
credential.value = { ...credential.value, token: 'bow_embed_a3', viewer: promoted }
await flush()
const pushed = posts.filter((m) => m.type === 'ARTIFACT_DATA')
assert.equal(pushed.length, 1, 'the running artifact is sent its data again')
assert.deepEqual(pushed[0].payload.current_user, { id: 'u-alice', name: 'Alice', role: 'manager', groups: ['sales', 'emea'] },
  'with the renewed role and groups as current_user')
assert.deepEqual(pushed[0].payload.visualizations, [{ id: 'viz-1', rows: [{ amount: 3 }] }], 'and the same data')
assert.deepEqual(pushed[0].payload.host_context, { record: 'r-1' })
assert.deepEqual(viewerContext.value, promoted, 'later pushes (filters, runs) carry the new viewer too')
assert.deepEqual(fetched, [], 'no data is fetched or re-run')
assert.equal(scope.srcdocSeed.value, seedBefore, 'the frame is not rebuilt (its seed is not replaced)')
assert.deepEqual(scope.srcdocSeed.value, { current_user: ALICE }, '(nor changed)')
console.log('PASS: a same-viewer renewal with a changed role or groups updates current_user in the running artifact')

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import ts from 'typescript'
import { ref } from 'vue'
import { createI18n } from 'vue-i18n'

// Run: node --experimental-strip-types tests/unit/entityEditModalSave.mjs
// The saved-query editor runs the query after Save (to fill its snapshot).
// useMyFetch never throws, so a refused or failed run used to vanish behind
// the success path: the editor closed with no word that the run failed. Save
// stays successful, and the run's reason is shown as a warning. Any failure
// the editor shows reads as text, never as a validation array printed as JSON.

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const scriptOf = (sfc) => sfc.match(/<script[^>]*\bsetup\b[^>]*>([\s\S]*?)<\/script>/)[1]
const declarations = (script) => {
  const ast = ts.createSourceFile('c.ts', script, ts.ScriptTarget.Latest, true)
  const out = new Map()
  for (const s of ast.statements) {
    if (ts.isFunctionDeclaration(s) && s.name) out.set(s.name.text, s.getText(ast))
  }
  return out
}
const js = (parts) => ts.transpileModule(parts.join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

const en = JSON.parse(read('../../../locales/en.json'))
const { global: i18n } = createI18n({ legacy: false, locale: 'en', missingWarn: false, fallbackWarn: false,
  messages: { en } })
const { t } = i18n
// The real composable, with the i18n it would get from Nuxt.
globalThis.useI18n = () => i18n
const { useErrorMessage } = await import('../../composables/useErrorMessage.ts')
const { getErrorMessage } = useErrorMessage()

const modal = read('../../components/entity/EntityEditModal.vue')
const fns = declarations(scriptOf(modal))

const refusal = { statusCode: 403, data: { detail: 'Query refused: writes are not allowed' } }
const validation = { statusCode: 422, data: { detail: [
  { loc: ['body', 'title'], msg: 'Field required', type: 'missing', input: 'typed title' },
] } }

async function save({ create, saveFailure = null, runFailure = null }) {
  const calls = []
  const toasts = []
  const emitted = []
  const scope = {
    saving: ref(false), errorMsg: ref(''), open: ref(true),
    isCreate: ref(create), canCreateEntities: ref(true),
    form: ref({ type: 'model', title: 'Orders', description: null, status: 'published', data_source_ids: ['ds-1'] }),
    code: ref('def generate_df(ds_clients, excel_files, params): ...'), savedCode: ref(''),
    paramSpecs: ref([]), cleanParamSpecs: () => [], selectedIds: ref(['ds-1']),
    props: { entityId: create ? null : 'e-1' },
    emit: (name, value) => emitted.push(name), t, toast: { add: (n) => toasts.push(n) }, getErrorMessage,
    // useMyFetch never throws: a failed request comes back as { error }.
    useMyFetch: async (url, opts) => {
      calls.push([url, opts.method])
      const failure = url.endsWith('/run') ? runFailure : saveFailure
      return { data: ref(failure ? null : { id: 'e-1', status: 'published', global_status: 'approved' }), error: ref(failure) }
    },
  }
  assert.ok(fns.has('onSave'), 'the editor saves with onSave')
  const helpers = fns.has('warnIfRunFailed') ? [fns.get('warnIfRunFailed')] : []
  const onSave = new Function(...Object.keys(scope), `${js([...helpers, fns.get('onSave')])}\nreturn onSave`)(...Object.values(scope))
  await onSave()
  return { calls, toasts, emitted, errorMsg: scope.errorMsg.value, open: scope.open.value }
}

for (const create of [true, false]) {
  const kind = create ? 'a new query' : 'an edited query'
  let r = await save({ create, runFailure: refusal })
  assert.ok(r.calls.some(([url]) => url.endsWith('/run')), `${kind} is run after Save`)
  assert.deepEqual(r.emitted, ['saved'], `${kind}: Save still succeeds when its run fails`)
  assert.equal(r.open, false, `${kind}: the editor closes`)
  const warnings = r.toasts.filter((n) => n.color === 'amber')
  assert.equal(warnings.length, 1, `${kind}: one warning for the failed run`)
  assert.equal(warnings[0].title, en.entityCreate.savedRunFailed, `${kind}: the warning says the run failed`)
  assert.equal(warnings[0].description, refusal.data.detail, `${kind}: the warning gives the run's reason`)
  assert.ok(!r.toasts.some((n) => n.color === 'red'), `${kind}: no error toast, the save worked`)

  r = await save({ create })
  assert.ok(!r.toasts.some((n) => n.color === 'amber'), `${kind}: a good run warns of nothing`)
  assert.deepEqual(r.emitted, ['saved'])

  r = await save({ create, saveFailure: validation })
  assert.equal(r.errorMsg, 'title: Field required', `${kind}: a validation failure reads as text`)
  assert.ok(!r.errorMsg.includes('typed title'), 'never the submitted value')
  assert.deepEqual(r.emitted, [], `${kind}: a failed save keeps the editor open`)
  assert.equal(r.toasts.filter((n) => n.color === 'red').length, 1)
  assert.equal(r.toasts[0].description, 'title: Field required')
}
console.log('PASS: Save stays successful and warns with the reason when its run fails')

async function preview(failure) {
  const scope = {
    running: ref(false), runError: ref(''), result: ref({ rows: [1] }), appliedParams: ref(null),
    isCreate: ref(false), code: ref('code'), paramSpecs: ref([]), paramTestValues: ref({}),
    cleanParamSpecs: () => [], collectTestValues: () => ({}), selectedIds: ref([]),
    props: { entityId: 'e-1' }, stamp: () => {}, t, getErrorMessage,
    useMyFetch: async () => ({ data: ref(null), error: ref(failure) }),
  }
  const run = new Function(...Object.keys(scope), `${js([fns.get('run')])}\nreturn run`)(...Object.values(scope))
  await run()
  return { runError: scope.runError.value, result: scope.result.value }
}

let p = await preview(validation)
assert.equal(p.runError, 'title: Field required', 'a validation failure of a run reads as text')
assert.equal(p.result, null)
p = await preview(refusal)
assert.equal(p.runError, refusal.data.detail, 'a refused run shows its reason')
p = await preview({ statusCode: 500, data: {} })
assert.equal(p.runError, en.queries.runFailed, 'with no reason the run says it failed')
console.log('PASS: a failed run in the editor reads as text')

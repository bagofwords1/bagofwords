import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import ts from 'typescript'
import { ref } from 'vue'
import { createI18n } from 'vue-i18n'

import { catalogMessage } from '../../composables/useErrorMessage.ts'

// Run: node --test tests/unit/kmsMemberCredentials.mjs
// A KMS Lighthouse member's login lives on the connection row that runtime
// reads. The credentials modal saves it there and shows the save error (the
// backend's localized reason, e.g. the token format); the integration page's
// Disconnect clears it there; the file-scope card keeps root folder 0.
// Other connection types keep their behaviour.

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const scriptOf = (sfc) => sfc.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)[1]
const declarations = (script) => {
  const ast = ts.createSourceFile('c.ts', script, ts.ScriptTarget.Latest, true)
  const out = new Map()
  for (const s of ast.statements) {
    if (ts.isFunctionDeclaration(s) && s.name) out.set(s.name.text, s.getText(ast))
    if (ts.isVariableStatement(s)) {
      for (const d of s.declarationList.declarations) if (ts.isIdentifier(d.name)) out.set(d.name.text, s.getText(ast))
    }
  }
  return out
}
const js = (parts) => ts.transpileModule(parts.join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

const en = JSON.parse(read('../../../locales/en.json'))
const { t, te } = createI18n({ legacy: false, locale: 'en', missingWarn: false, fallbackWarn: false,
  messages: { en } }).global

// ---------------------------------------------------------------- credentials modal
const modal = read('../../components/UserDataSourceCredentialsModal.vue')
const modalFns = declarations(scriptOf(modal))
assert.match(modal, /<p v-if="saveError"[^>]*role="alert"[^>]*>\{\{ saveError \}\}<\/p>/, 'the modal renders the save error')

async function save(type, failure) {
  const calls = []
  const emitted = []
  const scope = {
    saving: ref(false), saveError: ref(null), testResult: ref(null),
    connectionType: ref(type), connectionId: ref('conn-1'), ds: ref({ id: 'ds-1' }),
    form: ref({ auth_mode: 'token', credentials: { api_token: 'dummy tok3n' } }),
    emit: (name) => emitted.push(name), t,
    // The composable's precedence: the catalog line for the code, then the server detail.
    getErrorMessage: (e, fallback) => catalogMessage(e.data, t, te) ?? e.data?.detail ?? fallback,
    useMyFetch: async (url, opts) => { calls.push([url, opts.method]); return { error: ref(failure) } },
  }
  const run = new Function(...Object.keys(scope), `${js([modalFns.get('onSave')])}\nreturn onSave`)(...Object.values(scope))
  await run()
  return { calls, emitted, saveError: scope.saveError.value }
}

const tokenFormat = { data: { error_code: 'connection.kms_token_format', status_code: 422,
  detail: 'The KMS API token can contain only plain letters, digits and punctuation, with no spaces.' } }

let r = await save('kms_lighthouse', tokenFormat)
assert.deepEqual(r.calls, [['/connections/conn-1/my-credentials', 'POST']], 'KMS saves to the connection row')
assert.equal(r.saveError, en.errors['connection.kms_token_format'], 'a 422 shows the localized token-format line')
assert.ok(!r.saveError.includes('dummy tok3n'), 'never the typed token')
assert.deepEqual(r.emitted, [], 'a failed save keeps the modal open')

r = await save('kms_lighthouse', null)
assert.equal(r.saveError, null)
assert.deepEqual(r.emitted, ['saved', 'update:modelValue'], 'a good save closes the modal')

r = await save('documentum', null)
assert.deepEqual(r.calls, [['/connections/conn-1/my-credentials', 'POST']], 'Documentum is unchanged')
r = await save('postgresql', tokenFormat)
assert.deepEqual(r.calls, [['/data_sources/ds-1/my-credentials', 'POST']], 'other types keep the legacy endpoint')
assert.equal(r.saveError, null, 'other types keep their save behaviour')

// ---------------------------------------------------------------- integration page Disconnect
const page = read('../../pages/integrations/[id]/connection.vue')
const pageFns = declarations(scriptOf(page))

async function disconnect(type) {
  const calls = []
  const scope = {
    dsId: ref('ds-1'), connectionId: ref('conn-1'), connectionType: ref(type),
    fetchIntegration: async () => {},
    useMyFetch: async (url, opts) => { calls.push([url, opts.method]); return { error: ref(null) } },
  }
  const run = new Function(...Object.keys(scope),
    `${js([pageFns.get('disconnectUserCredentials')])}\nreturn disconnectUserCredentials`)(...Object.values(scope))
  await run()
  return calls
}

assert.deepEqual(await disconnect('kms_lighthouse'), [['/connections/conn-1/my-credentials', 'DELETE']],
  'KMS Disconnect clears the connection row')
assert.deepEqual(await disconnect('postgresql'), [['/data_sources/ds-1/my-credentials', 'DELETE']],
  'other types keep the legacy Disconnect')

// ---------------------------------------------------------------- file-scope card
const scope = read('../../components/datasources/FileSourceScope.vue')
const scopeFns = declarations(scriptOf(scope))
const baseOf = new Function(`${js([scopeFns.get('cfg'), scopeFns.get('baseOf')])}\nreturn baseOf`)()
const kms = (config) => baseOf({ type: 'kms_lighthouse', config: { base_url: 'https://kms.example.com', ...config } })

assert.equal(kms({ root_folder_id: 0 }), 'https://kms.example.com / folder 0', 'root folder 0 is kept')
assert.equal(kms({ root_folder_id: 10 }), 'https://kms.example.com / folder 10')
assert.equal(kms({ root_folder_id: '' }), 'https://kms.example.com', 'a blank root is the top level')
assert.equal(kms({ root_folder_id: null }), 'https://kms.example.com')
assert.equal(kms({}), 'https://kms.example.com')
assert.equal(baseOf({ type: 'documentum', config: { rest_url: 'https://dctm', repository: 'r', root_path: '/a' } }),
  'https://dctm / r / /a', 'Documentum is unchanged')
assert.equal(baseOf({ type: 'network_dir', config: { root_path: '/mnt/share' } }), '/mnt/share')

console.log('kmsMemberCredentials: ok')

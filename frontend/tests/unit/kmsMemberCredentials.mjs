import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import ts from 'typescript'
import * as Vue from 'vue'
import { ref } from 'vue'
import { compileTemplate, parse } from 'vue/compiler-sfc'
import { renderToString } from 'vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { catalogMessage } from '../../composables/useErrorMessage.ts'

// Run: node --test tests/unit/kmsMemberCredentials.mjs
// A KMS Lighthouse member's login lives on the connection row that runtime
// reads. The credentials modal saves it there and, for every type, shows the
// backend's localized reason on a failed save or Test; the integration page's
// Disconnect clears it there; the file-scope card keeps root folder 0 and says
// that a KMS root limits browsing, not access. The diagnostics panel shows a
// refused second run and keeps its download alive.
// Other connection types keep their endpoints and their silent Disconnect.

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const scriptOf = (sfc) => sfc.match(/<script[^>]*\bsetup\b[^>]*>([\s\S]*?)<\/script>/)[1]
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

// A login KMS refused on Save (or one still paused after a refusal): the localized reason, modal open.
for (const code of ['connection.kms_login_rejected', 'connection.kms_login_paused', 'connection.kms_session_refused']) {
  r = await save('kms_lighthouse', { data: { error_code: code, status_code: 422,
    detail: 'KMS Lighthouse did not accept the API token; it may be wrong or no longer valid.' } })
  assert.ok(en.errors[code], `${code} has a catalog line`)
  assert.equal(r.saveError, en.errors[code], `a refused login shows the localized ${code} line`)
  assert.ok(!r.saveError.includes('dummy tok3n'), 'never the typed token')
  assert.deepEqual(r.emitted, [], 'a refused login keeps the modal open')
}

r = await save('kms_lighthouse', null)
assert.equal(r.saveError, null)
assert.deepEqual(r.emitted, ['saved', 'update:modelValue'], 'a good save closes the modal')

r = await save('documentum', null)
assert.deepEqual(r.calls, [['/connections/conn-1/my-credentials', 'POST']], 'Documentum is unchanged')
r = await save('postgresql', tokenFormat)
assert.deepEqual(r.calls, [['/data_sources/ds-1/my-credentials', 'POST']], 'other types keep the legacy endpoint')
assert.equal(r.saveError, en.errors['connection.kms_token_format'], 'every type shows why a save failed')
assert.deepEqual(r.emitted, [], 'a failed save keeps the modal open for every type')
r = await save('postgresql', { data: { detail: 'Authentication mode not permitted by data source policy' } })
assert.equal(r.saveError, 'Authentication mode not permitted by data source policy', 'the server detail, for every type')
r = await save('postgresql', { statusCode: 500, data: {} })
assert.equal(r.saveError, en.data.failed, 'with no reason the save says it failed')

// The Test button: every type shows the backend's reason, never ofetch's status line.
async function testCreds(type, failure) {
  const scope = {
    testing: ref(false), testResult: ref(null),
    connectionType: ref(type), ds: ref({ id: 'ds-1' }),
    form: ref({ auth_mode: 'token', credentials: { api_token: 'dummy tok3n' } }), t,
    getErrorMessage: (e, fallback) => catalogMessage(e.data, t, te) ?? e.data?.detail ?? fallback,
    useMyFetch: async () => ({ data: ref(null), error: ref(failure) }),
  }
  const run = new Function(...Object.keys(scope), `${js([modalFns.get('onTest')])}\nreturn onTest`)(...Object.values(scope))
  await run()
  return scope.testResult.value
}

const ofetchLine = '[POST] "/api/data_sources/ds-1/my-credentials/test": 422 Unprocessable Entity'
r = await testCreds('kms_lighthouse', { ...tokenFormat, message: ofetchLine })
assert.deepEqual(r, { success: false, message: en.errors['connection.kms_token_format'] },
  'a KMS Test shows the localized token-format line')
r = await testCreds('kms_lighthouse', { message: ofetchLine, data: { detail: 'Invalid credentials: password: required' } })
assert.equal(r.message, 'Invalid credentials: password: required', 'other KMS 422s show the server detail')
r = await testCreds('postgresql', { ...tokenFormat, message: ofetchLine })
assert.equal(r.message, en.errors['connection.kms_token_format'], 'every type shows the localized reason on Test')
r = await testCreds('postgresql', { message: ofetchLine, data: {} })
assert.equal(r.message, en.data.failed, 'with no reason the Test says it failed, not the status line')

// Switching the auth mode clears a stale save error.
const authWatch = scriptOf(modal).match(/watch\(authMode, \(v\) => \{([\s\S]*?)\n\}\)/)
assert.ok(authWatch, 'the modal watches authMode')
assert.match(authWatch[1], /saveError\.value = null/, 'an auth-mode change clears the save error')

// ---------------------------------------------------------------- integration page Disconnect
const page = read('../../pages/integrations/[id]/connection.vue')
const pageFns = declarations(scriptOf(page))

const toasts = []
async function disconnect(type, failure = null) {
  const calls = []
  toasts.length = 0
  const scope = {
    dsId: ref('ds-1'), connectionId: ref('conn-1'), connectionType: ref(type), t,
    toast: { add: (n) => toasts.push(n) },
    getErrorMessage: (e, fallback) => catalogMessage(e.data, t, te) ?? e.data?.detail ?? fallback,
    fetchIntegration: async () => {},
    // useMyFetch never throws: a failed request comes back as { error }.
    useMyFetch: async (url, opts) => { calls.push([url, opts.method]); return { error: ref(failure) } },
  }
  const run = new Function(...Object.keys(scope),
    `${js([pageFns.get('disconnectUserCredentials')])}\nreturn disconnectUserCredentials`)(...Object.values(scope))
  await run()
  return calls
}

assert.deepEqual(await disconnect('kms_lighthouse'), [['/connections/conn-1/my-credentials', 'DELETE']],
  'KMS Disconnect clears the connection row')
assert.deepEqual(toasts, [], 'a good Disconnect shows no error')
assert.deepEqual(await disconnect('postgresql'), [['/data_sources/ds-1/my-credentials', 'DELETE']],
  'other types keep the legacy Disconnect')

const deleteFailed = { statusCode: 404, data: { error_code: 'connection.not_found', detail: 'Connection not found' } }
await disconnect('kms_lighthouse', deleteFailed)
assert.equal(toasts.length, 1, 'a failed KMS Disconnect shows one error toast')
assert.equal(toasts[0].color, 'red')
assert.equal(toasts[0].title, en.errors['connection.not_found'], 'the toast carries the localized reason')
await disconnect('kms_lighthouse', { statusCode: 500, data: {} })
assert.equal(toasts[0].title, en.data.failed, 'with no reason the toast says the action failed')
await disconnect('postgresql', deleteFailed)
assert.deepEqual(toasts, [], 'other types keep the silent Disconnect')

// ---------------------------------------------------------------- integration page connected state
// The connected line names the account the source reported, never the BOW user.
assert.ok(pageFns.has('connectedLabel'), 'the page labels the connected state from the member status')
const connectedLabel = (status) => new Function('computed', 'connectionUserStatus', 't',
  `${js([pageFns.get('connectedLabel')])}\nreturn connectedLabel`)(Vue.computed, ref(status), t).value
assert.equal(connectedLabel({ has_user_credentials: true, account_identity: 'bob.billing' }),
  t('integrationConnection.connectedAs', { identity: 'bob.billing' }), 'the reported identity is named')
assert.ok(connectedLabel({ has_user_credentials: true, account_identity: 'bob.billing' }).includes('bob.billing'))
for (const status of [{ has_user_credentials: true }, { has_user_credentials: true, account_identity: null },
  { has_user_credentials: true, account_identity: '' }]) {
  assert.equal(connectedLabel(status), en.integrationConnection.connected, 'with no reported identity: no name at all')
}
assert.doesNotMatch(page, /connectedUserDisplay|Connected as \{\{/, 'the BOW user name is not shown as the connected account')
assert.match(parse(page).descriptor.template.content, /\{\{ connectedLabel \}\}/, 'the connected line renders the label')

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

// The components as they render: their real templates, with the card's and the panel's real helpers.
const templateOf = (sfc, id) => {
  const { code } = compileTemplate({ source: parse(sfc).descriptor.template.content, filename: `${id}.vue`, id })
  return new Function('Vue', code
    .replace(/import \{([^}]*)\} from "vue"/, (_, names) => `const {${names.replace(/ as /g, ': ')}} = Vue`)
    .replace('export function render', 'return function render'))(Vue)
}
const NO_RENDER = { render: () => null }
const rendered = (render, state, components) => {
  const app = Vue.createSSRApp({ render, setup: () => state })
  app.config.globalProperties.$t = t
  for (const [name, component] of Object.entries(components)) app.component(name, component)
  return renderToString(app)
}

const scopeRender = templateOf(scope, 'file-source-scope')
const scopeNames = [...scopeFns.keys()]
const FILE_TYPES = { kms_lighthouse: { data_shape: 'files' }, documentum: { data_shape: 'files' }, network_dir: { data_shape: 'files' } }
const card = (connections, given = {}) => {
  const state = new Function('withDefaults', 'defineProps', 'computed', 't',
    `${js(scopeNames.map((name) => scopeFns.get(name)))}\nreturn { ${scopeNames.join(', ')} }`)(
    (_, defaults) => ({ ...defaults, connections, registryByType: FILE_TYPES, ...given }), () => ({}), Vue.computed, t)
  return rendered(scopeRender, { ...state.props, ...state, t }, { DataSourceIcon: NO_RENDER })
}

const KMS_ROOTED = { id: 'k', type: 'kms_lighthouse', name: 'KMS', config: { base_url: 'https://kms.example.com', root_folder_id: 10 } }
const DOCUMENTUM = { id: 'd', type: 'documentum', name: 'Docs', config: { rest_url: 'https://dctm', root_path: '/a', include_globs: '*.pdf' } }
const NETWORK = { id: 'n', type: 'network_dir', name: 'Share', config: { root_path: '/mnt/share' } }
const PATTERN_SUBTITLE = 'File connections are scoped by include-patterns, not by picking individual files. Everything matching is available to the agent.'

const componentWarn = console.warn
console.warn = () => {}       // rendered without the Nuxt shell
const kmsCard = await card([KMS_ROOTED])
const blankRootCard = await card([{ id: 'k', type: 'kms_lighthouse', name: 'KMS', config: { base_url: 'https://kms.example.com' } }])
const dctmCard = await card([DOCUMENTUM])
const shareCard = await card([NETWORK])
const mixedCard = await card([KMS_ROOTED, DOCUMENTUM])
const noConnectionCard = await card([])
const callerCard = await card([KMS_ROOTED], { headerSubtitle: 'Words from the caller.' })
const blankCallerCard = await card([KMS_ROOTED], { headerSubtitle: '' })

const agentFiles = read('../../components/datasources/AgentFilesPanel.vue')
const agentFilesFns = declarations(scriptOf(agentFiles))
const agentFilesRender = templateOf(agentFiles, 'agent-files-panel')
const agentFilesHelpers = new Function(`${js(['cfg', 'baseOf', 'globsOf', 'indexModeOf', 'badgeLabel', 'badgeClass']
  .map((name) => agentFilesFns.get(name)))}\nreturn { cfg, baseOf, globsOf, indexModeOf, badgeLabel, badgeClass }`)()
const filesTab = (conn) => rendered(agentFilesRender,
  { ...agentFilesHelpers, files: [], fileConnections: [conn], canUpdate: false, uploading: false },
  { DataSourceIcon: NO_RENDER, UIcon: NO_RENDER,
    ConnectionFileBrowser: { props: ['connectionType'], render() { return Vue.h('i', { 'data-type': this.connectionType }) } } })
const kmsFilesTab = await filesTab(KMS_ROOTED)
const dctmFilesTab = await filesTab(DOCUMENTUM)
const shareFilesTab = await filesTab(NETWORK)

const fileBrowser = read('../../components/datasources/ConnectionFileBrowser.vue')
const fileBrowserRender = templateOf(fileBrowser, 'connection-file-browser')
const listing = (connectionType) => rendered(fileBrowserRender, {
  connectionType, connectRequired: false, opened: null, loading: false, loaded: true, error: null, segments: [], query: '',
  searching: false, contents: { folders: [], files: [] }, shownFiles: [], results: [], files: [{}, {}, {}], total: 3,
  previewSupported: true,
}, { UIcon: NO_RENDER, ConnectionFilePreview: NO_RENDER })
const kmsListing = await listing('kms_lighthouse')
const dctmListing = await listing('documentum')
const untypedListing = await listing(undefined)
console.warn = componentWarn

// The header above the cards.
for (const [what, html] of [['a root folder', kmsCard], ['no root folder', blankRootCard]]) {
  assert.doesNotMatch(html, /include-patterns/, `the header over a KMS card with ${what} claims no pattern scope`)
  assert.doesNotMatch(html, /Everything matching/, `the header over a KMS card with ${what} claims no pattern scope`)
  assert.ok(html.includes(en.kmsFileScope.headerSubtitle), `the header over a KMS card with ${what} has the KMS wording`)
}
for (const [what, html] of [['Documentum', dctmCard], ['a network folder', shareCard], ['a mixed list', mixedCard], ['no connection', noConnectionCard]]) {
  assert.ok(html.includes(PATTERN_SUBTITLE), `${what} keeps today's header`)
  assert.ok(!html.includes(en.kmsFileScope.headerSubtitle), `${what} has no KMS header`)
}
assert.ok(callerCard.includes('Words from the caller.'), "a caller's subtitle wins over the KMS wording")
assert.ok(!callerCard.includes(en.kmsFileScope.headerSubtitle) && !callerCard.includes(PATTERN_SUBTITLE), "only the caller's subtitle shows")
assert.ok(!blankCallerCard.includes(en.kmsFileScope.headerSubtitle), 'an empty subtitle from a caller wins too')
assert.match(en.kmsFileScope.headerSubtitle, /KMS permissions/)
assert.doesNotMatch(en.kmsFileScope.headerSubtitle, /pattern/i)

// The Files tab: the Scope row, and the summary under the listing.
assert.doesNotMatch(kmsFilesTab, /whole path/, 'the KMS Files tab names no path scope')
assert.ok(kmsFilesTab.includes(en.kmsFileScope.browsingOnly), 'the KMS Files tab shows browsing only as its scope')
assert.ok(kmsFilesTab.includes('data-type="kms_lighthouse"'), 'the panel tells the browser its connection type')
assert.ok(dctmFilesTab.includes('*.pdf') && !dctmFilesTab.includes(en.kmsFileScope.browsingOnly), 'Documentum keeps its patterns')
assert.ok(dctmFilesTab.includes('data-type="documentum"'))
assert.ok(shareFilesTab.includes('whole path') && !shareFilesTab.includes(en.kmsFileScope.browsingOnly), 'no patterns reads as before')

assert.doesNotMatch(kmsListing, /reads only these/, 'the KMS summary promises no reading limit')
assert.doesNotMatch(kmsListing, /denials are audited/, 'the KMS summary promises no audit of denials')
assert.ok(kmsListing.includes(t('agentsPage.fileBrowserSummaryKms', { n: 3 })), 'the KMS summary has its own wording')
assert.match(en.agentsPage.fileBrowserSummaryKms, /\{n\} files listed/)
assert.match(en.agentsPage.fileBrowserSummaryKms, /KMS permissions/)
assert.match(fileBrowser, /defineProps<\{[^}]*connectionType\?: string/, 'the browser declares the type the panel passes')
const SCOPED_SUMMARY = '3 files in scope · the agent reads only these · denials are audited'
assert.ok(dctmListing.includes(SCOPED_SUMMARY), 'Documentum keeps its summary')
assert.ok(untypedListing.includes(SCOPED_SUMMARY), 'a caller that names no type keeps the summary')

// The card body.
for (const [what, html] of [['a root folder', kmsCard], ['no root folder', blankRootCard]]) {
  assert.doesNotMatch(html, /denied and audited/, `a KMS card with ${what} promises no access boundary`)
  assert.doesNotMatch(html, /only<\/strong> files matching/, `a KMS card with ${what} promises no file scope`)
  assert.doesNotMatch(html, /whole path/, `a KMS card with ${what} names no path scope`)
  assert.ok(html.includes(en.kmsFileScope.accessNote), `a KMS card with ${what} says what the root does`)
  assert.ok(html.includes(en.kmsFileScope.browsingOnly), `a KMS card with ${what} shows browsing only as its scope`)
}
assert.ok(kmsCard.includes('https://kms.example.com / folder 10'), 'the card still names the root folder')
assert.match(en.kmsFileScope.accessNote, /browsing/i)
assert.match(en.kmsFileScope.accessNote, /KMS permissions/)
assert.match(en.kmsFileScope.accessNote, /search and read/)

for (const [what, html] of [['Documentum', dctmCard], ['a network folder', shareCard]]) {
  assert.match(html, /denied and audited/, `${what} keeps its scope promise`)
  assert.ok(!html.includes(en.kmsFileScope.accessNote), `${what} has no KMS wording`)
  assert.ok(!html.includes(en.kmsFileScope.browsingOnly), `${what} has no KMS wording`)
}
assert.ok(dctmCard.includes('*.pdf'), 'patterns stay')
assert.ok(shareCard.includes('whole path (no patterns)'), 'no patterns reads as before')

for (const locale of ['en', 'es', 'he', 'fr', 'sv', 'ar', 'ru', 'de', 'pt', 'it']) {
  const catalog = JSON.parse(read(`../../../locales/${locale}.json`))
  const section = catalog.kmsFileScope
  assert.deepEqual(Object.keys(section ?? {}).sort(), ['accessNote', 'browsingOnly', 'headerSubtitle'], `${locale} has the card's keys`)
  for (const key of ['accessNote', 'browsingOnly', 'headerSubtitle']) {
    assert.ok(section[key].trim(), `${locale}.${key} is written`)
    if (locale !== 'en') assert.notEqual(section[key], en.kmsFileScope[key], `${locale}.${key} is translated`)
  }
  const summary = catalog.agentsPage.fileBrowserSummaryKms
  assert.ok(summary?.includes('{n}'), `${locale} has the KMS summary with its count`)
  assert.doesNotMatch(summary, /\|/, `${locale}: a bare "|" would split the summary into plural forms`)
  if (locale !== 'en') assert.notEqual(summary, en.agentsPage.fileBrowserSummaryKms, `${locale} summary is translated`)
  const connection = catalog.integrationConnection
  assert.ok(connection?.connected?.trim(), `${locale} has the neutral connected line`)
  assert.ok(connection?.connectedAs?.includes('{identity}'), `${locale} names the identity in the connected line`)
  for (const code of ['connection.kms_login_rejected', 'connection.kms_login_paused', 'connection.kms_session_refused']) {
    assert.ok(catalog.errors?.[code]?.trim(), `${locale} has ${code}`)
    if (locale !== 'en') assert.notEqual(catalog.errors[code], en.errors[code], `${locale} ${code} is translated`)
  }
}

// ---------------------------------------------------------------- diagnostics panel
const panel = read('../../components/datasources/KmsDiagnosticsPanel.vue')
const panelFns = declarations(scriptOf(panel))

// A second run answers 409 with a typed code: the panel shows its localized line, and no report.
const running = { statusCode: 409, data: { error_code: 'connection.kms_diagnostics_running', detail: 'Already running' } }
const panelScope = {
  props: { connectionId: 'conn-1' }, isRunning: ref(false), report: ref({ stale: true }), errorMessage: ref(''), t,
  getErrorMessage: (e, fallback) => catalogMessage(e.data, t, te) ?? e.data?.detail ?? fallback,
  useMyFetch: async () => ({ data: ref(null), error: ref(running) }),
}
await new Function(...Object.keys(panelScope), `${js([panelFns.get('runDiagnostics')])}\nreturn runDiagnostics`)(
  ...Object.values(panelScope))()
assert.equal(panelScope.errorMessage.value, en.errors['connection.kms_diagnostics_running'], 'the panel shows the 409')
assert.equal(panelScope.report.value, null)
assert.equal(panelScope.isRunning.value, false, 'the button comes back')

// Download: the blob URL stays alive until the click has been handled.
const revoked = []
const timers = []
const clicked = []
const downloadScope = {
  reportText: () => '{}',
  Blob: class {},
  URL: { createObjectURL: () => 'blob:report', revokeObjectURL: (url) => revoked.push(url) },
  document: { createElement: () => ({ click() { clicked.push(revoked.length) } }) },
  setTimeout: (run, delay) => timers.push([run, delay]),
}
new Function(...Object.keys(downloadScope), `${js([panelFns.get('downloadReport')])}\nreturn downloadReport`)(
  ...Object.values(downloadScope))()
assert.deepEqual(clicked, [0], 'the download was clicked with the URL still alive')
assert.deepEqual(revoked, [], 'not revoked in the same tick as the click')
assert.equal(timers.length, 1)
assert.equal(timers[0][1], 0, 'revoked after one tick')
timers[0][0]()
assert.deepEqual(revoked, ['blob:report'], 'the URL is revoked after the tick')

console.log('kmsMemberCredentials: ok')

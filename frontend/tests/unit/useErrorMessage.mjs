import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { createI18n } from 'vue-i18n'

import * as errorMessage from '../../composables/useErrorMessage.ts'

const { catalogMessage, detailText, errorCatalogKeys, specificDetail } = errorMessage

// The backend's AppError carries `error_code`, and the catalogs hold the
// localized message under `errors.<code>`. Most codes hold a dot
// ("artifact.not_found"); vue-i18n reads a dotted path as nesting, so a
// plain `errors.${code}` lookup never found them and users saw the raw code
// or the server's English text instead of their language.

const en = JSON.parse(readFileSync(new URL('../../../locales/en.json', import.meta.url)))
const { t, te } = createI18n({ legacy: false, locale: 'en', missingWarn: false, fallbackWarn: false,
  messages: { en } }).global

const resolve = (code) => {
  const key = errorCatalogKeys(code).find((k) => te(k))
  return key ? t(key) : null
}

const codes = Object.keys(en.errors)
const dotted = codes.filter((c) => c.includes('.'))
assert.ok(dotted.length > 0, 'the catalog holds dotted codes')

// The regression: a plain path misses every dotted code.
assert.equal(te(`errors.${dotted[0]}`), false, 'a dotted code is not found by a plain path')

// Every code in the catalog resolves to its own message, dotted or not.
for (const code of codes) {
  const message = en.errors[code]
  if (typeof message !== 'string') continue
  const got = resolve(code)
  assert.ok(got, `errors.${code} is found`)
  if (!message.includes('{')) assert.equal(got, message, `errors.${code} is its own message`)
}

// A code the catalog does not know still falls through (caller uses the
// server's detail).
assert.equal(resolve('no.such_code'), null)

// The catalog wins over the server's English detail whenever it has the
// code (PR #1455 review): a Hebrew or Spanish user never sees the English
// sentence for a code their catalog translates.
const withDetail = { error_code: dotted[0], detail: 'The server wrote this in English.' }
assert.equal(catalogMessage(withDetail, t, te), resolve(dotted[0]))
const plain = codes.find((c) => !c.includes('.') && typeof en.errors[c] === 'string' && !en.errors[c].includes('{'))
if (plain) assert.equal(catalogMessage({ error_code: plain, detail: 'English' }, t, te), en.errors[plain])
assert.equal(catalogMessage({ error_code: 'no.such_code', detail: 'English' }, t, te), null)
assert.equal(catalogMessage({ detail: 'English' }, t, te), null)

// Exception: a few codes carry a field-level reason in `detail` that must
// reach the user verbatim. The catalog still holds a generic line for them
// (what the user would otherwise see), so detail-first is what surfaces the
// real reason instead of "Invalid resource request".
assert.ok(en.errors.ARTIFACT_RESOURCE_VALIDATION, 'the generic line detail must override exists')
assert.equal(
  specificDetail({ error_code: 'ARTIFACT_RESOURCE_VALIDATION', detail: "Field 'record_id' must be unique" }),
  "Field 'record_id' must be unique",
)
assert.equal(
  specificDetail({ error_code: 'ARTIFACT_RESOURCE_CONFLICT', detail: "Field 'slug' must be unique" }),
  "Field 'slug' must be unique",
)
// Any other code, or a missing/non-string detail, falls back to the catalog.
assert.equal(specificDetail({ error_code: dotted[0], detail: 'English' }), null)
assert.equal(specificDetail({ error_code: 'ARTIFACT_RESOURCE_VALIDATION' }), null)
assert.equal(specificDetail({ detail: 'English' }), null)

// A refusal answered as an HTTP error carries its reason either as a string
// `detail` or, for a usage quota, as an object whose `message` is the reason
// (the rest are numbers for the UI). Either way the user reads the reason,
// never the object printed as JSON.
assert.equal(detailText('Query refused: writes are not allowed'), 'Query refused: writes are not allowed')
assert.equal(
  detailText({ message: 'Monthly query limit reached (5 of 5)', metric: 'data_queries', limit: 5, used: 5 }),
  'Monthly query limit reached (5 of 5)',
)
// Nothing readable: the caller falls back to its own localized line.
for (const unreadable of [undefined, null, '', {}, { message: '' }, { metric: 'data_queries' }, 42, [{ msg: 'x' }]]) {
  assert.equal(detailText(unreadable), null, `no text in ${JSON.stringify(unreadable)}`)
}

// The data-execution errors a run answers with read in every shipped
// language, with their own numbers and reasons. A query the code swallowed
// names the underlying query errors (a refusal among them): the catalog line
// must not hide them, and a list reads as text, not as JSON.
const catalogs = Object.fromEntries(['en', 'es', 'he', 'fr', 'sv', 'ar', 'ru', 'de', 'pt', 'it'].map((loc) => {
  const messages = JSON.parse(readFileSync(new URL(`../../../locales/${loc}.json`, import.meta.url)))
  const g = createI18n({ legacy: false, locale: loc, missingWarn: false, fallbackWarn: false,
    messages: { [loc]: messages } }).global
  return [loc, g]
}))
for (const [loc, g] of Object.entries(catalogs)) {
  const swallowed = catalogMessage({ error_code: 'query.failed_silently',
    params: { query_errors: ['no such column: totl', 'Query refused: writes are not allowed'] } }, g.t, g.te)
  assert.ok(swallowed, `${loc}: query.failed_silently has a line`)
  assert.ok(swallowed.includes('no such column: totl'), `${loc}: the first query error is shown: ${swallowed}`)
  assert.ok(swallowed.includes('Query refused: writes are not allowed'), `${loc}: every query error is shown`)
  assert.ok(!swallowed.includes('["') && !swallowed.includes('"]'), `${loc}: the errors read as text: ${swallowed}`)

  const timeout = catalogMessage({ error_code: 'query.timeout', params: { timeout_seconds: 45 } }, g.t, g.te)
  assert.ok(timeout, `${loc}: query.timeout has a line`)
  assert.ok(timeout.includes('45'), `${loc}: the timeout names its budget: ${timeout}`)

  assert.ok(catalogMessage({ error_code: 'entity.run_failed' }, g.t, g.te), `${loc}: entity.run_failed has a line`)
}

console.log(`useErrorMessage: ${codes.length} codes resolve (${dotted.length} dotted)`)

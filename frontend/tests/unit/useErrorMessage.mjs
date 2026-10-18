import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { createI18n } from 'vue-i18n'

import { errorCatalogKeys, wantsCatalogMessage } from '../../composables/useErrorMessage.ts'

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

// The catalog speaks only when the server did not: a bare code in `detail`
// (AppError without a message) or none at all. A sentence the server wrote
// for the case stays, being more specific than the code's generic text.
assert.equal(wantsCatalogMessage('membership.not_found', 'membership.not_found'), true)
assert.equal(wantsCatalogMessage('membership.not_found', undefined), true)
assert.equal(wantsCatalogMessage('membership.not_found', '  '), true)
assert.equal(wantsCatalogMessage('resource.conflict', 'This change moved since you viewed it — refresh and try again.'), false)
assert.equal(wantsCatalogMessage(undefined, 'anything'), false)

console.log(`useErrorMessage: ${codes.length} codes resolve (${dotted.length} dotted)`)

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { baseCompile } from '@intlify/message-compiler'

// vue-i18n compiles each catalog string as message syntax: `@` starts a
// linked message, `{` an interpolation, `|` a plural. A bare `@` in an email
// example ("support@example.com") fails to compile. Dev logs and falls back,
// but the production build throws mid-render, and the component holding the
// string renders nothing. The Exchange connection form went blank that way.
// Write a literal `@` as {'@'}.

const LOCALES = ['en', 'es', 'he']

function* strings(node, path = []) {
  if (typeof node === 'string') yield [path.join('.'), node]
  else if (node && typeof node === 'object') {
    for (const [k, v] of Object.entries(node)) yield* strings(v, [...path, k])
  }
}

const failures = []
for (const locale of LOCALES) {
  const catalog = JSON.parse(readFileSync(new URL(`../../../locales/${locale}.json`, import.meta.url)))
  for (const [key, message] of strings(catalog)) {
    const errors = []
    baseCompile(message, { onError: (e) => errors.push(e.message) })
    if (errors.length) failures.push(`${locale}:${key}: ${errors[0]}  ->  ${message.slice(0, 80)}`)
  }
}

assert.deepEqual(failures, [], `catalog messages that vue-i18n cannot compile:\n${failures.join('\n')}`)

// The guard actually catches the regression.
const caught = []
baseCompile('e.g. support@example.com', { onError: (e) => caught.push(e) })
assert.ok(caught.length > 0, 'a bare @ is reported')
const escaped = []
baseCompile("e.g. support{'@'}example.com", { onError: (e) => escaped.push(e) })
assert.equal(escaped.length, 0, "{'@'} compiles")

console.log('locale catalogs compile')

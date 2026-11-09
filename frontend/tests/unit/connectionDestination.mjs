import assert from 'node:assert/strict'
import test from 'node:test'

import {
  CREDENTIALS_REQUIRED_CODE,
  changedDestinationFields,
  needsCredentialReentry,
} from '../../utils/connectionDestination.ts'

test('a changed host, port or url is reported', () => {
  assert.deepEqual(changedDestinationFields({ host: 'a.example.com', port: 5432 }, { host: 'b.example.com', port: 5432 }), ['host'])
  assert.deepEqual(changedDestinationFields({ port: 5432 }, { port: 6543 }), ['port'])
  assert.deepEqual(changedDestinationFields({ base_url: 'https://a.example.com' }, { base_url: 'https://b.example.com' }), ['base_url'])
})

test('case and trailing slashes are not a change', () => {
  assert.deepEqual(changedDestinationFields({ host: 'DB.example.com/' }, { host: 'db.example.com' }), [])
  assert.deepEqual(changedDestinationFields({ server_url: 'https://a.example.com/' }, { server_url: 'https://a.example.com' }), [])
})

test('non-destination fields are not reported', () => {
  assert.deepEqual(changedDestinationFields({ database: 'a', schema: 'x' }, { database: 'b', schema: 'y' }), [])
})

test('the server refusal is recognized on both error shapes', () => {
  assert.equal(needsCredentialReentry({ data: { error_code: CREDENTIALS_REQUIRED_CODE } }), true)
  assert.equal(needsCredentialReentry({ response: { _data: { error_code: CREDENTIALS_REQUIRED_CODE } } }), true)
  assert.equal(needsCredentialReentry({ data: { error_code: 'connection.admin_required' } }), false)
  assert.equal(needsCredentialReentry({}), false)
  assert.equal(needsCredentialReentry(null), false)
})

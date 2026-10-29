// Embedded-artifact session lifecycle (utils/embedSession.ts):
// expiry is UTC whatever the browser's zone, a logout overtakes an in-flight
// sign-in, and a renewed token for the same person keeps the runtime connected.
import assert from 'node:assert/strict'
import { createAuthGuard, expiryMs, renewDelay, runtimeIdentity } from '../../utils/embedSession.ts'

const issued = Date.parse('2026-10-29T10:00:00Z'), lead = 5 * 60 * 1000
for (const wire of ['2026-10-29T11:00:00', '2026-10-29T11:00:00Z', '2026-10-29T11:00:00+00:00', '2026-10-29T13:00:00+02:00']) {
  assert.equal(expiryMs(wire), Date.parse('2026-10-29T11:00:00Z'), wire)
  assert.equal(renewDelay(wire, issued, lead), 55 * 60 * 1000, wire)
}
assert.equal(renewDelay('2026-10-29T10:01:00Z', issued, lead), 15_000)   // never a tight loop
assert.equal(renewDelay('not a date', issued, lead), 15_000)
console.log(`PASS: expiry is read as UTC (TZ=${process.env.TZ || 'system'}), renewal ${lead / 60000} min ahead`)

const auth = createAuthGuard()
const attempt = auth.ticket()
auth.invalidate()                     // logout while the host or popup answers
assert.equal(auth.isCurrent(attempt), false)
assert.equal(auth.isCurrent(auth.ticket()), true)
console.log('PASS: a logout makes every in-flight sign-in stale')

const alice = { hostApplication: 'host_a', organizationId: 'org', userId: 'u-alice', token: 'bow_embed_1' }
const renewed = { ...alice, token: 'bow_embed_2' }
assert.equal(runtimeIdentity(alice, null), runtimeIdentity(renewed, null))
assert.notEqual(runtimeIdentity(alice, null), runtimeIdentity({ ...alice, userId: 'u-bob' }, null))
assert.notEqual(runtimeIdentity(alice, null), runtimeIdentity(null, null))
assert.equal(runtimeIdentity(null, 'Bearer a'), runtimeIdentity(null, 'Bearer b'))
assert.notEqual(runtimeIdentity(null, 'Bearer a'), runtimeIdentity(null, ''))
console.log('PASS: renewal keeps the runtime identity; another person or signing out changes it')

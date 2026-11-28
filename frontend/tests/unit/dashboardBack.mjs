import assert from 'node:assert/strict'

import { backHref, backTarget, keepBack, withBack } from '../../utils/dashboardBack.ts'

// A member returns to the list they opened the dashboard from.
assert.deepEqual(backTarget('apps', false), { origin: 'apps', path: '/apps' })
assert.deepEqual(backTarget('dashboards', false), { origin: 'dashboards', path: '/dashboards' })
assert.deepEqual(backTarget(['dashboards'], false), { origin: 'dashboards', path: '/dashboards' }, 'a repeated param')

// A direct link (no origin), or one naming no known list, goes home; the
// parameter is never a redirect target.
for (const raw of [undefined, null, '', 'home', '/settings', 'https://evil.example', '//evil.example', 'toString', ['x']]) {
  assert.deepEqual(backTarget(raw, false), { origin: 'home', path: '/' }, String(raw))
}

// An app user has only the launcher, wherever the link says they came from.
for (const raw of [undefined, 'apps', 'dashboards', 'https://evil.example']) {
  assert.deepEqual(backTarget(raw, true), { origin: 'apps', path: '/apps' }, String(raw))
}

// The links that name their origin, and redirects that carry it over.
assert.equal(withBack('/d/sales', 'apps'), '/d/sales?back=apps')
assert.equal(withBack('/d/sales?from=draft', 'dashboards'), '/d/sales?from=draft&back=dashboards')
assert.equal(keepBack('/d/sales/draft', 'dashboards'), '/d/sales/draft?back=dashboards')
assert.equal(keepBack('/d/sales/draft', '/settings'), '/d/sales/draft', 'an unknown origin is dropped')
assert.equal(keepBack('/d/sales/draft', undefined), '/d/sales/draft')

// The href: the exact page the reader left (its filters) when it is the
// target list, never another page the router happens to remember.
const dashboards = backTarget('dashboards', false)
assert.equal(backHref(dashboards, '/dashboards?tab=shared&q=sales'), '/dashboards?tab=shared&q=sales')
assert.equal(backHref(dashboards, '/dashboards#top'), '/dashboards#top')
assert.equal(backHref(dashboards, '/dashboards-old'), '/dashboards')
assert.equal(backHref(dashboards, '/settings/members'), '/dashboards')
assert.equal(backHref(dashboards, null), '/dashboards', 'a reload with no history')
assert.equal(backHref(backTarget('apps', true), '/apps?kind=doc'), '/apps?kind=doc')
assert.equal(backHref(backTarget(undefined, false), '/dashboards'), '/', 'home is home')

console.log('dashboardBack: ok')

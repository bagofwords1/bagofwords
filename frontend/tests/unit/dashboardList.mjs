import assert from 'node:assert/strict'

import {
  DEFAULT_STATE,
  SORTS,
  PRESETS,
  STATUSES,
  ACCESS_CHIPS,
  TYPES,
  parseListQuery,
  toRouteQuery,
  zonedStartOfDay,
  presetRange,
  toApiParams,
  activeFilterCount,
  accessLabel,
  accessTitleParts,
  statusPill,
  cardMeta,
  mergeStarState,
} from '../../utils/dashboardList.ts'

// The dashboards page keeps its whole state in the URL (a bookmark, Back and
// Forward), and turns it into GET /api/artifacts params. These are the pure
// pieces; the page only wires them to the router and the fetch.

const NOW = new Date('2026-11-05T10:30:00Z')
// Agent and owner ids are UUIDs (the API answers 422 to anything else).
const A1 = '0b6a1f2e-3c4d-4e5f-8a9b-0c1d2e3f4a51'
const A2 = '0b6a1f2e-3c4d-4e5f-8a9b-0c1d2e3f4a52'
const U9 = '7d3c2b1a-0f9e-4d8c-b7a6-5e4d3c2b1a09'
const uuid = n => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`

// --- vocabularies -------------------------------------------------------------

assert.deepEqual(SORTS, ['edited', 'created', '-created', 'title', 'published'])
assert.deepEqual(PRESETS, ['any', '7', '30', '90', 'year', 'older90', 'custom'])
assert.deepEqual(STATUSES, ['any', 'published', 'changes', 'draft'])
assert.deepEqual(ACCESS_CHIPS, ['all', 'direct', 'group', 'project', 'org', 'edit'])
assert.deepEqual(TYPES, ['all', 'page', 'doc'])

// --- URL: parse and serialize -------------------------------------------------

// The default state is the bare /dashboards URL.
assert.deepEqual(toRouteQuery(DEFAULT_STATE), {})
assert.deepEqual(parseListQuery({}), DEFAULT_STATE)

// A round trip keeps every key, and the URL is what the page wrote.
const full = {
  tab: 'shared',
  q: 'revenue',
  type: 'doc',
  access: 'group',
  agents: [A1, A2],
  owners: [U9],
  created: 'custom',
  createdFrom: '2026-01-01',
  createdTo: '2026-03-31',
  edited: '30',
  editedFrom: '',
  editedTo: '',
  status: 'any',
  starred: true,
  attention: true,
  sort: 'published',
  page: 3,
}
const fullQuery = toRouteQuery(full)
assert.deepEqual(fullQuery, {
  tab: 'shared',
  q: 'revenue',
  type: 'doc',
  access: 'group',
  agents: `${A1},${A2}`,
  owners: U9,
  created_from: '2026-01-01',
  created_to: '2026-03-31',
  edited: '30',
  starred: '1',
  attention: '1',
  sort: 'published',
  page: '3',
})
assert.deepEqual(parseListQuery(fullQuery), full, 'parse(serialize(state)) is the state')
const mine = { ...DEFAULT_STATE, status: 'changes', edited: 'year', sort: 'title' }
assert.deepEqual(parseListQuery(toRouteQuery(mine)), mine)

// A custom range with one end only.
const onlyTo = { ...DEFAULT_STATE, edited: 'custom', editedTo: '2026-02-01' }
assert.deepEqual(toRouteQuery(onlyTo), { edited_to: '2026-02-01' })
assert.deepEqual(parseListQuery(toRouteQuery(onlyTo)), onlyTo)

// Unknown values are dropped, each on its own: the rest of the URL still holds.
assert.deepEqual(parseListQuery({
  tab: 'everyone',
  q: 'x',
  type: 'slides-and-more',
  access: 'friends',
  agents: `,${A1},,${A1},`,
  owners: '',
  created: '45',
  edited: 'year',
  edited_from: '2026-02-30',
  status: 'archived',
  starred: 'maybe',
  attention: '0',
  sort: 'random',
  page: '-2',
  bogus: 'value',
}), { ...DEFAULT_STATE, q: 'x', agents: [A1], edited: 'year' })
// Ids: only UUID-shaped ones are kept (lowercased, as the API stores them),
// one by one; the API would 422 the whole list for one bad id.
assert.deepEqual(parseListQuery({ agents: `${A1},not-an-id,${A2.toUpperCase()},${A2}`, owners: `u9,${U9}` }),
  { ...DEFAULT_STATE, agents: [A1, A2], owners: [U9] })
assert.deepEqual(parseListQuery({ agents: `{${A1}}`, owners: A1.replaceAll('-', '') }), DEFAULT_STATE,
  'braced or unhyphenated forms are not the canonical id')
// At most 100 ids each (the API's cap).
{
  const many = Array.from({ length: 150 }, (_, i) => uuid(i + 1))
  const parsed = parseListQuery({ agents: many.join(','), owners: many.join(',') })
  assert.equal(parsed.agents.length, 100)
  assert.equal(parsed.agents[99], '00000000-0000-4000-8000-000000000100')
  assert.equal(parsed.owners.length, 100)
}
// A custom range whose start is after its end is dropped, both ends; the
// other range and the rest of the URL still hold. A one-day range (from = to)
// is a valid range: the whole day.
assert.deepEqual(parseListQuery({ created_from: '2026-03-31', created_to: '2026-03-01', edited_to: '2026-02-01', q: 'x' }),
  { ...DEFAULT_STATE, q: 'x', edited: 'custom', editedTo: '2026-02-01' })
assert.deepEqual(parseListQuery({ edited: 'custom', edited_from: '2026-03-31', edited_to: '2026-03-01' }), DEFAULT_STATE)
assert.deepEqual(parseListQuery({ created_from: '2026-03-01', created_to: '2026-03-01' }),
  { ...DEFAULT_STATE, created: 'custom', createdFrom: '2026-03-01', createdTo: '2026-03-01' })
// Year 9999 is out: its last day has no next day for the exclusive `to`.
assert.deepEqual(parseListQuery({ created_to: '9999-12-31' }), DEFAULT_STATE)
assert.deepEqual(parseListQuery({ created_from: '9998-12-31' }),
  { ...DEFAULT_STATE, created: 'custom', createdFrom: '9998-12-31' })
// Search: at most 200 characters (the API's max_length, which counts code
// points), cut on a code point, never inside a surrogate pair.
assert.equal(parseListQuery({ q: 'x'.repeat(250) }).q, 'x'.repeat(200))
{
  const q = parseListQuery({ q: `${'a'.repeat(199)}😀b` }).q
  assert.equal(q, `${'a'.repeat(199)}😀`, 'the emoji at code point 200 is kept whole')
  assert.equal(Array.from(q).length, 200)
}
assert.equal(parseListQuery({ q: `${'a'.repeat(200)}😀` }).q, 'a'.repeat(200), 'past the cap, the whole emoji goes')
// A preset name wins over stray dates; impossible calendar dates are dropped.
assert.deepEqual(parseListQuery({ created: '7', created_from: '2026-01-01' }),
  { ...DEFAULT_STATE, created: '7' })
assert.deepEqual(parseListQuery({ created_from: '2026-13-01', created_to: 'soon' }), DEFAULT_STATE)
// A repeated key (?tab=shared&tab=my) reads its first value.
assert.equal(parseListQuery({ tab: ['shared', 'my'] }).tab, 'shared')
// Page: a positive integer only.
assert.equal(parseListQuery({ page: '2.5' }).page, 1)
assert.equal(parseListQuery({ page: '0' }).page, 1)
assert.equal(parseListQuery({ page: '12' }).page, 12)
assert.equal(parseListQuery({ page: '999999' }).page, 999999)
for (const page of ['1e3', '0x10', ' 3', '3 ', '+3', '1000000', '']) {
  assert.equal(parseListQuery({ page }).page, 1, `page ${JSON.stringify(page)} is dropped`)
}
// "Recently published" is a Shared sort: on My it falls back to the default.
assert.equal(parseListQuery({ sort: 'published' }).sort, 'edited')
assert.equal(parseListQuery({ tab: 'shared', sort: 'published' }).sort, 'published')
// starred=true is read as well as starred=1.
assert.equal(parseListQuery({ starred: 'true' }).starred, true)

// --- zonedStartOfDay: 00:00 of a calendar day in a time zone -----------------

assert.equal(zonedStartOfDay('2026-07-01', 'UTC').toISOString(), '2026-07-01T00:00:00.000Z')
assert.equal(zonedStartOfDay('2026-07-01', 'Asia/Jerusalem').toISOString(), '2026-06-30T21:00:00.000Z')
assert.equal(zonedStartOfDay('2026-07-01', 'America/New_York').toISOString(), '2026-07-01T04:00:00.000Z')
assert.equal(zonedStartOfDay('2026-01-15', 'Asia/Kolkata').toISOString(), '2026-01-14T18:30:00.000Z')
// DST-change days: the offset of local midnight, not of noon or of UTC midnight.
assert.equal(zonedStartOfDay('2026-03-29', 'Europe/Berlin').toISOString(), '2026-03-28T23:00:00.000Z',
  'Berlin springs forward at 02:00 that day; 00:00 is still CET (+1)')
assert.equal(zonedStartOfDay('2026-03-30', 'Europe/Berlin').toISOString(), '2026-03-29T22:00:00.000Z',
  'the day after: CEST (+2)')
assert.equal(zonedStartOfDay('2026-11-01', 'America/New_York').toISOString(), '2026-11-01T04:00:00.000Z',
  'New York falls back at 02:00 that day; 00:00 is still EDT (-4)')
assert.equal(zonedStartOfDay('2026-11-02', 'America/New_York').toISOString(), '2026-11-02T05:00:00.000Z',
  'the day after: EST (-5)')
assert.equal(zonedStartOfDay('2026-03-08', 'America/New_York').toISOString(), '2026-03-08T05:00:00.000Z')
// Israel springs forward at 02:00 on 2026-03-27, which is 00:00 UTC: the
// offset at UTC midnight (+3) is not the offset at local midnight (+2). Only
// the re-check gets this one right.
assert.equal(zonedStartOfDay('2026-03-27', 'Asia/Jerusalem').toISOString(), '2026-03-26T22:00:00.000Z')
// Chile springs forward at 00:00 on the first Sunday of September (2026-09-06):
// 00:00 does not exist there, the day starts at 01:00 -03, which is 04:00 UTC
// (the instant Saturday's 24:00 -04 would have been). 03:00 UTC is still
// Saturday 23:00 -04, so it must not be the answer.
assert.equal(zonedStartOfDay('2026-09-06', 'America/Santiago').toISOString(), '2026-09-06T04:00:00.000Z')
assert.equal(zonedStartOfDay('2026-09-07', 'America/Santiago').toISOString(), '2026-09-07T03:00:00.000Z', 'the day after: -03')
assert.equal(zonedStartOfDay('2026-09-05', 'America/Santiago').toISOString(), '2026-09-05T04:00:00.000Z', 'the day before: -04')
// Israel falls back at 02:00 on 2026-10-25 (23:00 UTC the day before). Both
// candidates land on the 25th: 01:00 IDT (by the +2 of UTC midnight) and
// 00:00 IDT (by the +3 re-check). The day starts at the earlier one.
assert.equal(zonedStartOfDay('2026-10-25', 'Asia/Jerusalem').toISOString(), '2026-10-24T21:00:00.000Z')
// Not a calendar date: null, never an Invalid Date (whose toISOString throws).
assert.equal(zonedStartOfDay('garbage', 'UTC'), null)
assert.equal(zonedStartOfDay('2026-02-30', 'UTC'), null)
assert.equal(zonedStartOfDay('9999-12-31', 'UTC'), null)
// No time zone: the runtime's own, as `new Date(y, m, d)` gives.
assert.equal(zonedStartOfDay('2026-03-29').getTime(), new Date(2026, 2, 29).getTime())
assert.equal(zonedStartOfDay('2026-11-01', undefined).getTime(), new Date(2026, 10, 1).getTime())

// --- presetRange ---------------------------------------------------------------

assert.deepEqual(presetRange('any', NOW, {}), {})
// Rolling windows: now minus N × 24 h; no `to` (up to now).
assert.deepEqual(presetRange('7', NOW, {}), { from: '2026-10-29T10:30:00.000Z' })
assert.deepEqual(presetRange('30', NOW, { timeZone: 'Asia/Jerusalem' }), { from: '2026-10-06T10:30:00.000Z' })
assert.deepEqual(presetRange('90', NOW, {}), { from: '2026-08-07T10:30:00.000Z' })
// "Older than 90 days" gives only `to`, and it is exclusive: it is the same
// instant "Last 90 days" starts at, so the two never share a dashboard.
assert.deepEqual(presetRange('older90', NOW, {}), { to: '2026-08-07T10:30:00.000Z' })
assert.equal(presetRange('older90', NOW, {}).to, presetRange('90', NOW, {}).from)
// "This year": Jan 1, 00:00 in the given zone. Two zones, two instants.
assert.deepEqual(presetRange('year', NOW, { timeZone: 'Asia/Jerusalem' }), { from: '2025-12-31T22:00:00.000Z' })
assert.deepEqual(presetRange('year', NOW, { timeZone: 'America/New_York' }), { from: '2026-01-01T05:00:00.000Z' })
assert.deepEqual(presetRange('year', NOW, {}), { from: new Date(2026, 0, 1).toISOString() })
// ...and the year is the zone's: at 23:30 UTC on Dec 31 it is already the new
// year in Jerusalem, still the old one in New York.
const newYearsEve = new Date('2026-12-31T23:30:00Z')
assert.deepEqual(presetRange('year', newYearsEve, { timeZone: 'Asia/Jerusalem' }), { from: '2026-12-31T22:00:00.000Z' })
assert.deepEqual(presetRange('year', newYearsEve, { timeZone: 'America/New_York' }), { from: '2026-01-01T05:00:00.000Z' })
// Custom: `from` is 00:00 of its day; `to` is 00:00 of the NEXT day, so the
// picked day is included whole (the API's `*_to` is exclusive).
assert.deepEqual(presetRange('custom', NOW, { from: '2026-03-01', to: '2026-03-31', timeZone: 'Asia/Jerusalem' }),
  { from: '2026-02-28T22:00:00.000Z', to: '2026-03-31T21:00:00.000Z' })
assert.deepEqual(presetRange('custom', NOW, { from: '2026-10-31', to: '2026-10-31', timeZone: 'America/New_York' }),
  { from: '2026-10-31T04:00:00.000Z', to: '2026-11-01T04:00:00.000Z' },
  'one day: from its 00:00 to the next 00:00')
// The custom `to` crosses month and year ends, and the DST day keeps 25 hours.
assert.deepEqual(presetRange('custom', NOW, { to: '2026-12-31', timeZone: 'UTC' }), { to: '2027-01-01T00:00:00.000Z' })
assert.deepEqual(presetRange('custom', NOW, { from: '2026-11-01', to: '2026-11-01', timeZone: 'America/New_York' }),
  { from: '2026-11-01T04:00:00.000Z', to: '2026-11-02T05:00:00.000Z' })
// An instant late on the `to` day is inside; the next day's first instant is not.
{
  const { to } = presetRange('custom', NOW, { to: '2026-03-31', timeZone: 'Asia/Jerusalem' })
  assert.ok(new Date('2026-03-31T20:59:59Z') < new Date(to), '23:59:59 Jerusalem time on Mar 31 is included')
  assert.ok(!(new Date('2026-03-31T21:00:00Z') < new Date(to)), '00:00 on Apr 1 is not')
}
// A custom range with no dates (still being picked) filters nothing.
assert.deepEqual(presetRange('custom', NOW, {}), {})
assert.deepEqual(presetRange('custom', NOW, { from: 'garbage' }), {})
// A date the URL parser would drop never throws here either.
assert.deepEqual(presetRange('custom', NOW, { from: '2026-01-01', to: '9999-12-31', timeZone: 'UTC' }),
  { from: '2026-01-01T00:00:00.000Z' })

// --- toApiParams ---------------------------------------------------------------

assert.deepEqual(toApiParams(DEFAULT_STATE, NOW, { view: 'grid' }),
  { filter: 'my', page: 1, limit: 15, sort: 'edited' })
assert.equal(toApiParams(DEFAULT_STATE, NOW, { view: 'list' }).limit, 50)
{
  const p = toApiParams({ ...full, status: 'draft' }, NOW, { view: 'list', newSince: '2026-11-01T00:00:00Z', timeZone: 'UTC' })
  assert.deepEqual(p, {
    filter: 'shared',
    page: 3,
    limit: 50,
    sort: 'published',
    search: 'revenue',
    mode: 'doc',
    access: 'group',
    agent_ids: `${A1},${A2}`,
    owner_ids: U9,
    created_from: '2026-01-01T00:00:00.000Z',
    created_to: '2026-04-01T00:00:00.000Z',
    edited_from: '2026-10-06T10:30:00.000Z',
    starred: true,
    attention: true,
    new_since: '2026-11-01T00:00:00Z',
  })
  assert.ok(!('status' in p), 'no status on Shared: the API ignores it there')
}
{
  // On My: the status goes out; the access chip and the owners do not, and
  // "new" has no meaning.
  const p = toApiParams({ ...full, tab: 'my', status: 'changes', sort: 'title' }, NOW,
    { view: 'grid', newSince: '2026-11-01T00:00:00Z', timeZone: 'UTC' })
  assert.equal(p.status, 'changes')
  assert.equal(p.filter, 'my')
  assert.ok(!('access' in p), 'no access on My')
  assert.ok(!('owner_ids' in p), 'no owners on My')
  assert.ok(!('new_since' in p), 'no new_since on My')
}
// Blank search is not sent; "any" status is not sent.
assert.ok(!('search' in toApiParams({ ...DEFAULT_STATE, q: '   ' }, NOW, { view: 'grid' })))
assert.ok(!('status' in toApiParams({ ...DEFAULT_STATE, status: 'any' }, NOW, { view: 'grid' })))
// Search is trimmed.
assert.equal(toApiParams({ ...DEFAULT_STATE, q: ' sales ' }, NOW, { view: 'grid' }).search, 'sales')
// The time zone reaches the date params.
assert.equal(toApiParams({ ...DEFAULT_STATE, edited: 'year' }, NOW, { view: 'grid', timeZone: 'Asia/Jerusalem' }).edited_from,
  '2025-12-31T22:00:00.000Z')

// --- activeFilterCount (the funnel badge) --------------------------------------

assert.equal(activeFilterCount(DEFAULT_STATE), 0)
assert.equal(activeFilterCount({ ...DEFAULT_STATE, agents: ['a', 'b'], edited: '7', starred: true, status: 'draft' }), 5)
// Owners and the access chip are Shared filters; status is a My filter.
assert.equal(activeFilterCount({ ...DEFAULT_STATE, owners: ['u1'], status: 'draft' }), 1)
assert.equal(activeFilterCount({ ...DEFAULT_STATE, tab: 'shared', owners: ['u1'], status: 'draft', access: 'org' }), 1)
// Search, type and sort are not funnel filters; a custom range without dates is not one either.
assert.equal(activeFilterCount({ ...DEFAULT_STATE, q: 'x', type: 'doc', sort: 'title', created: 'custom' }), 0)
assert.equal(activeFilterCount({ ...DEFAULT_STATE, created: 'custom', createdFrom: '2026-01-01' }), 1)

// --- the reason label ------------------------------------------------------------

const card = (over = {}) => ({
  id: 'd1',
  title: 'Weekly revenue',
  mode: 'page',
  published: true,
  role: 'view',
  owner: { id: 'u2', name: 'Dana Levi' },
  visibility: 'shared',
  access: { reasons: ['direct'], primary: 'direct', via_name: null },
  is_new: false,
  unpublished_changes: null,
  ...over,
})

assert.deepEqual(accessLabel(card()),
  { key: 'dashboards.reason.direct', params: {}, icon: 'i-heroicons-user' })
assert.deepEqual(accessLabel(card({ access: { reasons: ['group', 'org'], primary: 'group', via_name: 'Finance team' } })),
  { key: 'dashboards.reason.group', params: { name: 'Finance team' }, icon: 'i-heroicons-user-group' })
assert.deepEqual(accessLabel(card({ access: { reasons: ['project'], primary: 'project', via_name: 'Board' } })),
  { key: 'dashboards.reason.project', params: { name: 'Board' }, icon: 'i-heroicons-folder' })
assert.deepEqual(accessLabel(card({ visibility: 'internal', access: { reasons: ['org'], primary: 'org', via_name: null } })),
  { key: 'dashboards.reason.org', params: {}, icon: 'i-heroicons-building-office' })
assert.deepEqual(accessLabel(card({ visibility: 'public', access: { reasons: ['org'], primary: 'org', via_name: null } })),
  { key: 'dashboards.reason.public', params: {}, icon: 'i-heroicons-globe-alt' },
  'public vs org: anyone with the link')
// The label says only how it reached me, never whether I can edit (the pencil does).
assert.deepEqual(accessLabel(card({ role: 'edit' })), accessLabel(card({ role: 'view' })))
// The primary reason wins; without one, the first reason.
assert.equal(accessLabel(card({ access: { reasons: ['direct', 'org'], primary: null, via_name: null } })).key,
  'dashboards.reason.direct')
// My dashboards have no reason: no label.
assert.equal(accessLabel(card({ access: null })), null)
assert.equal(accessLabel(card({ access: { reasons: [], primary: null, via_name: null } })), null)

// The hover title: every reason, then what the reader may do.
assert.deepEqual(accessTitleParts(card({ role: 'edit', visibility: 'internal',
  access: { reasons: ['direct', 'org'], primary: 'direct', via_name: null } })), [
  { key: 'dashboards.reasonTitle.direct', params: { owner: 'Dana Levi' } },
  { key: 'dashboards.reasonTitle.org', params: {} },
  { key: 'dashboards.reasonTitle.canEdit', params: {} },
])
assert.deepEqual(accessTitleParts(card({ visibility: 'public',
  access: { reasons: ['group', 'project', 'org'], primary: 'group', via_name: 'Sales managers' } })), [
  { key: 'dashboards.reasonTitle.group', params: { name: 'Sales managers' } },
  // via_name names the primary reason's group or project only.
  { key: 'dashboards.reasonTitle.projectOther', params: {} },
  { key: 'dashboards.reasonTitle.public', params: {} },
  { key: 'dashboards.reasonTitle.viewOnly', params: {} },
])
assert.deepEqual(accessTitleParts(card({ access: { reasons: ['project', 'org'], primary: 'project', via_name: 'Board' } }))[0],
  { key: 'dashboards.reasonTitle.project', params: { name: 'Board' } })
assert.deepEqual(accessTitleParts(card({ access: { reasons: ['direct', 'group'], primary: 'direct', via_name: null } }))[1],
  { key: 'dashboards.reasonTitle.groupOther', params: {} })
assert.deepEqual(accessTitleParts(card({ access: null })), [])
// No owner name: "Shared with you", never " shared it with you".
for (const owner of [null, { id: 'u2', name: null }, { id: 'u2', name: '' }]) {
  assert.deepEqual(accessTitleParts(card({ owner }))[0], { key: 'dashboards.reason.direct', params: {} })
}

// --- the status pill ---------------------------------------------------------------

const my = (over = {}) => card({ role: 'owner', access: null, visibility: 'none', unpublished_changes: false, ...over })
assert.equal(statusPill(my({ published: false, unpublished_changes: false }), 'my'), null, 'a draft has the mode pill only')
assert.deepEqual(statusPill(my({ visibility: 'shared' }), 'my'), { key: 'dashboards.pill.published', tone: 'published' })
assert.deepEqual(statusPill(my({ visibility: 'internal' }), 'my'), { key: 'dashboards.pill.org', tone: 'org' })
assert.deepEqual(statusPill(my({ visibility: 'public' }), 'my'), { key: 'dashboards.pill.public', tone: 'public' })
assert.deepEqual(statusPill(my({ visibility: 'internal', unpublished_changes: true }), 'my'),
  { key: 'dashboards.pill.changes', tone: 'changes' }, 'changes win over where it is published')
// The API leaves unpublished_changes null for a view-only reader: no "changes"
// pill can come from a null, on either tab.
assert.deepEqual(statusPill(my({ visibility: 'shared', unpublished_changes: null }), 'my'),
  { key: 'dashboards.pill.published', tone: 'published' })
assert.equal(statusPill(card({ unpublished_changes: null }), 'shared'), null)
// On Shared: "New" first; an editor sees the changes; nothing else (the reason label says the rest).
assert.deepEqual(statusPill(card({ is_new: true, role: 'edit', unpublished_changes: true }), 'shared'),
  { key: 'dashboards.pill.new', tone: 'new' })
assert.deepEqual(statusPill(card({ role: 'edit', unpublished_changes: true }), 'shared'),
  { key: 'dashboards.pill.changes', tone: 'changes' })
assert.equal(statusPill(card({ role: 'edit', unpublished_changes: false }), 'shared'), null)

// --- the meta line ------------------------------------------------------------------

// My: Edited, Created. Shared: by owner, Edited; reached only through the
// organization: by owner, Published. A part without its value is left out
// ("by " with no name, "Created " with no date).
{
  const dated = { created_at: '2026-01-02T00:00:00Z', edited_at: '2026-03-04T00:00:00Z', published_at: '2026-03-05T00:00:00Z' }
  assert.deepEqual(cardMeta(my(dated), 'my'), ['edited', 'created'])
  assert.deepEqual(cardMeta(card(dated), 'shared'), ['by', 'edited'])
  const orgOnly = { ...dated, access: { reasons: ['org'], primary: 'org', via_name: null } }
  assert.deepEqual(cardMeta(card(orgOnly), 'shared'), ['by', 'published'])
  assert.deepEqual(cardMeta(my({ ...dated, created_at: null }), 'my'), ['edited'])
  // Edited falls back to the creation time.
  assert.deepEqual(cardMeta(my({ ...dated, edited_at: null }), 'my'), ['edited', 'created'])
  assert.deepEqual(cardMeta(my({ created_at: null, edited_at: null }), 'my'), [])
  assert.deepEqual(cardMeta(card({ ...dated, owner: null }), 'shared'), ['edited'])
  assert.deepEqual(cardMeta(card({ ...dated, owner: { id: 'u2', name: '' } }), 'shared'), ['edited'])
  assert.deepEqual(cardMeta(card({ ...orgOnly, owner: null, published_at: null }), 'shared'), [])
}

// --- the star, while a toggle is in flight --------------------------------------------

// A refetch that lands while a star request is in flight keeps that card's
// local star (the user's latest click); every other card takes the server's.
{
  const local = [card({ id: 'd1', is_starred: true }), card({ id: 'd2', is_starred: false })]
  const fresh = [card({ id: 'd2', is_starred: true }), card({ id: 'd1', is_starred: false }), card({ id: 'd3', is_starred: false })]
  const merged = mergeStarState(fresh, new Set(['d1', 'd3']), local)
  assert.deepEqual(merged.map(c => [c.id, c.is_starred]), [['d2', true], ['d1', true], ['d3', false]],
    'd1 pending: local; d2 not pending: server; d3 pending but not shown before: server')
  assert.equal(fresh[1].is_starred, false, 'the response is not mutated')
  assert.deepEqual(mergeStarState(fresh, new Set(), local).map(c => c.is_starred), [true, false, false])
}

console.log('dashboardList: ok')

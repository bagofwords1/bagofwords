# Design — "Open changes" badge + review-center modal (Agents page)

A top-of-page badge on the Agents page that opens a modal listing **all open
instruction changes** the caller can see, grouped/filterable by **agent (file),
date, and author**, scoped to the caller's access, and scalable to thousands of
open changes.

Status: design only — not implemented.

---

## TL;DR

Most of this already exists and is wired up but disabled. The work is **enable +
extend**, not build-new, plus **one decision** about the source of truth.

- Badge: re-enable the stubbed amber badge already in `KnowledgeExplorer.vue`.
- Modal: the `ReviewFeed.vue` component + `/api/review*` endpoints already list,
  filter (agent/status/type/search), group, and act (read / dismiss / resolve)
  on `ReviewItem` rows, already access-scoped per user.
- Gaps to close: date/author grouping + filters, real pagination, and reconciling
  the review inbox with the per-hunk "pending" set used by the tree dots.

---

## What already exists (don't rebuild)

**Badge (disabled).** `frontend/components/KnowledgeExplorer.vue:10`
```html
<button v-if="false && reviewCount > 0" ... @click="openReview(null)">
  <span ...></span>{{ $t('agentsPage.toReview', { n: reviewCount }) }}
</button>
```
`reviewCount` comes from `GET /api/review/count` (`{ open: N }`,
`KnowledgeExplorer.vue:1270`). `openReview(null)` already opens the review view.

**Modal/feed.** `frontend/components/ReviewFeed.vue` (355 lines) — fetches
`GET /api/review`, groups rows, and supports read / dismiss / resolve and an
agent filter. Mounted at `KnowledgeExplorer.vue:216`.

**Backend.** `backend/app/routes/review.py`:
- `GET /review` → `review_service.list_items(...)` with `agent_id`, `status`,
  `type`, `severity`, `search` filters.
- `GET /review/count` → `count_open(...)`.
- `POST /review/read-all`, `/review/{id}/read|dismiss|resolve`.

**Model.** `backend/app/models/review_item.py` — `ReviewItem` with:
- `type` (`instruction_suggestion` = an open instruction change; also
  `low_confidence`, `slow_query`, `schema_changed`, …),
- `status` (open / in_progress / snoozed / resolved / dismissed),
- `data_source_id` (the agent → our "file/agent" axis),
- `created_at` / `updated_at` (the "date" axis),
- `payload` (polymorphic pointer: `{instruction_id, build_id, completion_id, …}`),
- `why` (human-readable reason).

**Access scoping (already correct).** `review_service.list_items` calls
`_visible(db, org, user) → (is_admin, ds_ids)` and filters
`ReviewItem.data_source_id.in_(ds_ids)` for non-admins (admins see all). This is
exactly the "my instructions I have access to" requirement, and it already has
the admin "see everything" mode (parallel to the agents "Show all" toggle).

**Producer.** `review_producers.emit_instruction_suggestions_for_build(...)`
(called from `instruction_service.py:3601` on build creation) upserts an
`instruction_suggestion` `ReviewItem` per pending suggestion build.

---

## The one real decision: source of truth

There are **two populations of "open change"** today and they are not guaranteed
to be the same set:

1. **Per-hunk pending set** — `get_pending_change_instruction_ids` (the authoritative
   rule: a suggestion build counts only if it yields a *live* hunk vs current
   main). This drives the **tree dots** and `counts.pending_total`.
2. **`instruction_suggestion` ReviewItems** — emitted when a suggestion build is
   created; drives `/review/count` and the feed. These can drift from (1): an
   item can linger after its hunk is already applied/covered, or a build created
   outside the producer path may have no item.

**The badge and the modal must agree with the tree dots**, or users see "3
pending" in the tree and "5 to review" in the badge. Pick one:

- **Recommended:** make the per-hunk set authoritative for the
  `instruction_suggestion` slice. On read, reconcile `ReviewItem` rows against
  `get_pending_change_instruction_ids` (now ~1.9s for ~7.6k pending after the
  recent fix; cache it per request) — hide/auto-resolve items whose change is no
  longer live, and surface any live-but-itemless build. Keeps the rich inbox UX
  (read/dismiss/snooze/assignee) while staying consistent with the tree.
- Alternative: treat ReviewItems as the sole truth and switch the tree dots to
  read from them too. Simpler to keep consistent, but loses the precise
  "is there actually a live hunk" semantics the per-hunk model gives.

This choice should be made before building grouping/pagination on top.

---

## Endpoint shape

Extend the existing `GET /api/review` rather than add a parallel API.

```
GET /api/review
  ?type=instruction_suggestion          # default the modal to open changes
  &status=open,in_progress,snoozed      # active by default
  &agent_id=<id>                        # optional filter
  &author_id=<id>                       # NEW — "who"
  &since=<iso>&until=<iso>              # NEW — "dates"
  &group_by=agent|date|author           # NEW — server-applied grouping key
  &sort=updated_at|created_at           # default updated_at desc
  &search=<q>
  &cursor=<opaque>&limit=50             # NEW — cursor pagination (today: limit=200, no skip)
->
{
  "groups": [ { "key": "...", "label": "...", "count": N } ],   # for the chosen group_by
  "items": [ {
      "id", "type", "status", "why",
      "agent": {id, name}, "author": {id, name, source: user|ai|git},
      "created_at", "updated_at",
      "instruction": {id, title, snippet},
      "build_id"
  } ],
  "next_cursor": "..." | null,
  "total": N
}
```

Notes
- `author`/`source` already available: the suggestion build carries
  `created_by_user_id` + `source` (`user`/`ai`/`git`); the producer can denormalize
  these onto the `ReviewItem` (or join at read time) so "who" is one field.
- The **diff is loaded lazily** when a row is expanded, via the existing
  `GET /instructions/{id}/review-hunks` — never compute hunks for the whole list.
- `count_open` stays the badge source, but apply the same reconciliation as the
  list so badge == modal == tree.

---

## Component plan (frontend)

1. **Badge** — remove the `false &&` guard at `KnowledgeExplorer.vue:10`; keep
   `reviewCount` from `/review/count`. Place it in the page header next to
   "Connect Git / New".
2. **Modal** — reuse `ReviewFeed.vue`. Add:
   - a **group-by switch** (Agent / Date / Author) driving `group_by`,
   - **author** and **date-range** filters (it already has agent + status + search),
   - **pagination/virtualization** — today it renders the fetched list; switch to
     `cursor`+`limit` with "load more" or a virtual list (the tree itself isn't
     virtualized; same caution applies here at thousands of rows).
3. **Row → diff** — clicking a row opens the existing per-instruction review/diff
   view (`openInstructionFromReview` is already wired from the feed) with
   accept/reject hunks. No new diff UI.

---

## Scale plan

- **Don't** compute hunks for the list. Two-step, mirroring the perf fix:
  1. resolve the authoritative pending id set **once** and cache it
     (request-scope or short TTL) — used only for reconciliation/count;
  2. the list query is a plain indexed `ReviewItem` scan with the filters +
     **cursor pagination** (page-sized), returning denormalized metadata only.
- Group counts come from a `GROUP BY` (data_source_id / date_trunc / author),
  not Python aggregation over all rows.
- Lazy per-row diff via `review-hunks`.
- This keeps every interaction **O(page)**, independent of total backlog size.

Index check before building: `review_items(organization_id, type, status,
data_source_id)` and `(organization_id, type, status, updated_at)` for the
sort/paginate path (SQLite/Postgres don't auto-index these).

---

## Access control

Already handled by `_visible` (member data sources for non-admins, everything for
admins). Surface an **admin-only "All" toggle** in the modal header — identical
pattern and gating to the agents "Show all" — so a reviewer can triage the whole
org's backlog while members see only their accessible changes. No new permission
needed; reuse the governance perm that backs the agents toggle.

---

## Open question for product

"**by files**" — default grouping is by **agent** (the `data_source_id` an
instruction is attached to). True source *files* only exist for git/markdown-synced
instructions; for those, show the file path as a sub-label. Confirm whether
"files" means agents (assumed) or literally source files.

---

## Phasing

1. Reconcile source-of-truth + enable the badge (count consistent with tree).
2. Modal: author + date filters, group-by switch, cursor pagination.
3. Group counts via `GROUP BY`; add the missing indexes.
4. Admin "All" toggle; polish bulk actions (accept/reject/dismiss across a group).

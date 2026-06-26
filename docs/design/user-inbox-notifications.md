# User Inbox & Notifications — Design

Status: **draft** · Branch: `claude/user-inbox-notifications-0hcg42`

This proposes extracting the agent **Review** feed into a top-level **user inbox**
that unifies three notification sources, and lays out exactly what the backend
needs. The foundational delivery layer (model + migration) is included on this
branch; the service/API/frontend below are specced but **not yet implemented**.

---

## 1. The problem

Today there are two unconnected systems an inbox would need to merge:

| | `ReviewItem` (today) | What an inbox needs |
|---|---|---|
| Scoped to | **org + agent** (`data_source_id`) | **a user** |
| Read/dismiss state | **shared** (anyone with agent access clears it for everyone) | **per-user** (I dismiss mine, you keep yours) |
| Visibility rule | "manage permission on the agent" | "this was addressed to me" |
| Share/notify events | **not stored** — email-only, fire-and-forget | persisted, visible in-app |

`ReviewItem` is a **team triage queue**, not a personal inbox. Share events
aren't persisted at all. So the missing piece is a **per-recipient delivery
layer** — that's the whole proposal.

References:
- `backend/app/models/review_item.py` — team queue, shared state
- `backend/app/services/review_service.py` — `emit()`, triage, visibility
- `backend/app/routes/review.py` — `/review*` API
- `backend/app/routes/report.py:254` — `POST /reports/{id}/notify` (email-only today)
- `frontend/components/ReviewFeed.vue` — the list UI to generalize

---

## 2. Decision: notifications are the only surface; ReviewItem becomes an internal ledger

The review feed is **dropped as a UI** for now. The per-user inbox is the only
surface. The producers that detected conditions (low confidence, schema change,
slow query, query error, instruction suggestion) now deliver **per-user
notifications** to the same audience the feed used (the agent's managers).

To do this without rewriting five producers and their dedup/dismissal logic, the
fan-out hooks the single choke point they all call — `review_service.emit()`:

- `ReviewItem` stays as an **internal dedup / state ledger** (one ACTIVE per
  (org, agent, type, group_key); `respect_dismissal` / resurface still apply). It
  is no longer surfaced in any UI.
- On every create/bump, `emit()` calls `inbox_service.notify_agent_managers(...)`,
  delivering a per-user `Notification` (`source="review"`) to the agent managers.
- Share + in-report-tool paths call `inbox_service.notify_users(...)` directly.

Rejected alternatives: **generalize `ReviewItem`** into a user-scoped model
(fights its shared-state permission design); **repoint each producer** off
`emit()` (5+ call sites incl. `instruction_service`, loses the tested
dedup/resurface logic). Hooking `emit()` is one integration point and fully
reversible.

> **Inline actions** (the old *Run training* / *Run eval* buttons) are **not**
> carried onto notifications for now — they are informational + a deep-link to
> the agent. Admins still trigger training/eval from the agent page. The
> `/review` action routes remain but unsurfaced; porting actions onto
> notifications is a later, optional step.

> **Fully removing `ReviewItem`** (table + producers + `/review` routes) is a
> deliberate later cleanup, once the inbox proves out. Keeping it dormant now
> avoids a destructive change and preserves the dedup/auto-resolve machinery.

---

## 3. The delivery layer — `Notification` (implemented on this branch)

`backend/app/models/notification.py` + migration `notif0001_add_notifications.py`.

One row = one notification delivered to one user. It owns that user's
`read_at` / `dismissed_at`. Provenance is a soft `(source, source_id)` back-ref
(no hard FK). `group_key` dedups within a single user's inbox.

Key columns: `organization_id`, `user_id` (recipient), `actor_user_id` (who
caused it), `source` (`review|share|report_tool|schedule`), `type`, `severity`,
`title`, `body`, `link` (deep-link), `subject_json` (polymorphic pointer),
`source_id`, `group_key`, `read_at`, `dismissed_at`.

---

## 4. Backend wiring

### 4.1 `InboxService` — ✅ implemented (`app/services/inbox_service.py`)

> Named `inbox_service` to avoid collision with the existing email-dispatch
> `notification_service.py`.

- `notify_users(db, *, organization_id, user_ids, source, type, title, …, group_key, resurface_after_hours)`
  — deliver one row per user, deduped per (user, source, group_key); an active row
  is refreshed (re-surfaced unread, severity-escalated), a recently *dismissed*
  one is suppressed until `resurface_after_hours`. Never notifies the actor about
  their own action.
- `notify_agent_managers(db, *, data_source_id, …)` — resolves recipients (agent
  managers, or full admins when `data_source_id is None`) and fans out with
  `source="review"`.
- Reads: `list_for_user`, `count_unread`, `mark_read`, `mark_all_read`, `dismiss`
  — all scoped by `user_id == current_user`.

### 4.2 Fan-out from producers — ✅ implemented (hooked into `review_service.emit()`)

`emit()` (`review_service.py`) is the one choke point every producer already calls
(slow_query, low_confidence, schema_changed, instruction_suggestion, query_error,
incl. `instruction_service`). After it creates/bumps a `ReviewItem` with
`disposition == "notify"`, `_fanout_notification()` calls
`inbox_service.notify_agent_managers(...)` with `source="review"`,
`source_id=item.id`, `group_key=item.group_key`, `link=/agents/{ds}`,
`subject={"kind":"review_item", …}`. Wrapped in try/except — a delivery failure
never breaks emission.

**Reverse resolver — ✅ implemented:** `get_user_ids_with_permission(db, org_id,
"manage", data_source_id)` in `permission_resolver.py` (inverse of
`get_ds_ids_with_permission`). Enumerates org members and reuses the forward
resolver — O(members), fine for event-fired fan-out; revisit if it ever runs hot.
Global (`data_source_id=None`) → full admins only.

*Not done:* on review-item auto-resolve (`resolve_open_for`,
`resolve_for_instruction`) the fanned-out notifications are left as-is (low stakes
for informational rows). Could mark them read via `source_id` later — see §8.

### 4.3 Share path *(to build)*

In `POST /reports/{id}/notify` (`report.py:254`) and `ReportService.set_visibility`
(`report_service.py:104`): for every recipient that is a **known user**
(share-with-specific-users, and `notification_subscribers` entries of
`type=="user"`), also `notify_users(source="share", type=payload.type, ...,
actor_user_id=current_user.id, link=share_url, subject={"kind":"report",
"report_id":...})`. Email stays as-is for external addresses; the inbox row is
*additive*. This makes "shared with you" show up in-app, not just in an inbox.

### 4.4 In-report tool notification (the future piece) *(to build)*

Once 4.1 exists this is thin. A report run already resolves
`report → org + data_source + agents` (`review_producers._org_and_data_sources_for_report`).
Add a tool (MCP `notify_user` / internal helper) that calls
`notify_users(source="report_tool", type="report_tool", ...)` with recipients =
report owner + collaborators (`ReportShare`), `subject={"kind":"report",
"report_id":...}`, `link` to the report. Guard: only notify users who already
have access to that report (no arbitrary targeting), and dedup by
`group_key=f"report:{report_id}:{tool_key}"` to avoid a chatty agent spamming.

---

## 5. API — ✅ implemented (`app/routes/notification.py`, mounted in `main.py` after `review.router`)

Mirrors `/review*` so the frontend reuse is trivial:

```
GET  /api/notifications          ?source=&unread=&limit=    -> {items, total, unread}
GET  /api/notifications/count                                -> {unread}
POST /api/notifications/read-all                             -> {ok, marked}
POST /api/notifications/{id}/read     {read: bool}           -> {ok}
POST /api/notifications/{id}/dismiss                         -> {ok}
```

Every handler filters by `user_id == current_user.id`. No `organization`
permission gymnastics — ownership is the recipient.

---

## 6. Frontend *(to build)*

- **`InboxFeed.vue`** adapted from `ReviewFeed.vue` (~90% of the list UI: rows,
  severity colors, read/dismiss). Drive it off `/api/notifications`; the `source`
  filter narrows to `review` / `share` / `report_tool`.
- **New `/inbox` page** + a **bell with unread count** in `layouts/default.vue`
  (poll `/api/notifications/count`, same pattern KnowledgeExplorer uses for
  `/api/review/count`).
- Clicking a row navigates to `notification.link` and marks it read.
- **Remove the Agents "Review" panel** (`ReviewFeed.vue` in `KnowledgeExplorer.vue`)
  — the inbox replaces it. The `/review` API stays mounted but unsurfaced.

---

## 7. Build order

1. ✅ `Notification` model + migration + env.py registration
2. ✅ `InboxService` (`notify_users`, `notify_agent_managers` + reads)
3. ✅ `get_user_ids_with_permission` reverse resolver
4. ✅ Fan-out hook in `review_service.emit()`
5. ✅ `/api/notifications` routes + mount
6. ⬜ Share-path `notify_users` calls (additive to email)
7. ⬜ Frontend: `InboxFeed`, `/inbox`, nav bell
8. ⬜ In-report tool notification (depends on 2)
9. ⬜ Unit tests for `InboxService` (dedup / resurface / per-user read) + an
   integration test that `emit()` fans out to agent managers

**Remaining critical path:** 6 + 7 give an end-to-end visible inbox (shares +
review signals). 8 layers on the in-report tool. 9 hardens.

---

## 8. Open questions

- **Backfill**: fan out existing open `ReviewItem`s into notifications on
  deploy, or start fresh? (Recommend: start fresh; review feed still shows
  history.)
- **Retention**: auto-dismiss/prune read notifications after N days?
- **Real-time**: poll the count endpoint (simple, matches today) vs. push over
  the existing SSE channel. Recommend poll first.
- **Email bridge**: should some inbox notifications also email (digest)? Out of
  scope for v1; the email path already exists for shares.

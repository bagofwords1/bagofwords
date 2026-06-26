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

## 2. Decision: fan-out (keep ReviewItem as source of truth)

Two options were considered:

- **(A) Generalize `ReviewItem`** — add per-user read state + make shares/tool
  events into review items. Rejected: widens an agent-scoped, shared-state model
  into a user-scoped one, fighting its permission design.
- **(B) Fan-out (chosen)** — `ReviewItem` stays the team queue. A new per-user
  `Notification` row is created for each recipient. Share + in-report-tool paths
  write `Notification`s directly. Review feed and inbox can coexist; the inbox is
  the per-user union.

This keeps all existing review producers, dedup, and actions intact.

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

## 4. Backend wiring still to build

### 4.1 `NotificationService` (new — `app/services/notification_inbox_service.py`)

> Note: there's already a `notification_service.py` for *email dispatch*. Name
> this one distinctly (e.g. `notification_inbox_service` / `InboxService`) to
> avoid collision.

```python
async def notify_users(db, *, organization_id, user_ids, source, type, title,
                       body=None, severity="info", link=None, subject=None,
                       actor_user_id=None, source_id=None, group_key=None,
                       dedup=True) -> list[Notification]:
    # For each user_id: if dedup and an ACTIVE (dismissed_at IS NULL) row exists
    # for (user, source, group_key), refresh it (title/body/severity-escalate,
    # bump updated_at, optionally re-open read_at=None); else insert.
```

Plus reads: `list_for_user(...)`, `count_unread(user)`, `mark_read(id, user)`,
`mark_all_read(user)`, `dismiss(id, user)`. All scoped by `user_id == current_user`
— no agent-permission resolution needed (delivery already decided the audience).

### 4.2 Fan-out from review items

`review_service.emit()` (`review_service.py:124`) is the one choke point. After it
creates/bumps a `ReviewItem` with `disposition == "notify"`, resolve recipients
and call `notify_users(...)` with `source="review"`, `source_id=item.id`,
`group_key=f"review:{item.id}"`, `link` to the agent + item.

**Recipients = users with `manage` on the item's agent (or full admins).** A
reverse resolver is needed — `permission_resolver.py` currently only has the
per-user `get_ds_ids_with_permission` (`:368`); add the inverse
`get_user_ids_with_permission(db, org_id, data_source_id, "manage")`. For
**global** items (`data_source_id is None`), recipients = full admins.

Keep fan-out side-effecty but non-fatal (wrap in try/except like the existing
audit-log calls) so a notification failure never breaks emission. When a review
item resolves/dismisses (`resolve_open_for`, `dismiss`), optionally mark the
fanned-out notifications read via `source_id`.

### 4.3 Share path

In `POST /reports/{id}/notify` (`report.py:254`) and `ReportService.set_visibility`
(`report_service.py:104`): for every recipient that is a **known user**
(share-with-specific-users, and `notification_subscribers` entries of
`type=="user"`), also `notify_users(source="share", type=payload.type, ...,
actor_user_id=current_user.id, link=share_url, subject={"kind":"report",
"report_id":...})`. Email stays as-is for external addresses; the inbox row is
*additive*. This makes "shared with you" show up in-app, not just in an inbox.

### 4.4 In-report tool notification (the future piece)

Once 4.1 exists this is thin. A report run already resolves
`report → org + data_source + agents` (`review_producers._org_and_data_sources_for_report`).
Add a tool (MCP `notify_user` / internal helper) that calls
`notify_users(source="report_tool", type="report_tool", ...)` with recipients =
report owner + collaborators (`ReportShare`), `subject={"kind":"report",
"report_id":...}`, `link` to the report. Guard: only notify users who already
have access to that report (no arbitrary targeting), and dedup by
`group_key=f"report:{report_id}:{tool_key}"` to avoid a chatty agent spamming.

---

## 5. API (new — `app/routes/notification.py`, mounted in `main.py` near `review.router`)

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

## 6. Frontend

- **Generalize `ReviewFeed.vue` → `InboxFeed.vue`**: it's ~90% of the list UI
  already (rows, severity colors, read/dismiss, filters). Drive it off
  `/api/notifications`; keep a `source` filter so the Agents page can still show
  a review-only view by passing `source=review`.
- **New `/inbox` page** + a **bell with unread count** in `layouts/default.vue`
  (poll `/api/notifications/count`, same pattern KnowledgeExplorer uses for
  `/api/review/count`).
- Clicking a row navigates to `notification.link` and marks it read.
- The existing Agents Review panel stays (it's the team queue); the inbox is the
  personal union across agents + shares + report tools.

---

## 7. Build order

1. ✅ `Notification` model + migration + env.py registration *(this branch)*
2. `InboxService` (`notify_users` + reads) + unit tests
3. `get_user_ids_with_permission` reverse resolver
4. Fan-out hook in `review_service.emit()`
5. `/api/notifications` routes + mount
6. Share-path `notify_users` calls (additive to email)
7. Frontend: `InboxFeed`, `/inbox`, nav bell
8. In-report tool notification (depends on 2)

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

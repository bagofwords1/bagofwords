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

### 4.3 Share path — ✅ implemented (notify-first)

**Direction: notify → email, not email → notify.** The durable in-app
`Notification` is the canonical record of "shared with you"; email is a downstream
channel layered on top (this also completes the `notification_service` design,
whose `NotificationChannel` enum already stubbed `IN_APP`).

- `inbox_service.notify_share(report, share_type, user_ids, actor)` centralises the
  share copy and dedups per (user, `share:{share_type}:{report_id}`).
- `report_service.set_visibility` (`report_service.py`) — on a share **grant**,
  snapshots existing share recipients, then notifies only the *newly added* users.
  This is automatic: in-app awareness without forcing an email.
- `report.py notify_report` — the explicit "Notify" action resolves recipient
  emails → org users and creates/refreshes their in-app notification **before**
  dispatching email; external addresses get email only. Shared `group_key` dedups
  against the grant-path notification.

Verified end-to-end in the sandbox: sharing a conversation/artifact creates the
right `share_conversation`/`share_artifact` row with the canonical copy + deep
link; re-sharing does not duplicate.

### 4.3b Other emit sites — ✅ implemented (notify-first)

- **Scheduled run** — `scheduled_prompt_service.py` and `report_service.py` (the
  scheduled-report rerun) create an in-app `scheduled_run` notification for the
  `type=="user"` subscribers, collapsed per report (`group_key=schedule:{report_id}`)
  so repeated runs refresh one entry; email follows for all subscribers.
- **Added to an agent** — `data_source_member_email.send_member_added_email`
  creates an `agent_access` notification for the added user, **independent of
  SMTP** (the scheduling SMTP gate was removed so it fires even without email),
  inside the existing 5-min undo-delayed + re-validated job. Email is sent only
  when SMTP is configured.

Verified in the sandbox: adding a member created the `agent_access` row with the
canonical copy + `/agents/{id}` link, and it was created even though the email
send failed (dummy SMTP) — proving in-app is decoupled from email.

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
6. ✅ Share-path notify-first (conversation + artifact; `set_visibility` + `notify_report`)
7. ⬜ Frontend: `InboxFeed`, `/inbox`, nav bell
6b. ✅ Scheduled-run + agent-access emit sites (notify-first)
8. ⬜ In-report tool notification (depends on 2)
9. ⬜ Unit tests for `InboxService` (dedup / resurface / per-user read) + an
   integration test that `emit()` fans out to agent managers

**Remaining critical path:** 6 + 7 give an end-to-end visible inbox (shares +
review signals). 8 layers on the in-report tool. 9 hardens.

---

## 8. Notification copy (canonical text per type)

Variables interpolated at emit time: `{actor}` (who did it), `{agent}` (data-source
name), `{report}` (report title), `{n}`/`{secs}`/`{actual}` (counts). Convention:
**title carries the subject; body is one explanatory sentence; no trailing
call-to-action** (the row is the link). Counts render as a badge from
`group_count`/dedup, not baked into the title, so text stays stable on refresh.

**Review signals** — `source="review"`, link `/agents/{agent}`:

| type | severity | title | body |
|---|---|---|---|
| `low_confidence` | warning | `Low-confidence answers on {agent}` | `{n} answers on {agent} were scored below 3/5 in the past week.` (only after 5 in the rolling week window) |
| `schema_changed` | warning | `Schema changed on {agent}` | `Tables or columns changed on this connection — answers may be affected.` |
| `slow_query` | warning | `Slow queries on {agent}` | `A data query on {agent} ran {actual}s, over the {secs}s budget.` |
| `query_error` | error | `Query error on {agent}` | `A data query on {agent} failed to run.` |
| `instruction_suggestion` | info | `{instruction title}` | `{AI\|Proposed} instruction change awaiting review on {agent}.` |

**Share & access** — `source="share"`:

| type | severity | title | body | link |
|---|---|---|---|---|
| `share_conversation` | info | `{actor} shared a conversation with you` | `{actor} shared "{report}" with you.` | `/reports/{id}` |
| `share_artifact` | info | `{actor} shared a dashboard with you` | `{actor} shared the dashboard "{report}" with you.` | `/reports/{id}` |
| `agent_access` | info | `You were added to {agent}` | `{actor} added you to {agent}. You can now chat with this agent and explore its data.` | `/agents/{agent}` |

**Scheduled runs** — `source="schedule"`, link `/reports/{id}`:

| type | severity | title | body |
|---|---|---|---|
| `scheduled_run` | info | `"{report}" ran` | `Your scheduled report ran — {iterations} steps, {queries} queries, {artifacts} artifacts.` |
| `scheduled_run_failed` | error | `"{report}" failed to run` | `Your scheduled report didn't complete on its last run.` |

**In-report tool** — `source="report_tool"`: free-form (tool supplies title/body);
default title `Update from {report}`, link `/reports/{id}`.

> **Two required tweaks to the review fan-out** (`review_producers.py` /
> `_fanout_notification`) to match the copy above:
> 1. **Drop action references.** Current producer bodies end with *"Run training
>    to close the gaps"* / *"Consider a guardrail instruction"* — those point at
>    the run-training/run-eval buttons we removed from this surface. Strip them.
> 2. **Add the agent name to titles.** Producer titles are generic
>    (`"Low-confidence answers"`) because the review feed was agent-scoped. The
>    inbox spans agents, so `_fanout_notification` must resolve the data-source
>    name and put `{agent}` in the title.

---

## 9. Frontend — ✅ bell + modal implemented

- `composables/useNotifications.ts` — shared open-state, item list, unread count,
  and actions (`fetchCount`/`fetchItems`/`markRead`/`markAllRead`/`dismiss`).
- `components/NotificationModal.vue` — minimal `UModal` (header + unread badge +
  "Mark all read", `All / Agents / Shares / Scheduled` filter chips, rows with a
  severity-tinted icon, title/body/relative-time, unread dot, hover-dismiss, empty
  state). Row click → `markRead` + navigate to `link`.
- `layouts/default.vue` — a small bell with a red unread badge at the top of the
  sidebar (next to the logo); count polled every 60s and resynced when the modal
  closes.

*Still copy/i18n:* the modal uses literal English strings; add `$t` keys across
locales as a follow-up. A top-level `/inbox` page (vs. modal-only) is optional —
the modal covers the primary surface.

---

## 10. Open questions

- **Backfill**: fan out existing open `ReviewItem`s into notifications on
  deploy, or start fresh? (Recommend: start fresh; review feed still shows
  history.)
- **Retention**: auto-dismiss/prune read notifications after N days?
- **Real-time**: poll the count endpoint (simple, matches today) vs. push over
  the existing SSE channel. Recommend poll first.
- **Email bridge**: should some inbox notifications also email (digest)? Out of
  scope for v1; the email path already exists for shares.

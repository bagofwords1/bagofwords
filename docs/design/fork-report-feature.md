# Fork Report Feature - Design Document

**Status:** Draft / Design Phase
**Date:** 2026-03-20

---

## Problem Statement

When a user views a shared report (`/r/ID`) or conversation (`/c/TOKEN`), they currently have no way to build on that work. If they're logged in and have access to the same data sources (system-only auth, no per-user credentials required), they should be able to **fork** the report into their own workspace and continue the conversation.

---

## Core Concept

A logged-in user can "fork" a published report or shared conversation, creating a new report under their account that is seeded with the original conversation history. The fork inherits the data source associations, allowing the user to continue querying the same data.

---

## Access Eligibility

The **Fork** button appears when ALL of these are true:

| Condition | Check |
|-----------|-------|
| User is logged in | `current_user` is not null |
| Same organization | `user.organization_id == report.organization_id` |
| All data sources use system auth | Every connection has `auth_policy = "system_only"` |
| User has data source access | Each data source is `is_public=True` OR user has `DataSourceMembership` |
| User is not the owner | `user.id != report.user_id` |

If any connection requires `auth_policy = "user_required"`, the button is either hidden or shown disabled with a tooltip explaining that user credentials are needed.

---

## What Gets Forked

| Asset | Behavior |
|-------|----------|
| **Report** | New report created, owned by forking user |
| **Data source associations** | Same data sources linked to the new report |
| **Completions (messages)** | Copied as read-only seed history (`is_forked_seed=True`) |
| **CompletionBlocks** | Copied to preserve reasoning/tool/answer structure |
| **Steps** (query results + viz) | **Referenced, not copied** — step data is immutable |
| **Widgets** | New widgets created, pointing to referenced steps |
| **Artifacts** | Latest artifact version duplicated (existing `duplicate` logic) |

**NOT forked:** AgentExecutions, ToolExecutions, PlanDecisions (execution internals, not user content).

---

## Recommended Approach: Snapshot Fork

The forked report gets a **frozen copy** of the conversation history as context. The user starts a **new conversation thread** that can reference the prior work.

```
Original Report (read-only source)
  ├── Message 1: "Show me revenue by region"
  ├── AI: [SQL query] → chart
  ├── Message 2: "Break it down by quarter"
  └── AI: [SQL query] → chart

Forked Report (user's copy)
  ├── [Forked from "Revenue Analysis" by @alice]  ← visual header
  │   ├── Message 1 (read-only, seed)
  │   ├── AI response (read-only, seed)
  │   ├── Message 2 (read-only, seed)
  │   └── AI response (read-only, seed)
  │
  └── [Your conversation starts here]  ← divider
      ├── Message 3: "Now add profit margins"  ← user's new work
      └── AI: [new SQL query] → new step
```

### Why Snapshot over Live Fork

| | Snapshot Fork | Live Fork |
|--|---|---|
| **Complexity** | Lower | Higher |
| **Ownership clarity** | Clear separation of original vs new | Blurred |
| **AI context** | Seed messages included as context | Full history |
| **Risk of confusion** | Low | User might think they're editing original |

---

## Schema Changes

### Report model

```python
# New fields on Report
forked_from_id = Column(String(36), ForeignKey('reports.id'), nullable=True)
forked_from_token = Column(String, nullable=True)  # if forked from /c/TOKEN
```

### Completion model

```python
# New fields on Completion
is_forked_seed = Column(Boolean, default=False)  # marks imported messages
source_report_id = Column(String(36), nullable=True)  # origin report for attribution
```

---

## API Design

### Fork endpoint

```
POST /api/reports/{report_id}/fork
```

**Request:**
```json
{
  "title": "My analysis (optional, defaults to 'Fork of {original_title}')"
}
```

**Response:**
```json
{
  "id": "new-report-uuid",
  "title": "Fork of Revenue Analysis",
  "forked_from_id": "original-report-uuid",
  "slug": "fork-of-revenue-analysis-abc123"
}
```

**Permission:** `create_reports` + data source access checks.

### Fork eligibility (returned with existing endpoints)

Add to `/r/{report_id}` and `/c/{token}` response:

```json
{
  "fork_eligibility": {
    "can_fork": true,
    "reason": null
  }
}
```

Possible reasons when `can_fork = false`:
- `"not_logged_in"` — user is anonymous
- `"different_org"` — user is in a different organization
- `"user_auth_required"` — one or more connections require per-user credentials
- `"no_data_source_access"` — user lacks access to one or more data sources
- `"is_owner"` — user already owns this report

---

## Frontend Changes

### /r/[id] and /c/[token] pages

- Show **Fork** button in the header/toolbar when `fork_eligibility.can_fork == true`
- Button style: secondary/outline, with a fork icon
- On click: call `POST /api/reports/{report_id}/fork`, then redirect to `/reports/{new_id}`
- If `can_fork == false`, show disabled button with tooltip showing the reason

### /reports/[id] (chat editor) — forked report

- Seed messages render with a subtle visual indicator (muted background, small badge)
- Show a header banner: "Forked from [Original Report Title]" with link to original
- Visual divider between seed messages and new conversation
- Seed messages are not editable or deletable

---

## Backend Flow (Fork Service)

```
1. Validate eligibility
   ├── User is authenticated
   ├── Report exists and is published/shared
   ├── User is in same org
   ├── All connections are system_only
   └── User has access to all data sources

2. Create new Report
   ├── title = request.title or "Fork of {original.title}"
   ├── user_id = current_user.id
   ├── organization_id = current_user.organization_id
   ├── forked_from_id = original.id
   ├── status = "draft"
   └── mode = original.mode

3. Link data sources
   └── Copy report_data_source_association entries

4. Copy completions as seeds
   ├── For each completion in original (ordered by turn_index):
   │   ├── Create new Completion with is_forked_seed=True
   │   ├── Copy prompt, completion, role, turn_index
   │   └── Set source_report_id = original.id
   └── Copy associated completion_blocks (reference same steps)

5. Create widgets for referenced steps
   ├── Create new Widget entries
   └── Link to original Step records (steps are immutable/shared)

6. Duplicate latest artifact (if exists)
   └── Use existing ArtifactService.duplicate() logic

7. Return new report
```

---

## Open Questions

1. **Fork attribution visibility** — Should the original author see "5 people forked this"?
2. **Cross-org forking** — Strictly same-org only, or allow cross-org if data sources permit?
3. **Fresh data vs snapshot** — Should forked steps re-execute queries for fresh data, or keep the snapshot?
4. **Conversation length cap** — For very long conversations, fork last N messages or let user choose?
5. **Fork of a fork** — Allow it? `forked_from_id` points to immediate parent, not the root.
6. **Notifications** — Notify the original author when their report is forked?

---

## Future Extensions

- **Fork with modifications** — Let user select which messages/steps to include
- **Merge back** — If the forked user discovers something useful, suggest it back to the original
- **Fork gallery** — Show all public forks of a popular report
- **Template reports** — Reports explicitly designed to be forked (starter templates)

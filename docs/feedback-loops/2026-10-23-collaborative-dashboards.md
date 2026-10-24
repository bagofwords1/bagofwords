# Feedback Loop — Collaborative dashboards: view/edit roles, several conversations, results that never cross users

Request: build on #1455 (`feat/dashboards`) so a dashboard works like a
shared Google Doc. People get **view** or **edit** access. Everyone in a
dashboard's project can edit it. Several conversations, by several people,
can edit the same dashboard. Every change shows who made it ("edited by
Yochay's agent"). The existing artifact analytics stay the analytics.
Everything ships in **one PR stacked on #1455**.

The claim to validate: a new editor can take over or join a dashboard, and
**no person ever receives rows produced with another person's credentials**.

Status: plan, not started. Base: `feat/dashboards` @ `ab5840c`.

---

## Decisions

- **Roles:** view and edit on every grant, like Google Docs.
- **Projects:** everyone who can open a project edits its dashboards, as in
  #1455.
- **Editing:** several conversations can edit one dashboard. Each conversation
  gets its **own copies** of the queries, steps and charts. Nothing is shared by
  reference.
- **Copy source:** the newest draft. The published version is used only when
  there is no draft.
- **Credentials:** a new editor's copies **re-run with that editor's
  credentials**, for every kind of connection. The owner re-editing their own
  dashboard keeps today's instant copy.
- **Viewing:** unchanged. Viewers run with their own credentials.
- **Run as creator:** only the owner may publish a dashboard set to run as
  its creator.
- **Context:** a new conversation starts with a summary that holds **no data
  values** and needs **no LLM**.
- **History:** the dashboard keeps the chain of conversations that edited it.
- **Attribution:** "Changed by …" on each chart, and live outlines while an
  agent edits, as in Figma.
- **Analytics:** reuse the existing artifact analytics. No new system.
- **Live vs work in progress:** the published version is live. Drafts pile up
  versions (v1…v23) until someone publishes. Viewers never see a draft.
- **Drafts stay invisible by default:** every dashboard has one draft that
  people never have to name or pick. A named second draft (for example
  "Hotfix" next to "Redesign v2") is opt-in, and only then does a draft picker
  appear.
- **Preview:** editors open any version on the real page:
  `/d/sales/draft?version=23`, each with their own data.
- **Rollback:** the last few releases are kept. Rolling back moves live to an
  older release instantly, with nothing re-run.

---

## Current state (validated on `feat/dashboards`)

What blocks collaboration today, with file:line:

- **No roles.** `ArtifactShare` has no role column
  (`backend/app/models/artifact_release.py:83-105`), so every grant means view.
- **Owner-only settings.** Nine routes use `owned_dashboard`
  (`backend/app/services/dashboard_settings_service.py:31-50`), which allows
  only the owner.
- **Project = edit.** `can_edit` is the owner, or anyone who can view the
  project (`backend/app/services/dashboard_access.py:78-95`). That includes
  org-shared projects (`project_service.py:83-99`).
- **One conversation per dashboard.**
  - `Artifact.report_id` is a single column (`backend/app/models/artifact.py:33-36`).
  - `edit_dashboard` moves it atomically (`dashboard_lifecycle.py:242-300`).
- **Agent tools refuse other conversations:**
  - `edit_artifact.py:118,180`, `read_artifact.py:265,435`, `edit_doc.py:109`
  - `create_artifact.py:1055,1234,1312`, `edit_artifact_legacy.py:1014`, `create_doc.py:78`
  - `mcp/edit_artifact.py:100,193`, `mcp/app_tools.py:243,261`, `manage_artifact_resources.py:47`
  - `services/artifact_payload.py:78`
- **Query scoping is per conversation and can stay.** `run_query`,
  `read_query`, `add_parameter`, `query_context_builder` and `loadables`
  filter by `Query.report_id`. Each conversation works on its own copies,
  so this is already correct.
- **Versions.** `ArtifactVersion` already has `report_id` and `user_id`
  (`artifact.py:103-175`). It has **no base/parent version**.
- **Schedules run as the owner.**
  - Published: `run_user=owner` (`dashboard_refresh.py:330-362`).
  - Draft: `_refresh_draft` (`:365-400`).
  - Failure alerts go to the owner only (`_tell_failure`, `:458-500`).
- **Copies change ids.** `remap_ids` in the copy engine gives every copy new
  ids. A chart has no stable identity across conversations.
- **Edit copies the owner's rows as they are**
  (`dashboard_lifecycle.py:121` `release_to_conversation`). That is safe only
  for the owner.
- **Fork already does the safe version:**
  - `fork_eligibility` checks access to every source.
  - Over RLS or personal connections the rows aren't copied, and the code
    re-runs as the forker (`hydrate_fork`).
- **Analytics exist.**
  - `artifact_view_days` stores views per dashboard, day, surface and hashed
    viewer (`artifact_resource.py:105-115`).
  - Read through `GET /runtime/analytics` (`artifact_resources.py:380-410`),
    shown in `ArtifactResourceExplorer.vue`.
  - Owner only (`artifact_resource_service.py:99-105`).
  - `/d/{id}/draft` counts as `standalone`, so **editors inflate the numbers**.
- **No live channel between users.** Agent progress is SSE to its own
  browser only. The websocket layer was removed
  (`streaming/completion_event_bus.py:1-11`).

---

## Design

### 1. Roles

| Action | Viewer | Editor | Owner |
|---|---|---|---|
| See published | ✅ | ✅ | ✅ |
| Edit in a conversation | — | ✅ | ✅ |
| Publish | — | ✅ ¹ | ✅ |
| Add viewers / editors | — | ✅ ² | ✅ |
| Make public | — | — | ✅ |
| Schedule, chat, Data tab, link name | — | ✅ | ✅ |
| Viewer ↔ creator credentials | — | — | ✅ |
| Delete, transfer ownership | — | — | ✅ ³ |

¹ Not when the dashboard runs as its creator.
² Unless the owner turns off "Editors can share".
³ Org admins can too.

- **Where a role comes from:** a person grant, a group grant, the dashboard's
  project, or its visibility. The highest role wins. Visibility only ever
  gives view.
- **One gate:** `dashboard_access.capabilities(artifact, user)` returns the
  set of allowed actions.
  - It replaces `owned_dashboard` and the `is_owner` / `can_edit` flags.
  - The page meta returns it, and the UI shows only allowed buttons.
- **Owner leaves the org:** the dashboard stays live, and an org admin
  transfers it.

### 2. Several conversations, one dashboard

How a conversation joins:

1. An editor clicks **Edit in my conversation**.
2. The newest draft is copied in, or the published version when there is no
   draft. `release_to_conversation` gains a version source, as Publish's
   `copy_dashboard` already has.
3. The draft records `base_version_id`.
4. Every step re-runs as the editor (section 3).
5. The summary is added (section 4).

Code changes:

- **The list:** a new `artifact_conversations` table is the source of truth.
  `Artifact.report_id` stays, holding the newest conversation, so code from
  #1455 keeps working.
- **Agent tools:** each `artifact.report_id == report.id` check becomes "this
  conversation edits this dashboard and its user may edit".
- **Draft per conversation:** the newest completed version made there.
  Publish accepts a version from any editing conversation.
- **Deleting a conversation:** it leaves the list. A never-published dashboard
  is deleted only when no editing conversation is left.
- **Publish check:** if someone published since this draft's
  `base_version_id`, Publish returns 409 with who and when. The UI then says
  *"Avi published 2 h ago. Review changes / Publish anyway"*.

### 3. Credentials and data safety

Rule: **a result reaches only the person whose credentials produced it**, or
a viewer the connection already lets see it.

| Moment | Today | After |
|---|---|---|
| Owner re-edits | owner, rows copied | same |
| Another editor joins | not possible | re-run as editor |
| Draft charts | owner | that conversation's user |
| Publish | publisher | same |
| Viewer opens / filters | viewer (or owner if run-as-creator) | same |
| Scheduled / on-open refresh | owner | **last publisher** |
| Draft schedule | owner | newest conversation's user |

- **Joining is checked like Fork:** the editor needs access to every source
  (`fork_eligibility`) and must pass the monitoring check (`assert_read`).
  Files are linked only if the editor can read them.
- **Why the last publisher runs the schedule:** Avi may add a source Dana
  can't use. A refresh as Dana would then fail, or run Avi's query with
  Dana's access.
- **Failure alerts:** they go to the owner and the last publisher.
- **Another editor's draft:** shown as structure only, never results.

### 4. Context carried over

- **The summary, built in code:**
  - charts and queries by title, their sources and parameters;
  - the change log per version: added, removed or renamed, with who and when;
  - the dashboard **notes**.
- **What it never includes:** agent replies, result rows, or the user's
  messages, which can quote numbers.
- **Notes:** a free-text field on the dashboard, like a README, for intent,
  definitions and to-dos.
- **Where it shows:** as the agent's starting context, and as a card in the
  new conversation: *"Continued from Dana's conversation, Oct 12, 3 versions"*.
  The link opens only for people who can already open that conversation.
- **History:** dashboard settings list the chain of conversations, from
  `artifact_conversations`.

### 5. Attribution and presence

- **A stable key per chart:** every content block gets an `element_key` that
  `remap_ids` keeps.
- **Changed by:** hovering a chart shows *"Changed by Avi's agent, 2 h ago"*.
  It comes from comparing versions by `element_key`, so it is history, not
  presence.
- **Unpublished changes:** outlines every block that differs from the live
  release.
- **Live, own agent:** the edit tools add `element_key` to the existing SSE
  progress. The chart is outlined: *"Yochay's agent: editing"*.
- **Live, other editors:** avatars (*"Avi is also editing"*) and outlines on
  the charts another person's agent is changing now.
- **Who sees it:** editors only. Viewers of `/d` get no presence row.

**`artifact_presence`: who is editing right now** (one row per person per
conversation):

| Field | Meaning |
|---|---|
| `artifact_id` | the dashboard |
| `user_id` | who |
| `report_id` | their conversation |
| `state` | `viewing` / `agent_editing` |
| `element_keys` | charts the agent is changing now |
| `completion_id` | the agent run doing it |
| `seen_at` | last heartbeat |

**When the row changes:**

| Moment | Written by | Change |
|---|---|---|
| Editor opens the dashboard in a conversation | browser | create, `viewing` |
| Every ~15 s while open | browser | `seen_at` |
| Agent run starts | run-start hook | `agent_editing` + `completion_id` |
| An edit tool is about to change charts | one shared helper | `element_keys` |
| Agent run ends (done, error, cancel) | run-end hook | back to `viewing`, keys cleared |
| Tab closes | browser | delete |
| No heartbeat for ~30 s | cleanup job | treated as gone, deleted later |

- **The tool hook lives in one place:** at the shared version write
  (`new_version`) or in a common tool wrapper, not in each of the ~10 edit
  tools, so a new tool can't forget it.
- **Why the table and not hooks alone:**
  - The hook event bus runs inside one server process
    (`completion_event_bus.py`). Another user's browser may be on a different
    server.
  - So hooks write the state, and the table shares it across servers.
- **Delivery:** through the existing cross-user stream
  `GET /reports/activity/stream` (`report_activity_hub.py`, which re-reads the
  DB each tick). No new poller or websocket.

### 6. Analytics (existing system)

1. **Access:** editors and the owner, through the `view_analytics` capability.
2. **Clean numbers:** stop recording `/d/{id}/draft` views, and leave out
   editors' own views.
3. **Entry point:** an Analytics item in the `/d` toolbar for editors.
4. **Optional:** a `release_id` on the day counter, for views per published
   version.

### 7. Drafts, preview, promotion and rollback

The flow: a dashboard is live, people work on it for weeks (v1…v23), editors
preview it, and someone promotes it.

```
artifacts (dashboard)
 ├─ published_release_id ──► artifact_releases   = live
 │                             (last few kept, for rollback)
 ├─ artifact_drafts          = "Main" (implicit), "Redesign v2" (optional)
 │    └─ artifact_conversations  = who works on that draft
 │         └─ artifact_versions  = v1 … v23
 └─ step_user_results        = previews, each person's own rows
```

**Invisible unless needed**
- Every dashboard has one implicit draft. Editing, joining, preview and Publish
  use it, and the word "draft" never needs to appear.
- "Start a separate draft" (for example a hotfix next to a redesign) is
  opt-in. Only when a dashboard has more than one open draft do the draft
  picker and the draft name on Publish appear.

**`artifact_drafts` (new)**

| Field | Purpose |
|---|---|
| `id`, `artifact_id` | the dashboard |
| `name`, `slug` | "Redesign v2" → `?draft=redesign-v2`; the implicit one is "Main" |
| `base_release_id` | the live release it started from |
| `status` | `open` / `promoted` / `abandoned` |
| `promoted_release_id` | the release it became |
| `created_by`, `created_at` | who and when |

- `artifact_conversations.draft_id`: each conversation works on one draft.
- `artifact_versions.draft_id`: a draft's head is its newest completed version.
  Joining a draft copies from its head.

**Preview**
- `/d/sales/draft?version=23` (and `?draft=redesign-v2`) shows that version on
  the real page. It builds on #1455's editor-only draft page; everyone else
  gets "not found".
- **Your own version:** its results as they are.
- **Someone else's version:** its steps re-run with your credentials on open,
  stored per step and per person in `step_user_results` (exists for viewer
  runs).
- The page has a **Publish this version** button.

**Promote = Publish**
1. A release is built from the chosen version by #1455's copy engine,
   all-or-nothing.
2. When it's ready, `published_release_id` moves to it, and the previous
   release is kept.
3. The draft becomes `promoted`. The implicit draft simply continues.
4. Another open draft still has the old `base_release_id`. When it publishes,
   it gets the "live changed since you started" warning.

**Rollback**
- Today #1455 deletes the previous release's rows and files at the next
  publish. Change: keep the last 5 ready releases.
- Rollback moves `published_release_id` to an older ready release. It's
  instant, nothing re-runs, and the data is the same as it was.
- Editors and the owner can roll back. It is audited (`dashboard.rolled_back`).

**Later, not in this PR** (what Power BI deployment pipelines add):
- a test stage for non-editors (reviewer previews);
- different data per stage.

### 8. UI changes

- **Share:**
  - "Can view" / "Can edit" per person and group;
  - an "Editors can share" switch for the owner;
  - "Everyone in project Sales can edit".
- **Toolbar** (`/d` and in the conversation):
  - "Edit in my conversation";
  - avatars of who's editing;
  - Analytics for editors.
- **Publish:**
  - the warning *"Avi published 2 h ago: Review changes / Publish anyway"*;
  - disabled, with the reason, for editors of a run-as-creator dashboard;
  - the draft name only when there are several drafts.
- **Charts:**
  - "Changed by …" on hover;
  - a live "…'s agent: editing" outline;
  - a Review changes view against live.
- **Joining:**
  - charts show "Running as you…";
  - a "Continued from Dana's conversation" card.
- **Preview page:** a version picker, a draft picker (only with several
  drafts), and "Publish this version".
- **Settings:**
  - Notes;
  - History (conversations, versions, releases, with **Roll back**);
  - transfer ownership;
  - credentials and public visibility locked for editors.
- **Everywhere else:** schedule, settings and delete show or hide by role.
- **Delete conversation dialog:** says when other conversations keep editing.
- **Dashboards page:** a "Can edit" / "Can view" label on shared cards.
- **Analytics panel:** the existing one, opened to editors.

### 9. Data model

| Where | Change |
|---|---|
| `artifact_shares` | + `role` (`view`/`edit`, default `view`) |
| `artifacts` | + `editors_can_share`, + `notes` |
| `artifact_versions` | + `base_version_id`, + `draft_id` |
| version/release `content` | + `element_key` per block |
| `artifact_releases` | kept (last 5 ready), not deleted at next publish |
| `artifact_drafts` (new) | name, slug, base release, status, promoted release |
| `artifact_conversations` (new) | dashboard, conversation, user, draft, base version, summary, started/ended |
| `artifact_presence` (new) | dashboard, user, conversation, state, element keys, run, seen_at |

### 10. Migration (`collab01…`, after `dashfc08`)

1. Add `role`. Existing grants become `view`, so no one's access changes.
2. Add `editors_can_share` (default on) and `notes`.
3. Create one "Main" draft per dashboard. Its `base_release_id` is the current
   live release.
4. Fill `artifact_conversations` from the conversations existing versions
   name, all on "Main". Set `draft_id` on existing versions.
5. Set `element_key` = the current block id in existing versions and
   releases.
6. Create `artifact_presence` empty.

The downgrade drops all of it. No query, step or result row is touched.

---

## Loop A — deterministic tests (no external services)

```bash
cd backend
export TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db
uv run pytest tests/e2e/dashboards/test_collaboration_*.py -m e2e --db=sqlite -q
uv run pytest tests/e2e/dashboards/test_collaboration_*.py -m e2e --db=postgres -q
```

The world: Dana (owner), Avi (editor), Noa (viewer), Gil (org member with no
access), an org admin, and a stranger. There are three connections: a shared
one, one with RLS, and a personal (per-user) one. A unique number is seeded
in Dana's results.

| Invariant | Test |
|---|---|
| Every route × every role matches the Roles table | `test_capability_matrix` |
| Avi joins over shared / RLS / personal: Dana's number never reaches Avi's copies, API, summary or agent context until Avi's own run makes it | `test_join_never_carries_rows` |
| A summary of a seeded conversation contains no result value | `test_summary_has_no_data` |
| An editor's conversation edits the dashboard; a viewer's is refused | `test_agent_tools_by_role` |
| Second publisher gets 409 with who and when; "publish anyway" works | `test_publish_since_you_started` |
| Two joins at once: both get their own draft; nothing is lost | `test_concurrent_joins` (Postgres) |
| Run-as-creator: only the owner publishes | `test_run_as_creator_owner_only` |
| Schedule runs as the last publisher; the owner if they left | `test_schedule_identity` |
| Draft views and other editors' views are not counted (the owner's are, as before); editors read analytics | `test_analytics_editors` |
| A dashboard with one draft never asks for a draft; a second draft is opt-in | `test_implicit_draft` |
| Preview of another editor's version re-runs as me; I never see their rows | `test_preview_runs_as_me` |
| Promote moves live only when ready; the previous release is kept | `test_promote_keeps_previous` |
| Rollback is instant, runs nothing, and serves the old data | `test_rollback` |
| Presence: set on run start, cleared on run end, gone after 30 s with no heartbeat | `test_presence_lifecycle` |
| Before/after migration: everyone sees and can do the same; downgrade exact | `test_migration_before_after.py` |

**Before:** every test fails (no roles, no join, no `artifact_conversations`).
**After:** all pass.
**Mutation check:** remove each rule (the re-run on join, the role check, the
409, the summary filter) and its test must fail.

## Loop B — live confirmation (real credentials)

This proves the data rule on a real per-user source.

- **Power BI / Fabric** through the Entra app (delegated: openid, profile,
  email; Power BI and Fabric read all).
- **The OpenAI key**, for real agent runs.
- Credentials come from env vars only, kept outside the repo in this
  session's scratchpad. They never go into this doc, logs or commits.

Steps:

1. `tools/agent/boot_stack.sh`, then `uv run python ../tools/agent/seed_org.py`.
2. Dana connects Power BI with her Entra user and builds a dashboard with the
   agent. She publishes it.
3. Avi, an editor with his own Entra user, joins from his conversation.
   - Check: his charts show "loading", then **his** rows, never Dana's.
   - Check in the DB: no copied step holds Dana's data.
4. Avi publishes. Dana's next Publish shows the warning.
5. Capture before/after screenshots (ui-evidence skill), in English and
   Hebrew.

Needed: **two Entra test users** with different Power BI access (see Open
questions).

---

## Build order (one PR, one commit per step)

1. **Schema:** migrations, downgrade, and the before/after test.
2. **Roles:** `capabilities()`, the nine routes, the role in the share API,
   and a view/edit picker in `ShareModal.vue`.
3. **Editing conversations:** `artifact_conversations`, agent-tool checks,
   draft per conversation, Publish from any editing conversation, delete
   rules.
4. **Joining:** "Edit in my conversation", copying the newest draft,
   re-running as the editor, Fork-style checks.
5. **Credentials:** schedule and refresh as the last publisher,
   run-as-creator owner-only, failure alerts.
6. **Context:** summary, notes, History.
7. **Drafts and the publish check:** the implicit draft, opt-in named
   drafts, `base_version_id` / `base_release_id`, the 409, Review changes.
8. **Preview, promote, rollback:** `?version=` / `?draft=`, re-run as the
   viewing editor, "Publish this version", kept releases, Roll back.
9. **Attribution:** `element_key`, "Changed by", the unpublished-changes view,
   and the live outline.
10. **Presence:** the table, the run and tool hooks, the heartbeat, delivery on
    the activity stream, avatars and outlines.
11. **Analytics:** editor access, clean numbers, the `/d` entry.
12. **Finish:** locales, screenshots, ui-audit, PR description, release notes,
    docs.

Steps 1–5 are the safety core: a review can stop there.

---

## Definition of done

The PR is done when **every** box is ticked.

**Behaviour**
- [x] Share has a view/edit picker for people and groups; existing grants
      show as view.
- [x] Everyone in a dashboard's project can edit it.
- [x] Viewers, editors and the owner see exactly the buttons in the Roles
      table, in the UI and the API.
- [x] An editor can open the dashboard in their own conversation, and two
      editors can work at the same time.
- [x] A joining editor's charts re-run as them. They never see another
      person's rows, over shared, RLS and personal connections.
- [x] The new conversation shows the summary card. The agent receives the
      summary as context. The summary holds no data.
- [x] The dashboard has notes and a History of its conversations.
- [x] Publish warns when someone published since the draft started.
- [x] Charts show "Changed by …". The agent's chart is outlined live.
      Other editors show as avatars.
- [x] Only the owner can publish a run-as-creator dashboard, make a dashboard
      public, delete it or transfer it.
- [x] Scheduled and on-open refreshes run as the last publisher. Failure
      alerts reach the owner and the last publisher.
- [x] Editors can open Analytics from `/d`. Draft and editor views are not
      counted.
- [x] Drafts are invisible by default. A user with one draft never sees the
      word or a picker. "Start a separate draft" works, and the picker
      appears only then.
- [x] Editors preview any version at `/d/<name>/draft?version=N`, with their
      own data, and can publish it from there.
- [x] Viewers keep the live version through weeks of draft work. They switch
      only when a publish succeeds.
- [x] Rollback to any of the last 5 releases is instant and audited.
- [x] Presence clears when the agent run ends, and within ~30 s after a crash
      or a closed tab.

**Safety**
- [x] `test_join_never_carries_rows` and `test_summary_has_no_data` pass on
      sqlite and Postgres.
- [x] Mutation check: each rule's test fails when the rule is removed.
- [ ] Loop B on Power BI with two Entra users: no row crosses users.

**Upgrade**
- [x] The migration changes no one's access (before/after test, every user ×
      dashboard).
- [x] The downgrade restores every row exactly.
- [x] Old `/r` and `/d` links behave as in #1455.

**Quality**
- [ ] Full e2e is green on sqlite and Postgres, except failures that are red
      on the base branch too.
- [ ] Unit and Playwright tests are green.
- [x] New strings are in every locale catalog. Hebrew RTL is checked.
- [x] Before/after screenshots (en + he) for Share, the toolbar, the summary
      card, History, the publish warning and attribution.
- [ ] ui-audit pass, role by role, on the dashboard, Share and settings.
- [ ] The PR description follows `.claude/templates/PR_DESCRIPTION_STANDARD.md`.
- [x] `docs/design/dashboards-first-class.md` is updated.
- [ ] `CHANGELOG.md` and `VERSION` are updated (release-notes skill).

### Status (2026-10-23, live walkthrough + tests)

- **Tests:** dashboards e2e on sqlite 451 passed, 4 skipped; collaboration,
  list and sharing on Postgres 38/38. Unit: new presence and edit-guard
  tests pass. Pre-existing failures, red on `main` too:
  `tests/unit/test_artifact_feedback_loop.py::test_page_edit_prompt_static_reference_lives_in_system_prompt`,
  `frontend/tests/unit/kickoff-stream.test.mjs`.
- **Mutation checks (each test fails when its rule is removed):** join
  copies rows; edit grants ignored; publish skips the published-since
  check; editor visits counted; rows leak into the summary; a dashboard
  whose versions all failed is listed; the draft run omits filter-option
  queries; the edit guard counts other conversations' versions.
- **Live (Music Store, 5 users, screenshots in `media/pr/collab-dashboards/`):**
  share with view/edit roles; join re-runs as the joiner; live presence and
  the agent's chart outline; published-since dialog and Changes panel;
  History (versions, releases, conversations, notes, owner) and rollback;
  analytics without editors' visits; project dashboards editable by project
  members; a separate named draft; the delete note; Hebrew RTL.
- **RLS, live:** built-in RLS on a Music Store custom table (policy seeded in
  the sandbox DB — authoring needs an enterprise license): the viewer, the
  joining editor and the owner each see only their own slice; a preview of
  someone else's version never shows their slice.
- **Power BI, live:** service-principal connection to the bow14 tenant; a
  dashboard on `SalesPush` re-runs as the joining editor. The RLS model is
  unreadable to the service principal (as documented). Per-user Power BI RLS
  (demo1/demo2 via OBO) is **not yet run**: it needs those users' passwords.
- **Deviations from the plan:** presence reaches browsers by polling the
  presence endpoint (not the activity stream); the agent's chart outline
  comes from each version it saves (chart fingerprints vs. its base), since
  a tool's input rarely names charts; no backfill migration (existing
  dashboards get lineage and fingerprints from their next version).
- **Open:** per-user Power BI RLS run (Loop B); ui-audit pass; PR
  description; CHANGELOG/VERSION at release.

---

## Open questions

- [ ] **Org-shared projects:** does every org member become an editor?
      Recommended: org-shared gives view, and a direct or group grant gives
      edit.
- [ ] **Public:** can editors make a dashboard public? Recommended: owner
      only.
- [ ] **Schedule identity:** the last publisher (recommended) or always the
      owner?
- [ ] **Limit:** a maximum number of editing conversations per dashboard, or
      none?
- [ ] **Merge:** "Bring in Avi's changes" (an agent-assisted merge) in this
      PR or the next?
- [ ] **Analytics:** add `release_id` to the view counter now or later?
- [ ] **Transfer:** can the owner hand a dashboard over alone, or only an org
      admin?
- [ ] **Kept releases:** is 5 the right number, or should it be per-org?
- [ ] **Next PR:** a test stage for non-editor reviewers, and different data
      per stage (as Power BI deployment pipelines do). Wanted?
- [ ] **Loop B users:** two Entra test users with different Power BI access.
      Who provides them?

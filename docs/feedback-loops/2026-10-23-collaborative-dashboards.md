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
  It comes from comparing versions by `element_key`.
- **Unpublished changes:** outlines every block that differs from the live
  release.
- **Live, own agent:** the edit tools add `element_key` to the existing SSE
  progress. The chart is outlined: *"Yochay's agent: editing"*.
- **Live, other editors:** avatars (*"Avi is also editing"*) and outlines on
  charts other drafts changed.
  - Built on an `artifact_presence` heartbeat row, polled every few seconds.
  - Works across several servers.
- **Who sees it:** editors only. Viewers see none of it.

### 6. Analytics (existing system)

1. **Access:** editors and the owner, through the `view_analytics` capability.
2. **Clean numbers:** stop recording `/d/{id}/draft` views, and leave out
   editors' own views.
3. **Entry point:** an Analytics item in the `/d` toolbar for editors.
4. **Optional:** a `release_id` on the day counter, for views per published
   version.

### 7. Data model

| Where | Change |
|---|---|
| `artifact_shares` | + `role` (`view`/`edit`, default `view`) |
| `artifacts` | + `editors_can_share`, + `notes` |
| `artifact_versions` | + `base_version_id` |
| version/release `content` | + `element_key` per block |
| `artifact_conversations` (new) | dashboard, conversation, user, base version, summary, started/ended |
| `artifact_presence` (new) | dashboard, user, conversation, element keys, seen_at |

### 8. Migration (`collab01…`, after `dashfc08`)

1. Add `role`. Existing grants become `view`, so no one's access changes.
2. Add `editors_can_share` (default on) and `notes`.
3. Fill `artifact_conversations` from the conversations existing versions
   name.
4. Set `element_key` = the current block id in existing versions and
   releases.
5. Create `artifact_presence` empty.

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
| Draft and editor views are not counted; editors read analytics | `test_analytics_editors` |
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
7. **Publish check:** `base_version_id`, the 409, Review changes.
8. **Attribution:** `element_key`, "Changed by", the unpublished-changes view,
   and the live outline.
9. **Presence:** heartbeat, polled endpoint, avatars and outlines.
10. **Analytics:** editor access, clean numbers, the `/d` entry.
11. **Finish:** locales, screenshots, ui-audit, PR description, release notes,
    docs.

Steps 1–5 are the safety core: a review can stop there.

---

## Definition of done

The PR is done when **every** box is ticked.

**Behaviour**
- [ ] Share has a view/edit picker for people and groups; existing grants
      show as view.
- [ ] Everyone in a dashboard's project can edit it.
- [ ] Viewers, editors and the owner see exactly the buttons in the Roles
      table, in the UI and the API.
- [ ] An editor can open the dashboard in their own conversation, and two
      editors can work at the same time.
- [ ] A joining editor's charts re-run as them. They never see another
      person's rows, over shared, RLS and personal connections.
- [ ] The new conversation shows the summary card. The agent receives the
      summary as context. The summary holds no data.
- [ ] The dashboard has notes and a History of its conversations.
- [ ] Publish warns when someone published since the draft started.
- [ ] Charts show "Changed by …". The agent's chart is outlined live.
      Other editors show as avatars.
- [ ] Only the owner can publish a run-as-creator dashboard, make a dashboard
      public, delete it or transfer it.
- [ ] Scheduled and on-open refreshes run as the last publisher. Failure
      alerts reach the owner and the last publisher.
- [ ] Editors can open Analytics from `/d`. Draft and editor views are not
      counted.

**Safety**
- [ ] `test_join_never_carries_rows` and `test_summary_has_no_data` pass on
      sqlite and Postgres.
- [ ] Mutation check: each rule's test fails when the rule is removed.
- [ ] Loop B on Power BI with two Entra users: no row crosses users.

**Upgrade**
- [ ] The migration changes no one's access (before/after test, every user ×
      dashboard).
- [ ] The downgrade restores every row exactly.
- [ ] Old `/r` and `/d` links behave as in #1455.

**Quality**
- [ ] Full e2e is green on sqlite and Postgres, except failures that are red
      on the base branch too.
- [ ] Unit and Playwright tests are green.
- [ ] New strings are in every locale catalog. Hebrew RTL is checked.
- [ ] Before/after screenshots (en + he) for Share, the toolbar, the summary
      card, History, the publish warning and attribution.
- [ ] ui-audit pass, role by role, on the dashboard, Share and settings.
- [ ] The PR description follows `.claude/templates/PR_DESCRIPTION_STANDARD.md`.
- [ ] `docs/design/dashboards-first-class.md` is updated.
- [ ] `CHANGELOG.md` and `VERSION` are updated (release-notes skill).

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
- [ ] **Loop B users:** two Entra test users with different Power BI access.
      Who provides them?

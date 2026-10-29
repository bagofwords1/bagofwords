# Next work: app conversation policy and artifact activity

Status: **§1 (conversation policy), §2 (activity log), §3 (preview evidence) and
§4 (agent context) are implemented**; §5 (the Activity window) and the
"Fix with BOW" toast are not started (no UI yet). As-built notes for §2–§4 are in
§2.3 below. Written 2026-10-29 on
`ai/exciting-goodall-m7toth` as the follow-up to `docs/design/embedded-artifacts.md`.

## 0. Why this exists (the bug that started it)

An editor built a Music Store chat app from a report in **Auto** (no agents
attached to the report), published it, then edited it (draft v10). In the app:

- creating a conversation that named the Music Store agent failed with
  `conversation.agent_forbidden` ("You can't use that agent here");
- once BOW removed the agent name, conversations ran in **data-only** scope and
  the assistant answered "I have no access to create_data / any agent".

Root cause (`backend/app/services/artifact_conversations.py`, `_built_with` and
`allowed_agent_ids`):

1. When a published release exists, agents come only from the release:
   `dashboard_chat._release_sources` returns the release's linked sources, else
   source names found in its copies' code. A chat app published from an Auto
   report has neither, so the list is empty.
2. The draft the editor is looking at is ignored once anything is published.
3. Empty agents silently means "data only", so the app answers from dashboard
   rows instead of failing.
4. BOW's own preview runs the SDK in fixture mode (`artifact-sdk.js`,
   `fixtureConversation`), so `create({agents})` and `policy()` were never
   checked for real. BOW said "the test viewer only gives dummy answers".

My live loop missed it because the sandbox report had the agent attached
explicitly; nobody tested an Auto report, which is the normal case.

## 1. Conversation policy — DONE

As built (`backend/app/services/artifact_conversations.py`):

- `agent_limit`: the owner's list (`chat_data_source_ids`, non-empty) → else the
  agents the report attached explicitly (`release_data_source_ids`; on a draft,
  the editing conversations' roster) → else **None = Auto**.
  `allowed_agent_ids` = the viewer's live accessible agents ∩ that limit.
- A conversation narrowed at creation remembers it in the thread's
  `focused_data_source_ids` (no migration); otherwise its roster is re-resolved
  for the viewer on every message.
- No agent → `policy()` `{available:false, reason:"no_agents"}`;
  `create`/`send` → `conversation.no_agents`. Never data-only.
- `policy()` returns `agents_mode` (`auto` / `limited`) and `model`
  `{id, name, source: default|pinned|fallback}`. The resolver is the one runs
  use (`LLMService.get_default_model_for_report`).
- The prompt marks an app conversation (`surface: "app"`): reply in the
  person's language, no narration, nothing about charts or tools.
  `messages()` returns the final block as `text`, with earlier narration as
  `steps`.
- Settings → Viewers: **Agents and model** is always visible (Auto / Only
  these, model = each viewer's default or a pin); "Allow chat" and
  "dashboard data only" are the bubble's alone. Stored in the existing
  columns: `["*"]` → null (Auto), a list (a limit), `[]` (bubble data only;
  apps then use Auto or the report's agents).
- The chat bubble's own rules are unchanged (its default stays the release's
  sources, so existing dashboards do not widen).
- Tests: `test_artifact_conversations.py` (11), `tests/unit/test_artifact_chat_context.py`.
  Live: `tools/agent/embed/auto_app_flow.mjs`, an Auto report, a Hebrew prompt,
  a chat app with no queries, answered in Hebrew from the data
  (`media/pr/embedded-artifacts/50-auto-app-he-answer.png`).

### The original plan for §1

The rule, decided with the user:

- **Agents: Auto by default, with an optional limit.**
  - If the artifact's conversation is Auto, app conversations are Auto: any
    agent the viewer may use, chosen at run time like a normal BOW chat. Viewer
    access is applied on every message, so embedding never widens it.
  - If the owner sets "Agents this app can use" (artifact settings), use only
    those, still ∩ the viewer's access.
  - If the report had explicit agents, those are the default limit.
- **Model: chosen by the owner.** Organization default, or one enabled model.
  Viewers do not choose (an owner-approved list could come later).
- **Both live in the artifact's own settings**, not under the bubble's
  "Allow chat" section. The chat bubble reads the same two settings, so there is
  one source of truth. "Dashboard data only" stays a bubble-only option and
  never applies to `bow.conversations`.
- **No silent data-only mode for apps.** If no agent resolves, `policy()`
  returns `available:false, reason:"no_agents"` and `send()` fails with a typed
  error. The app shows it; the agent sees it (§3).
- **The editor's draft uses the draft's conversation(s)**
  (`dashboard_collab.editing_report_ids`) even when an older version is
  published. Viewers use the published release.
- **Authoring guidance** (`backend/app/ai/agents/planner/artifact_sdk_reference.py`):
  do not pass `agents` to `conversations.create` unless the user asked to
  narrow them; show `policy().reason` when unavailable.

Where:

- `services/artifact_conversations.py`: replace `_built_with` /
  `allowed_agent_ids`. Auto needs the same resolution a normal report run uses
  (`app/ai/tools/implementations/agent_focus_common.py::accessible_agents`, and
  how an empty roster means Auto for non-`artifact_chat` reports;
  `resolve_run_agents` special-cases `artifact_chat` today, so read it first).
- Settings: `DashboardSettingsModal.vue` (move or duplicate the agent and model
  pickers out of the bubble section), `dashboard_settings_service` and
  `Artifact.chat_data_source_ids` / `chat_model_id`. Consider whether reusing
  those two columns is enough, or whether the bubble needs its own "data only"
  flag once they mean "app agents and model".

Tests (`backend/tests/e2e/dashboards/test_artifact_conversations.py`):

- an app published from an **Auto** report answers with an agent (stubbed loop,
  assert the roster or the run's agents);
- the owner's limit narrows; a viewer without access to an agent never gets it;
- an editor on an unpublished draft, and on a draft newer than the release,
  gets the draft's agents;
- no agents → `policy().available == false` and `send` → typed error, never a
  data-only answer;
- bubble "data only" and Allow chat off change nothing for conversations.

Live re-check: reproduce the user's case exactly (Auto report, Hebrew prompt,
chat app, edit after publish) with the real model, as in
`docs/feedback-loops/embedded-artifacts.md`.

## 2. Artifact activity log

What happened in the app while the **editor** used it, for the agent (and later
a window). Never recorded for viewers or inside host applications.

### 2.1 What is captured

| Kind | Where it is captured | Fields |
| --- | --- | --- |
| `request` | **server**, in the runtime routes (`routes/artifact_resources.py`, `routes/artifact_conversations.py`) and the `/api/d/{ref}/*` routes the page calls | op (`records.update`, `conversations.create`, `ai.stream`, `queries.run`…), status, error code, duration, sizes, short request/response excerpts |
| `stream` | client (broker) | how a stream ended: completed, error, interrupted, cancelled |
| `filter` | client (`SharedDashboardView` / `ArtifactFrame` param handling) | param changed, queries re-run, row counts, rows withheld |
| `resource_write` | server (records/files routes) | resource, record id, revision before/after, conflict |
| `error` / `console` | client (frame `pageerror`, console errors, render failures) | message, stack excerpt |
| `click` | client (listener in the artifact runtime script, `public/libs/artifact-globals.js` or the SDK) | role + visible text + nearest label ("button 'Send'"); never typed values |

Every request already passes through the page broker
(`frontend/composables/useArtifactRuntime.ts`; the sandbox has
`connect-src 'none'`), so the server side sees all of them. The client only adds
what the server cannot know.

### 2.2 Storage

One table, `artifact_activity_events`:

- `id`, `organization_id`, `artifact_id`, `artifact_version_id`, `report_id`,
  `user_id`;
- `session_id` (per page load), `seq`;
- `source`: `editor` | `preview` (the agent's own browser, §3);
- `kind`, `op`, `status`, `error_code`, `duration_ms`;
- `detail` JSON: at most a few KB, after the org's PII redaction
  (`app/ai/llm/pii`), with message text and field values dropped by default;
- `shown_in_completion_id` (null until an agent turn included it);
- `created_at`.

Client events are sent in batches to one editor-only endpoint
(`POST /api/artifacts/{id}/runtime/activity`, gate: can edit the artifact),
every few seconds and immediately on an error.

Retention is short: 7 days or the last ~500 events per report, whichever is
smaller. Use a scheduled sweep, like the `ArtifactRateBucket` pruning in
`services/artifact_admission.py`. It is a database table (not memory or files)
because there are several workers and the agent's tool queries by report, on
SQLite and Postgres.

The migration must be idempotent if the dashboard test corpus applies it
unstamped (see `hostapp01` and `tests/fixtures/dashboard_corpus/alembic_at.py`).

### 2.3 As built (§2–§4)

- **Capture is client-side, at the broker** (`frontend/composables/useArtifactRuntime.ts`):
  every SDK request's op, status, error code and duration, how streams end, plus
  what the app reports over the validated port (`artifact-sdk.js`: clicks named
  by role and label, uncaught errors, `console.error`). The host adds filter
  commits and render errors (`ArtifactFrame.vue`). No server-side route capture:
  the editor's log only feeds their own agent, so the broker's view is enough.
- **Who is logged:** the editor's report page (`ArtifactFrame`, not view-as) and
  the draft page; never published pages or host applications. The endpoint
  (`routes/artifact_activity.py`) takes an app session only (embed tokens get
  401) and someone editing a conversation of the artifact (others get 404).
- **Storage:** `artifact_activity_events` (`activity01`, idempotent). Detail is
  an allowlist per kind, bounded, PII-redacted; typed values are never sent.
  Retention: 7 days and the newest 500 per conversation, pruned on every write.
- **Context:** `<app_activity>` (`ai/context/sections/app_activity_section.py`)
  holds failures not yet shown, with up to 3 steps before each and the app
  version (`vN`). It is built once per turn and stamped with the turn's
  prompt completion. Tool: `read_artifact_activity`.
- **Preview:** every SDK call in BOW's preview is evidence (`sdk_calls`, failed
  ones in `errors`) and is kept with `source=preview`. Simulated
  conversations use the editor's real `policy()`, so `create({agents})` and
  `no_agents` fail exactly as live. Playwright's service-worker blocking
  error inside the sandboxed frame is ignored.
- **Verified live (Haiku 5.5):** editor hits `agent_forbidden` → next message
  "I got an error, fix it" → BOW reads `<app_activity>` and fixes it unprompted.
  Fresh conversation "check it works before I share" → BOW's preview finds
  `agent_forbidden` → "fix it" → edit + preview re-check passes → the
  editor's real question is answered (`media/pr/embedded-artifacts/70-…`, `71-…`).

## 3. Browser testing (BOW's own preview)

`backend/app/ai/tools/implementations/_artifact_browser.py` already opens the
artifact in Playwright and records evidence from the
`bow:artifact-evidence` window event (`__bowPreviewEvidence`), plus console and
page errors.

- The broker emits the same evidence event for **every SDK call** (op, status,
  error code, duration). The editor's log (§2) and the agent's preview then
  consume one stream; preview events are stored with `source=preview`.
- **Honest fixture.** In preview, read-only calls (`conversations.policy`,
  `list`, `messages`, `records.list`, runtime context) go to the real server.
  Only `send()`, `ai.stream` and writes stay simulated, so previews never spend
  money or change data. `create({agents})` is validated against the real
  policy.
- The agent can assert after its own clicks: "no failed requests after Send",
  "policy offers ≥1 agent", "the filter change re-ran N queries".
- Optional: one real `send()` only when the user asks ("try it for real").

This would have caught §0 before the user opened the app.

## 4. Context: how the agent gets it

- **Automatic, small.** When the editor sends a message and the app logged
  errors since their last message, the turn gets an "app activity" section:
  ≤10 lines, errors first, each with the 2–3 steps before it. For example:
  `clicked New conversation → conversations.create 403 agent_forbidden (Music Store) → "You can't use that agent here" shown`.
  No errors means no section. Plug it into the per-turn part of
  `app/ai/context/context_hub.py` (the warm refresh that rebuilds messages,
  queries and mentions), with a fixed budget of about 1.5k tokens.
- **On demand.** A tool, `read_artifact_activity(artifact_id, since?, kinds?,
  only_errors?, source?, event_id?)`, returns compact rows, and one event's
  detail when asked. It reads both `editor` and `preview` events.
- **Traceability.** Stamp `shown_in_completion_id` so each answer can be traced
  to what the agent saw, and the next turn adds only new events.
- **Guidance.** `create_artifact` / `edit_artifact` instructions: when the user
  says it doesn't work, read the activity before editing; after a fix, verify
  with a preview that has no failing requests.
- **"Fix with BOW".** On an app error in the editor, a small toast sends that
  event and its steps with one click.

## 5. Activity window (after §1–§4)

Editor-only side panel from the artifact frame menu (next to Resources and
Analytics in `components/dashboard/ArtifactFrame.vue`):

- one timeline, newest first: requests by op name, filters with query runs,
  resource writes, errors, click breadcrumb;
- row detail: redacted, truncated request/response; streams show their events;
- filter by kind, errors only, clear;
- **Ask BOW about this** on a row (attaches it and the steps before it).

It reads the §2 table and adds no plumbing. Session-only view; viewer history
would be telemetry, a separate privacy decision.

## 6. How others do it (checked 2026-10-29)

- **Lovable:** Agent mode "can inspect logs and network activity to identify
  and fix errors automatically", and debugs "without you needing to feed in the
  errors yourself". The agent pulls logs as a tool; there is also a "Try to fix"
  button. Nothing documented about clicks or session replay.
  [features](https://docs.lovable.dev/features),
  [Agent mode](https://lovable.dev/blog/agent-mode-beta),
  [console logs FAQ](https://lovable.dev/faq/development/best-practices/check-console-logs)
- **Replit:** "Ask AI" on a log entry, on demand only; browser logs stay in the
  preview's developer tools.
  [console](https://docs.replit.com/features/workspace-tools/console)
- **Bolt:** check the console yourself, then prompt. The open-source bolt.diy
  shows preview errors in an alert the user sends to the AI.
  [troubleshooting](https://support.bolt.new/troubleshooting/guide)
- **Clicks plus network** is session-replay and debugging tooling (PostHog,
  PlayerZero, Browserbase inspector, Loggy), not an app-builder pattern.

Take-away: capture like Lovable, let the agent pull, add a one-click "Fix with
BOW", keep clicks as a short breadcrumb before errors rather than a full replay.
Our advantage: every app request already passes through the broker, so the log
is complete for free.

## 7. Order and exit gates

| Step | Scope | Exit gate |
| --- | --- | --- |
| 1 | Conversation policy (§1) | e2e list in §1 green on SQLite + Postgres; live re-run of the user's Auto case answers with the agent |
| 2 | Activity table, server capture, client batch endpoint, retention (§2) | e2e: editor events stored, viewer and embed events never; redaction applied; sweep prunes |
| 3 | Agent context + `read_artifact_activity` + "Fix with BOW" (§4) | live: break an app on purpose, the next message carries the failure, BOW fixes it without the user pasting anything |
| 4 | Preview evidence for SDK calls + honest fixture (§3) | BOW's own preview reports `agent_forbidden` / `no_agents` before the user opens the app |
| 5 | Activity window (§5) | UI evidence en + he (ui-evidence skill) |

## 8. Guidance for whoever picks this up

- Start by reproducing §0 with an **Auto** report (the composer's default), not
  a report with an agent attached. That is how the first version passed every
  test and still failed for the user.
- Use the existing loop: `tools/agent/boot_stack.sh --dev` with a stable
  `BOW_ENCRYPTION_KEY` (a restart otherwise invalidates tokens and stored
  credentials), `tools/agent/seed_org.py --demo`, then the scripts in
  `tools/agent/embed/`. Real-model runs: key from an env var only.
- Prompts to BOW in live runs should be what a user would type ("the answers
  say it has no access"), never hints about the SDK.
- Don't hook `fetch` in the sandbox; capture at the broker and the routes.
- Keep viewer privacy as the default: editor sessions and agent previews only.
- Avoid in the dev stack:
  - an 8-worker pytest run next to `yarn dev` (OOM);
  - editing backend files while an answer is streaming (uvicorn reload waits
    on the open stream and stops answering);
  - a `pkill -f` pattern that matches your own shell command.

## 9. Open questions

1. Should the owner's agent limit default to the agents the report had when
   the app was first published, or stay Auto until set?
2. Is one shared pair of settings (agents, model) for app and bubble right, or
   does the bubble keep its own "data only" scope as a separate flag?
3. Retention: are 7 days and 500 events right, or should the log be cleared on
   publish?
4. Should viewers ever see a "something went wrong, report it" path that
   records an event (opt-in), for apps used in host applications?

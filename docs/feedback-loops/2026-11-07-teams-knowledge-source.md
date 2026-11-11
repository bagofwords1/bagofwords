# Feedback Loop — "Connect Microsoft Teams as a knowledge source, like email"

Goal: members can ask the agent about what their team said in Microsoft Teams
(channel threads and chats) the same way they ask about their Outlook mail. The
agent searches and reads **live, as the signed-in user**. It can only see the
channels and chats that user belongs to, and nothing is copied or indexed. This
is the email model (`outlook_mail`), and it is how Glean reads Slack. Glean
indexes Teams instead; we chose not to.

## What was built

| Layer | Where |
|---|---|
| Graph client: search, browse, thread read | `backend/app/data_sources/clients/graph_teams_client.py` |
| Chat capabilities | `backend/app/data_sources/clients/base.py` (`LIST_CHAT_MESSAGES`, `READ_CHAT_THREAD`, `SEARCH_CHAT_MESSAGES`) |
| Agent tools | `backend/app/ai/tools/implementations/chat_tools.py`, `backend/app/ai/tools/schemas/chat_tools.py` |
| Registry entry `teams_messages` (per-user, integration form) | `backend/app/schemas/data_source_registry.py` |
| "Sign in with Microsoft" scopes and OBO | `backend/app/services/connection_oauth_service.py` |
| Planner context (chat vocabulary, what Teams holds) | `backend/app/ai/context/sections/tables_schema_section.py`, `agent_roster.py`, `search_agents.py` |
| Files tab wording (messages, Live, newest first) | `frontend/components/datasources/ConnectionFileBrowser.vue`, `AgentFilesPanel.vue`, `locales/*.json` |
| Tool cards reuse SearchFiles / ListFiles / ReadFile | `frontend/pages/reports/[id]/index.vue`, `pages/c/[token]/index.vue`, `components/tools/*FilesTool.vue` |

Ids are self-describing (`channel:<team>/<channel>/<message>`,
`chat:<chat>/<message>`), so a search hit can be read without any lookup state.

## Loop A — deterministic (no Microsoft tenant)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/unit/test_graph_teams_client.py tests/unit/test_teams_schema_context.py \
  tests/unit/test_graph_connect_regressions.py tests/unit/test_schema_validation_file_fast_path.py -q
uv run pytest tests/e2e/test_teams_messages_connector.py -m e2e --db=sqlite -q
```

`FakeTeams`, in the unit test, implements the documented Graph behaviour:
- `$top` paging with `@odata.nextLink`.
- Channel roots come back with `$expand=replies`.
- A reply cannot be fetched by id (Graph answers 404).
- Microsoft Search pages with `from`/`size`, ANDs every word, and only shows
  conversations the user is a member of.

Mutation checks were run by breaking the code and watching the tests fail:
removing the reply→root scan, showing deleted messages, dropping the cursor
host check, reversing the chat window, turning off broadening, and dropping the
read-card naming.

## Loop B — live (real tenant, real user, Claude Haiku 5.5)

Secrets go in environment variables only. The flow, driven with Playwright
against `tools/agent/boot_stack.sh`:

1. **Admin.** Add connection, then **Microsoft Teams**, then the org's Entra app
   (tenant / client / secret). "Test credentials" answers *Connected
   successfully. Each user sees their own messages after signing in*. Save,
   which creates an org-wide agent with a **Sign in** badge.
2. **Member.** Click **Sign in**, which runs the real Microsoft login and
   consent and returns through `{base_url}/api/connections/oauth/callback`. The
   Files tab then lists *All conversations*, one folder per channel or chat,
   with *N recent messages · read live with your own access · nothing is
   stored*.
3. **Questions.** Each answer was checked against the seeded discussions:
   - *Q3 revenue and what drove EMEA* gave $4.82M (+18% QoQ), EMEA +31%, and
     Analytics Pro $1.1M of $1.9M, citing the thread link.
   - *Is October revenue on the dashboard accurate?* gave "Overstated by about
     $62K: duplicate `fct_orders` rows on Oct 3–5, use `fct_orders_dedup`, fix
     due Nov 12".
   - *Board deck: gross margin and the 2027 target* gave 71.4% (not 74%, which
     excluded hosting costs) and "$22M base / $24M stretch, agreed".
   - *Plans to cut the Q1 marketing budget?* As demo1 (not in that chat):
     nothing found. As demo2 (in the chat): the confidential message. Same
     agent, same question, so per-user access is enforced by Microsoft Search.

### What the live loop caught (all fixed, all with regression tests)

| Symptom in the real run | Root cause | Fix |
|---|---|---|
| "Test credentials" said *Connected but no tables found* | The pre-save validation only skipped the table check for file and mail capabilities (`connection_service._acount_files_for_validation`) | Count via the inventory path for `LIST_CHAT_MESSAGES` too |
| The agent searched "Q3 revenue EMEA growth", got 0 hits, and gave up | Microsoft Search ANDs every word | If a plain multi-word query finds nothing, retry with an `OR` of its meaningful words, and tell the agent (`broadened_query`) |
| The answer missed the cause, or the final "Agreed", next to a hit | A hit is one line; the answer to "And gross margin?" doesn't contain "margin" | Each hit carries the surrounding messages (Glean-style): the post it answers, 2 messages before and 3 after, and *"… N more after — read_chat_thread"* |
| The dashboard question never searched Teams | The planner context described Teams like a file share | A `<holds>` note (decisions, numbers, definitions, data-quality warnings): search before concluding the data is unavailable |
| The read card was titled with a raw message id | The read path treats ids containing `/` as file paths | `read_chat_thread` names the read after its conversation |
| The Files tab offered a broken preview | The inherited `GraphDriveClient.read_raw_bytes` (Outlook had the same bug) | Hide it on the Teams and Outlook clients |

### Sandbox gotchas (not product bugs)

- Set a stable `BOW_ENCRYPTION_KEY`. Otherwise every backend restart makes the
  stored LLM key and the Entra secret undecryptable.
- The root `bow-config.yaml` has `base_url: http://0.0.0.0:3000`, which the
  Entra app rejects as a redirect URI. Point `BOW_CONFIG_PATH` at a copy with
  `http://localhost:3000`.
- `main.py` runs uvicorn with `reload=True`. Editing backend files while it runs
  can leave orphaned workers holding `:8000` with the old environment. Kill
  every `backend/.venv/bin/python3` process before restarting.

## Not in scope / follow-ups

- Admin channel allow and deny lists, as in Glean. Needs config fields in the
  integration form, which today renders only credentials.
- Slack, the same tool family on a second client: the Real-time Search API, or
  the Slack MCP preset.
- Meeting transcripts, which are a separate Graph surface.

## Follow-up: agents vs connectors (Phase 0–3)

**Rule** (`backend/app/services/data_source_kind.py`): `data_sources.kind` is
`agent | connector | NULL`. An explicit value wins. NULL derives it: a data
source is a connector iff every connection is a connector type (MCP, or a
per-user integration: Outlook/Gmail mail, Teams, OneDrive, personal Drive). A
non-MCP connector type on `system_only` auth (e.g. Drive with a service account)
is an agent. Migration `dskind01` adds the column and keeps curated MCP data
sources (those with instructions) as agents.

**Run scope** (`resolve_run_agents`): Auto runs every agent and connector
the user can reach. A pinned selection is used as pinned (so pinning still
narrows the MCP tools a run offers) plus the user's OWN connectors (per-user
sign-in: mail, Teams, personal Drive, per-user MCP). Shared-key connectors
follow the selection like any agent. A scheduled run whose notifications reach
anyone but its owner drops personal connectors.

**LLM context**: connectors render in their own `<connectors>` block, never in
`<agents>`. The roster, focus and `set_report_agents`/`search_agents` ignore
them, and connector instructions are always in scope. Message/email reads no
longer leave a session file copy on the report.

**UI**: the prompt box has a puzzle-piece connectors menu (status dot or Sign
in, with "Browse connectors" at the bottom). The Browse modal handles search,
sign in, sign out, "Use as agent" (managers) and remove. Connectors are hidden
from the agent picker and the Knowledge Explorer, and have their own category
in the @-mention menu.

**Verified live** (Haiku 5.5, bow14 tenant): a Teams-only question, Music
Store + Teams in one turn, and Music Store pinned with a Teams question were
all answered correctly, with source attribution. Sign out → Not connected →
Microsoft sign-in → Connected round-trip. Tests: `tests/unit/test_data_source_kind.py`
(29), plus additions to the Teams e2e tests (kind round-trip, create flow,
422 on an invalid kind).

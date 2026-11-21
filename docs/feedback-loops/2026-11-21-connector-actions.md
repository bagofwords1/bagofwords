# Feedback Loop — Connector actions: write tools on Outlook and Teams, per-agent tool rules

Covers BOW-206…BOW-210 (CrowdStrike, BOW-211, is out of scope here). Three
claims are validated:

1. **Per-agent rules (BOW-206).** Two agents on one connection with different
   tool settings no longer merge to "strictest wins": a call goes through one
   agent and follows that agent's rule. The connection is the ceiling (a tool
   off there is off for every agent; a locked policy can only be tightened).
2. **One tool catalog with a Tools tab (BOW-207/208).** Outlook and Teams write
   actions are tool rows on the connection, with an effect
   (modify / external / destructive), safe defaults (always off), policies the
   effect allows, and the sign-in scopes the enabled actions need.
3. **Outlook and Teams actions (BOW-209/210)** run end to end with a real LLM
   (Claude Haiku 5.5) against a fake Microsoft 365, through the real OAuth code
   flow, the real policy layer and the approval card.

## Root cause (validated) — the "strictest wins" merge

A connection-tool call was identified only by `(connection, tool)`. With
several agents in a run, `strictest_admin_setting`
(`backend/app/services/tool_policy_service.py`, removed) merged every agent's
setting, in three places that had to agree: the tool list
(`backend/app/ai/tools/mcp_tool_registry.py`, `build_mcp_tool_catalog`), the
`<mcp_tools>` context (built from that catalog), and the call-time check
(`backend/app/ai/tools/implementations/execute_mcp.py`, `_enforce_policy` →
`ToolPolicyService.resolve_for_run`). One agent denying a tool hid it for all.

The fix replaces the merge with `pick_via_agent` (same file): among the run's
agents that carry the connection and enable the tool, the least strict one is
used, and the catalog's routing records it as `via_agent`. All three places
call the same function, so they cannot disagree.

## Loop A — deterministic (no external services)

LLM and Microsoft Graph are stubbed at the boundary (scripted planner;
`backend/tests/mocks/fake_microsoft365.py` behind `httpx.MockTransport`, which
enforces delegated scopes like Graph does).

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest -q tests/unit/test_tool_policy_pick_via_agent.py \
  tests/unit/test_connector_actions_graph.py \
  tests/unit/test_unknown_tool_points_to_builtin.py \
  tests/e2e/rbac/test_mcp_tools_cap.py tests/e2e/rbac/test_connector_actions.py
```

**Before the fix** (app code stashed, new tests kept):

```
FAILED test_mcp_tools_cap.py::test_a_shared_tool_follows_the_rule_of_the_agent_it_goes_through
FAILED test_mcp_tools_cap.py::test_a_tool_switched_off_on_the_connection_is_off_for_every_agent
FAILED test_mcp_tools_cap.py::test_a_locked_connection_policy_can_be_tightened_but_never_loosened[agent]
FAILED test_mcp_tools_cap.py::test_a_locked_connection_policy_can_be_tightened_but_never_loosened[user]
4 failed, 2 passed
```

(The two that pass guard behaviour that was already right: a tool no agent
enables stays hidden; members cannot change connection tools.)

**After:** the five files pass (67 tests), and the wider tool / connection /
Graph / OAuth suites pass: `1935 passed` across `tests/e2e/rbac/` plus the MCP,
custom API, Teams, Graph and OAuth unit/e2e files. The only errors were 6 in
`test_list_files_persist_per_user.py`, from this sandbox's proxy
(`Session.verify` is the CA bundle path), unrelated to this change.

## Loop B — live run (real LLM, fake Microsoft 365)

Needs an Anthropic key (env only). Microsoft is faked, so no tenant is needed.

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py
ANTHROPIC_API_KEY=... HAIKU_MODEL_ID=claude-haiku-5-5 uv run python ../tools/agent/setup_haiku_llm.py
uv run python ../tools/agent/mock_graph_server.py --port 8765 &      # fake Graph + identity
# restart the backend pointed at it (stable key so stored tokens survive restarts):
BOW_ENCRYPTION_KEY=<fernet key> BOW_GRAPH_BASE_URL=http://localhost:8765/v1.0 \
BOW_GRAPH_TOKEN_BASE_URL=http://localhost:8765 bash ../tools/agent/restart_backend.sh "$(pwd)/db/agent.db"
uv run python ../tools/agent/connector_actions_sandbox.py   # connections, agents, tools, OAuth sign-in
```

`connector_actions_sandbox.py` creates Outlook (agents "Inbox assistant" and
"Finance desk") and Teams (agent "SOC agent", posts allowed only to "Alerts"),
turns on: flag/move = allow, create_draft = ask, send_email = ask + locked,
post_message = ask; Finance desk turns all Outlook actions off for itself. It
then signs in through authorize → fake IdP → BOW callback. The permissions
endpoint confirms the token carries exactly the scopes the enabled actions need:

```
Outlook  granted: Mail.Read Mail.ReadWrite Mail.Send User.Read   missing: none
Teams    granted: … ChannelMessage.Send …   missing: send_chat_message → ChatMessage.Send (that tool is off)
```

**Outlook** — prompt: *"flag the overdue invoice email, move the lunch email to
Archive, then draft a reply saying we will pay on Friday."* Haiku searched,
flagged and moved without a prompt (allow), paused on the draft (ask; card shows
"Modify · Reply draft: …" and "Via Inbox assistant"), and after *Allow once* the
fake recorded exactly:

```
update msg-1 {flag: flagged} · move msg-3 → Archive (new id msg-11) · createReply msg-1 → draft msg-12
```

**Teams** — prompt: *"Post in Alerts: 3 detections closed. Then post the same
in General."* Alerts asked (card "External · Post to Alerts: …", via SOC agent)
and posted; General was refused by the connection's allowed-channel list, and
the agent said so instead of looking for another route. Only one write reached
the fake: `channel_post Alerts`.

Screenshots: `media/pr/connector-actions/` (before/after Tools tab, Teams, Hebrew
RTL, both approval cards and results, `outlook-flow.gif`).

### Found by the live loop and fixed

- Haiku generalised the `mcp__Outlook__…` naming of the write actions to the
  built-in read tool and called `mcp__Outlook__search_email` (4 failed calls,
  then gave up). The unknown-tool observation now names the real tool when the
  last part matches one the run offers (`agent_v2._unknown_tool_observation`,
  `tests/unit/test_unknown_tool_points_to_builtin.py`). Re-run: it searched with
  `search_email` and completed.
- The Outlook/Teams "Manage connection" opens the integration editor, not
  `EditConnectionModal`; the Tools tab was added there too
  (`ConnectionDetailModal.vue`). Changing the modal width while open blanked it;
  the width is now fixed per connection type.

## Loop C — two agents on one MCP connection, before vs after (live, Haiku 5.5)

A real streamable-HTTP MCP server (`tools/agent/mock_jira_mcp.py`, tools
`search_issues` / `create_issue` / `add_comment`, every call logged) behind one
"Jira" connection; `tools/agent/two_agents_one_mcp.py` seeds two agents on it:

| | create_issue | add_comment | search_issues |
|---|---|---|---|
| Ops agent | allow | allow | allow (default) |
| Finance agent | **deny** | **off** | allow (default) |

Prompt in every run: *"Create a Jira issue in the OPS project titled 'Rotate
reporting replica disk', then add a comment to OPS-42 saying a fix ticket was
opened."* "Before" is main (`711f317e`) run from a git worktree on :8001 against
a copy of the same database; "after" is this branch on :8000. Calls are what
the MCP server logged.

| Run | Calls that reached Jira | Agent's answer |
|---|---|---|
| **Before**, Ops + Finance | `search_issues` only | "The Jira connection only exposes search_issues, so I can't create the issue … or add a comment" |
| Before, Ops only (control) | `create_issue`, `add_comment` | both done |
| **After**, Ops + Finance | `create_issue`, `add_comment` — both recorded **via Ops agent**, no prompt | both done |
| After, Finance only | `search_issues` only | can't create or comment (Finance's own rule) |

The control shows that adding Finance to the report is what took Ops' tools
away before; after the change each call follows the agent that allows it, and
Finance alone still gets only what Finance allows. Screenshots:
`two-agents-ops-finance.png`, `two-agents-finance-only.png`.

## Review fixes (PR #1847)

| Finding | Fix | Test |
|---|---|---|
| P1 "Always allow" let a destructive tool run silently | `clamp_to_effect` caps the policy by effect at every resolution point (tool list, call time, external gateway, agent rows, `<mcp_tools>`), so stored preferences cannot bypass it; the confirmation handler does not save a forbidden preference; the card hides "Always allow" for destructive and locked tools | `tests/unit/test_policy_review_fixes.py` |
| P1 remote shell ignored a locked `ask` | `_lock_floor` applies the locked admin policy to the command-specific decision in `effective_shell_policy` and `display_policy` | same file |
| P1 card hid provider arguments starting with `_` | card context moved to a new `tool_confirmations.display` column (migration `tconfdisp01`) and `confirmation.display`; the card shows the arguments unfiltered | same file (payload) |
| P2 agent Tools panel filtered out Outlook/Teams | `KnowledgeExplorer` and `AgentKnowledgeTabs` include them; `ToolsSelector` labels them Built-in and hides refresh/remove | screenshot `agent-tools-panel.png` |
| P2 actions missing from `<mcp_tools>` | context builder uses `CALLABLE_TOOL_CONNECTION_TYPES` and the same ceiling/lock/effect resolution | `test_an_action_past_the_native_budget_is_still_listed_for_search` |
| P2 execution audit lacked the agent | `tool.mcp_executed` carries `via_agent` | — |
| P2 reply/forward drafts dropped CC | `ccRecipients` passed in every mode | `test_cc_reaches_the_draft_in_every_mode` |

Each new test was run against the pre-fix code: the CC and `<mcp_tools>` tests fail, and the policy file fails to import (`clamp_to_effect` did not exist). Called directly on the pre-fix code, both P1 cases resolved to `allow`:

```
old destructive + user allow -> allow          (now: ask)
old locked ask shell + personal allow -> allow (now: ask)
```

## The change (where)

- Policy: `backend/app/services/tool_policy_service.py` (`agent_admin_setting`,
  `pick_via_agent`, lock in `resolve_effective_policy`, `policy_not_allowed_for`).
- Catalog / call: `mcp_tool_registry.py` (routing `via_agent`),
  `execute_mcp.py` (via agent on card / result / audit; native calls need a row;
  `_preview` and `_effect` on the card).
- Actions: `backend/app/data_sources/clients/connector_actions.py` (base,
  effects, defaults, scope check), `graph_drive_client.graph_write` (one attempt,
  no retry), `graph_mail_client.py` (flag / move / draft / send),
  `graph_teams_client.py` (post / chat, allowed channels).
- Catalog sync and scopes: `connection_service.sync_native_actions`,
  `_refresh_action_scopes` (server-managed `config.action_scopes`, read by
  `connection_oauth_service.get_oauth_params`), `GET /connections/{id}/tools/permissions`.
- Migration `connact01`: `connection_tools.policy_locked`, `effect`, `source`.
- UI: `ConnectionToolsPanel.vue` (new), Tools tab in `EditConnectionModal.vue`
  and the integration editor in `ConnectionDetailModal.vue`, effect-limited
  policies and lock badge in `ToolsSelector.vue`, preview / effect / "Via
  <agent>" on the approval card in `MCPTool.vue`; strings in all 10 locales.

## Known gaps / follow-ups

- **Allowed-channel check runs after approval.** A post to a channel outside
  the list asks first, then is refused. It should be refused before asking
  (an action-level pre-check before the policy prompt).
- **"Always allow" is stored per user and tool, not per agent.** Under BOW-206 it
  should be keyed by the agent the call goes through.
- **Built-in read tools are listed but not rows.** `list_emails` etc. stay
  capability tools (always on); the Tools tab lists them read-only. Making them
  switchable rows is not done.
- **The org "MCP tools" switch also gates connector actions** (they run through
  `execute_mcp`). It is on by default.
- **PR #1786's agent cap** should take the cap from `via_agent`, not the
  strictest agent in the run.
- **Graph throttling:** a 429 on a write is reported, not retried. Graph
  throttles before executing, so one retry would be safe; it was left out for
  simplicity.
- **Gotcha:** this sandbox's backend runs with auto-reload. Editing a backend
  file during a chat makes the reload wait forever on the run's background
  task; restart with `kill -9` and `restart_backend.sh`.

# Feedback Loop — per-user MCP tool policies (allow / ask / deny / auto)

Validates the three-layer tool-policy system for MCP / custom-API tools:
connection default (`ConnectionTool.policy`) → per-agent overlay
(`DataSourceConnectionTool`) → **per-user preference**
(`user_connection_tool_preferences`, new). `deny` blocks and hides the tool,
`ask` pauses the run with an in-report approval card (the decision can be
remembered as the user's preference), `auto` delegates the decision to the
org's **small default model**, and an admin `deny` can never be relaxed by a
user. Before this change the internal run path enforced only `is_enabled` —
`confirm`/`deny` were silently ignored during report runs.

## Root cause / gaps closed (validated)

* `app/ai/tools/implementations/execute_mcp.py` checked only
  `ConnectionTool.is_enabled` — no policy enforcement in-run.
* The confirmation primitives (`tool.confirmation` SSE event,
  `app/ai/tools/confirmation.py`, frontend handler) existed but had **no
  emitter**; `wait_for_confirmation` auto-approved after 5s.
* `ConnectionService.refresh_tools` deleted *all* `ConnectionTool` rows when a
  provider returned an empty tool list — cascade-wiping overlays (and now
  user preferences). Guarded: the delete pass is skipped on empty discovery.
* Holding the agent session's transaction while awaiting approval deadlocked
  SQLite ("database is locked" on the resolve endpoint) — the tool now
  commits before long waits, mirroring `_release_db_between_steps`.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/unit/test_tool_policy_resolution.py \
              tests/e2e/rbac/test_rbac_tool_policies.py \
              tests/e2e/test_mcp_agent_tools.py \
              tests/e2e/test_mcp_tools.py \
              tests/e2e/test_custom_api_tools.py -q --db=sqlite
# 46 passed
```

Covers: resolution precedence (user pref wins; admin deny absolute; legacy
`confirm`→`ask`), admin-only writes at both admin layers (member gets 403 but
can read the list and write `…/my_policy`), gateway enforcement of the user
layer, `auto` failing closed with no LLM configured, the `ask` resolve
endpoint's authorization (only the run owner; `remember` persists the
preference), and the refresh-tools empty-result guard. Before the change,
`test_custom_api_tool_policy_update` demonstrated the old behavior (`confirm`
round-tripped raw); it now asserts normalization.

## Loop B — live confirmation (real LLM + real MCP servers)

Stack: `tools/agent/boot_stack.sh`, `seed_org.py`, an Anthropic provider from
`ANTHROPIC_KEY` (Claude Sonnet 5 default, Claude 4.5 Haiku as
`is_small_default` — the auto judge), a standalone FastMCP "mock workboard"
server on `:9500` (streamable HTTP), and a second MCP connection pointing BOW
at **its own** `/mcp` endpoint (API-key auth) — 13 self-tools discovered.

Policy matrix on the Workboard agent: `list_boards`/`get_board_items` allow,
`create_item` **ask**, `archive_board` **deny**, `delete_all_items` **auto**.

Observed (screenshots in `media/pr/claude-mcp-tool-permissions-user-ftsiyo/`):

1. **allow** — `list_boards` ran silently; no approval card (`10-run-allow`).
2. **ask** — `create_item` paused the run with the approval card (tool, args,
   Allow once / Always allow / Deny / Always deny; `11-run-ask-card`).
   "Always allow" resumed the run (`12-…`) and persisted
   `user_policy=allow`; a second run never prompted (`13-run-remembered`).
3. **auto approve** — benign `get_board_items` under `auto`: Haiku approved
   (conf 0.95, "read-only… matches user's task") and the verdict badge renders
   and survives rehydration (`14-run-auto-approved`).
4. **auto deny** — `create_item` with prompt-injection-shaped arguments was
   declined (conf 0.99, "prompt injection attack…"; `15-run-auto-denied`).
   Note: a *user-confirmed* destructive `delete_all_items` was **approved** by
   the judge — the LLM weighs explicit user intent
   (`15-run-auto-confirmed-delete`).
5. **deny** — `archive_board` is invisible to the planner (excluded from
   `<mcp_tools>` and `search_mcps`); the model states the tool doesn't exist
   (`15-run-deny`). Direct gateway calls are blocked
   (`blocked_by_policy='deny'`).
6. **per-user deny** — a member set `get_board_items → deny` for themselves;
   their run can't see the tool while the admin's runs are unaffected
   (`16-run-member-deny`). The member's tools panel is read-only for admin
   controls (no enable checkboxes, badge-only policy) but their "me" select
   works (`02-…`, `03-…`).
7. **BOW-as-MCP gateway** — over `/api/mcp`, `list_agent_tools` hides the
   denied tool, executing it is blocked, and the caller's remembered
   "always allow" applies (create_item ran although the admin layer says ask).

## What this proves / regression notes

The full policy lifecycle works end-to-end with a real planner LLM: default →
agent overlay → in-run enforcement → interactive approval → remembered
per-user preference → later runs honoring it, on both the internal planner
path and the external MCP gateway. `ask` in headless contexts (schedules,
platforms, evals) fails closed to deny, as does `auto` without a configured
model.

Pre-existing, unrelated: `execute_mcp` CSV materialization can fail with
`files.user_id NOT NULL` in agent runs (falls back to inline preview) —
reproduces without this change.

Known limits (documented, acceptable v1): pending approvals live in-process
(`PENDING_CONFIRMATIONS`), so multi-worker deployments need the resolve POST
to land on the worker running the completion; a page reload while a card is
pending loses the card (the run denies on the 240s timeout).

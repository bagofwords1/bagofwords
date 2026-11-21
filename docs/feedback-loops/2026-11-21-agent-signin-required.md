# Feedback Loop — "If I haven't signed in to an agent, does the LLM know and ask me to?"

Asked: when someone can use an agent but it needs their own sign-in, and they
haven't signed in yet, does chat surface that (an inline Connect prompt, or at
least "you need to sign in for sales"), or does it act as if the agent doesn't exist?

**Before:** the agent selector showed the agent greyed out with a Connect
button, but the LLM never knew it existed. Ask "What were total sales by region
last quarter?" and the answer was "no agent has sales data." Sign-in only came
up if the planner happened to call `search_agents`, and even then the Connect
button sat inside a collapsed tool row.

**After:** the planner is told which agents need this user's sign-in. It calls a
new `request_agent_signin` tool, and the user gets an inline Connect card. Once
they connect, **Ask again** re-runs the same question against that agent.

## Root cause (validated)

- `prepare_run_agents` (`backend/app/ai/tools/implementations/agent_focus_common.py`)
  skips any agent whose `construct_clients` returns a 403. A `user_required`
  connection with no credentials for this user (and no admin system fallback)
  raises `CredentialsRequiredError`, so the agent gets no client.
- `AgentV2.__init__` (`backend/app/ai/agent_v2.py`, the `_has_client` filter)
  then drops any agent without a client, and `build_rich_context`
  (`ai/tools/mcp/context.py:101`) only **logs** the skipped names. Nothing
  reaches the planner prompt.
- The only remaining signal was `search_agents`' `SIGN-IN REQUIRED` suffix,
  which the planner sees only if it chooses to search. In a small org (few
  agents) the planner never searches, because every schema is already inlined.
- Related edge case: when **every** agent fails, `AgentV2` skips the client
  filter (`if clients:`), so the agent the user can't query stayed in the
  schema block.

## Loop A — deterministic (no external services)

The LLM boundary is stubbed. Routes, services, context building, the tool and
the agent loop run for real.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/test_agent_signin_required.py -m e2e --db=sqlite -q
cd ../frontend && node tests/unit/useRunActivity.mjs
```

| Check | Fix stashed | With fix |
|---|---|---|
| member's planner prompt lists the agent in `<agents_needing_signin>` and nowhere else | FAIL `planner must be told which agents need the user's sign-in` | PASS |
| a user who can use the agent (admin system fallback) gets no block | PASS (negative case) | PASS |
| `request_agent_signin` returns the agent and ends the turn with the planner's message | FAIL `the sign-in request ends the turn` | PASS |
| ids that need no sign-in (usable agent, unknown id, or the admin's agent) are rejected and the turn continues | FAIL | PASS |
| a run ending on the sign-in card does not fold it into "Worked for …" (`useRunActivity.mjs`) | FAIL | PASS |

Nearby suites (focus/roster units, agent folders, MCP reach, smart instructions
planner prompt, `test_domains.py`) are green with the change.

## Loop B — live, through the UI as a real member

The stack ran via `tools/agent/boot_stack.sh --dev`, with a local Postgres 16
`sales` database:

- **Sales** — a Postgres agent with `auth_policy=user_required` and
  `allowed_user_auth_modes=["userpass"]`. Its system credentials (`bow_service`)
  act as the admin fallback; members sign in with their own Postgres login.
- **Inventory** — a `system_only` SQLite agent.
- **member@example.com** — invited member who has not signed in to Sales.

`tools/agent/signin_required_flow.mjs` drives Chromium as that member:

```bash
cd frontend
SALES_DB_PASSWORD=… node ../tools/agent/signin_required_flow.mjs ../media/pr/agent-signin-required           # after
node ../tools/agent/signin_required_flow.mjs ../media/pr/agent-signin-required --before                      # fix stashed
```

**LLM:** the sandbox's Anthropic key had no credit, so the run returned "credit
balance is too low". Loop B therefore uses `tools/agent/stub_llm_signin.py`, a
scripted OpenAI-compatible model registered with `setup_stub_llm.py`. It acts
**only on what the planner prompt shows it**:

- a sign-in block listing Sales → it calls `request_agent_signin`;
- the Sales `orders` schema in the prompt → it calls `create_data`;
- neither → "no agent has sales data".

So the before/after difference comes from the prompt this change builds, not
from the script. A run with a funded real model is still to do.

Observed (two consecutive passes after the fix; one before):

| Stage | Before | After |
|---|---|---|
| Reply to the sales question | "None of the agents available to me has sales data" | "Sales figures come from the **Sales** agent, which you haven't connected yet…" + Connect card |
| Connect | — | Opens "Connect Sales" (User / Password) |
| After Save | — | Card shows ✓ Connected + **Ask again** |
| Ask again | — | The same question re-runs; `create_data` queries the member's Postgres as `alice`: North America 55 400, LATAM 54 400, APAC 53 400, Europe 53 300 (matches `SELECT region, sum(amount) … GROUP BY region`) |

Evidence: `media/pr/agent-signin-required/` (`00-before-no-sales-data.png`,
`01…07-*.png`, `flow.gif`).

## The fix

- `backend/app/ai/context/agent_roster.py` — `render_signin_required_xml`:
  `<agents_needing_signin>` lists name and one-liner only (no schema), capped by
  `BOW_SIGNIN_REQUIRED_CAP` (10). It tells the planner to call
  `request_agent_signin` rather than answer from another agent.
- `backend/app/ai/agent_v2.py`:
  - remembers the run's agents before the client filter;
  - `_signin_required_agents()` resolves, once per run, which of them need this
    user's sign-in (via `signin_required_ids`; connectors excluded);
  - `_render_schemas_with_roster` keeps those agents out of the schema and
    roster and appends the block in every mode (all / focus / pick, and when no
    agent is usable at all).
- `backend/app/ai/tools/implementations/request_agent_signin.py` (+ schema), the
  new action tool:
  - re-checks the ids against this report's candidate agents and
    `signin_required_ids`, and rejects anything else back to the planner;
  - on success ends the turn (`analysis_complete`) with the planner's own
    message, in the user's language.
- `search_agents` now points its SIGN-IN REQUIRED hint at the tool.
- `frontend/components/tools/RequestAgentSigninTool.vue`:
  - one row per agent with a Connect button (OAuth-only connections redirect,
    the rest open `UserDataSourceCredentialsModal`);
  - re-reads live status on mount, so it survives an OAuth round-trip;
  - shows Connected + **Ask again**, which fires `prompt:rerun-for-completion`.
  `pages/reports/[id]/index.vue` maps the tool and reruns the user message that
  completion answered (the existing `rerunPrompt` gates apply).
- `frontend/composables/useRunActivity.ts` — a run ending on
  `request_agent_signin` is waiting on the user, like `clarify`, so it never
  folds.
- Strings: `tools.requestAgentSignin.*` in all 10 locale catalogs.

## What this proves / notes

- The planner learns about agents the user can't use yet, without ever seeing
  their schema, and the user can fix it inline in one click plus one re-run.
- **Scope:**
  - The block covers the run's own working set: Auto, and a pinned selection
    whose sign-in lapsed. Folder-scoped reports filter unusable agents earlier
    (`resolve_run_agents`) and are unchanged.
  - Connectors (personal mail/Teams/Drive) are left out; they have their own
    `<connectors>` block.
  - The shared-conversation view (`/c/<token>`) still renders the tool
    generically.
- **Sandbox pitfalls hit:**
  - `restart_backend.sh` without `BOW_ENCRYPTION_KEY` generates a fresh key, so
    stored LLM and connection credentials can no longer be decrypted ("Agent
    failed:"). Pin the key for multi-restart loops.
  - Editing the report page from a script outside the watcher left `nuxt dev`
    serving a stale page module; `touch` the file.

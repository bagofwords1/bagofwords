# Feedback Loop — stopping a shell approval looked like a timeout

A report containing SSH shell calls displayed the MCP logo, exposed planner
instructions in orange error paragraphs, and labelled a stopped approval as
“declined” / “approval request timed out.” This loop uses throwaway users and
commands; it never restarts the application or contacts a real SSH host.

## Root cause (validated)

- `backend/app/ai/tools/implementations/execute_mcp.py`: the approval loop broke
  on `sigkill_event`, then classified every absent response as a timeout. The
  same output string contained both the display error and planner guidance.
- `frontend/components/tools/MCPTool.vue`: the connection resolver knew the
  connection type, but the icon used only its catalog key and fell back to MCP.
  Failed calls rendered an error outside the expanded details.
- `frontend/pages/reports/[id]/index.vue`: response gaps and sibling margins
  accumulated; a stopped completion and its `run_stopped` event both rendered.

## Loop A — deterministic backend reproduction

Use Python 3.13 and the repository's normal backend dev dependencies:

```bash
cd backend
uv sync --frozen --extra dev
mkdir -p db
BOW_DATABASE_URL=sqlite:///db/test.db TESTING=true uv run pytest \
  tests/e2e/rbac/test_remote_tool_lifecycle.py \
  tests/e2e/rbac/test_remote_server.py --db=sqlite -q
BOW_DATABASE_URL=sqlite:///db/test.db TESTING=true uv run pytest \
  tests/unit/test_tool_confirmation_service.py \
  tests/unit/test_remote_server_client.py \
  tests/unit/test_remote_shell_policy.py \
  tests/unit/test_execute_mcp_routing.py -q
```

On base commit `83ba925f8`, copying the new lifecycle test into a clean checkout
and running its cancellation case failed with `KeyError: 'outcome'`: the tool
returned the old timeout-shaped failure instead of a cancellation. After the
fix, the lifecycle and connector suites passed **29 tests**, and the focused
unit suites passed **64 tests**.

The lifecycle tests create users, connections, agents and reports through the
API fixtures. They drive the public tool stream with real policy resolution,
confirmation rows and DB sessions. Only the SSH executable / transport boundary
is substituted. Admin and member cases cover cancellation during approval,
approval racing Stop, Stop immediately before dispatch, denial, expiry,
execution timeout, nonzero exit (preserving stderr), argument validation, and successful execution.
No sleeping or LLM credentials are needed.

## Loop B — full-stack UI confirmation

Boot a fresh stack using `tools/agent/boot_stack.sh --dev`, then seed a throwaway
user with `tools/agent/seed_org.py`. The evidence script defaults to isolated
frontend/backend ports 3016/8016; override them for the boot script's ports:

```bash
cd frontend
PLAYWRIGHT_BASE_URL=http://localhost:3000 BOW_BASE_URL=http://localhost:8000 \
BOW_TEST_EMAIL="$FIXTURE_EMAIL" BOW_TEST_PASSWORD="$FIXTURE_PASSWORD" \
  node tests/reports/remote-shell-evidence.mjs after
node tests/reports/tool-outcome.mjs
PLAYWRIGHT_BASE_URL=http://localhost:3000 \
  npx playwright test --config=playwright.i18n.config.ts --workers=2
```

The evidence script creates a real report over HTTP and injects deterministic
records into the production report renderer (no model). It checks English and
Hebrew, terminal icons, collapsed errors, expansion, command/output details,
approval request fields, production streaming handlers, canonical API replay on
reload without attached agents, genuine MCP fallback, and mobile overflow.
The approval HTTP reply and reloaded completion payload are stubbed; the actual
backend policy/confirmation paths are covered by Loop A.

For **before** evidence, use the base versions of `MCPTool.vue` and the report
page with the same fixture script, viewport and seed. Do not capture customer
reports. Evidence is under `media/pr/ssh-tool-lifecycle/` (before/after, expanded,
approval, successful output, Hebrew, mobile, and `flow.gif`).

Observed: both UI journeys passed with **zero page errors**, all **13 outcome
contracts** passed, and the **30 locale smoke tests** passed. All ten catalogs
received the same 14 translated keys without increasing pre-existing key drift.

The production build also passed. With the built server running on the selected
frontend port, `remote-shell-production.mjs` checks the public browser/API
boundary without Vue dev-mode introspection. English and Hebrew both passed:
terminal details, collapsed errors, cancellation, reload and mobile overflow,
with zero page errors. For example, use the same fixture credentials and URLs
as above with `node tests/reports/remote-shell-production.mjs`.

## The fix

- Persist `outcome` and `execution_state` separately from the generic runner
  status. Cancellation before dispatch is `cancelled` / `not_started`; an
  execution timeout is `execution_timed_out` / `unknown`.
- Check Stop again immediately before the wire call. An approval does not
  override a cancellation that arrived while it was being processed.
- Keep planner guidance in the observation; keep display errors concise.
  A transport timeout no longer recommends correcting valid arguments.
- Carry connection type/name and a safe host/port projection in progress and
  terminal outputs so the UI does not depend on an attached agent roster.
- Render shell commands through `RemoteShellTool.vue`, with terminal icon,
  command, localized status and duration. Details contain host, directory,
  full command, approval, stdout/stderr, exit code and error; errors stay hidden
  while collapsed. Other MCP errors also require expansion.
- Use one block gap, symmetric transcript padding, and one stopped indicator.

## What this proves / limits

The loop verifies the backend result contract and the production UI's handling
of it. Historical approvals have best-effort presentation using their saved
approval metadata and the stopped parent; explicit outcomes always take
precedence. Existing audit/confirmation history is retained.

This does not introduce durable remote jobs, prove that Stop kills a remote
process, or make self-restarts survive the app container. Those are separate
execution-lifecycle work. PostgreSQL and a real production restart were not run
locally (the Docker daemon was unavailable); the database tests used SQLite.

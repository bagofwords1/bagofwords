# Feedback Loop — MCP builder tools for agent managers (instructions + schema)

People who build agents (org instruction admins, agent owners, agent managers)
could only *create* and *delete* instructions over MCP, and the delete didn't
work for most of them. This loop adds `read_instruction`, `edit_instruction`,
`describe_tables` and `edit_schema`, and holds every MCP builder tool to the
web app's access rules, persona by persona.

The access matrix was written first (`backend/tests/mocks/mcp_acl_matrix.py`),
run against the old code to see what broke, then the tools were built until it
passed — in-process (pytest `TestClient`) and against a live server over real
HTTP, with Claude Haiku 5.5 driving the tools as each persona.

## Bugs found in the existing MCP tools (baseline run)

1. **`delete_instruction` refused the org admin.** It checked for the literal
   org-level `manage_instructions`; a `full_admin_access` owner doesn't hold
   that string, so the org owner got "Permission denied" on every delete. Agent
   owners and managers were refused too, though the tool was listed for them.
2. **`create_instruction` never checked the agents it targeted.** A plain
   member, a viewer, or a manager of one agent could attach a rule to any
   private agent in the org — including ones they can't see (`BETA`, another
   user's `OWNED`). It landed as a pending suggestion on that agent.
3. **`create_instruction` misreported the outcome.** Agent managers were told
   `pending_approval` when their rule had actually gone live (the tool guessed
   from org-level perms; the build layer already publishes for agent managers).
4. **Hidden tools could be called.** `tools/list` hid a tool from users without
   its permission, but `tools/call` didn't check: a plain member's direct
   `create_instruction` call succeeded.
5. **Latent service bug.** `InstructionService.update_instruction` reads
   `instruction.data_sources` without loading it; it only worked when the
   caller had loaded the row earlier in the same session. Any non-admin edit
   from MCP crashed with `lazy='raise'`. Fixed in the service.

## What changed

| Piece | Where |
|---|---|
| Authority rules lifted out of the REST routes into one shared module (REST behavior unchanged) | `backend/app/services/instruction_authority.py`, `backend/app/routes/instruction.py` |
| Call-time permission gate (same check as `tools/list`) | `backend/app/routes/mcp.py` |
| `read_instruction`, `edit_instruction` (new); `create_instruction`, `delete_instruction` (fixed) | `backend/app/ai/tools/mcp/instructions.py` |
| `describe_tables`, `edit_schema` (new) | `backend/app/ai/tools/mcp/schema_tools.py` |
| Eager-load `data_sources` in the update lookup | `backend/app/services/instruction_service.py` |

Rules, same as the web app:

- Instruction edit/delete: `manage_instructions` on **every** agent it's
  attached to; global rules need it org-wide. Re-scoping needs the same on the
  new scope (no promoting to global, no widening to an agent you don't manage).
- `describe_tables`: `view_schema` (any grant, or a public agent). Readers see
  only the agent's active tables; managers also see deselected ones.
- `edit_schema`: `manage` on the agent (owner, manager, group or connection
  grant).
- A missing, foreign or invisible id gets one answer, so ids can't be probed.
- `read_instruction` returns `can_edit`; `describe_tables` returns
  `can_edit_schema` — Haiku used both to explain refusals without trying.

## Compared with the web app and the in-app agent

Three ways to change an instruction exist; MCP now takes its **rights and
outcome from the web app** and its **edit input from the in-app agent**:

| | Web app (REST, what the UI calls) | In-app agent tool | MCP (now) |
|---|---|---|---|
| Create — who | every listed agent: `manage_instructions`; global: org-level | anyone with it somewhere; scoped to the session's agents | = web app (agent managers can't author a global rule) |
| Create — outcome | published | staged in an AI draft build, a person accepts | published |
| Create — gates | none | confidence ≥ 0.7, overfit critic | none (by decision) |
| Edit — text | full replace | **anchored**: `old_text` → `text`; whole rewrite only with `replace_entire_text`; result ≥ 20 chars | = in-app agent (same `_apply_anchor_edit`, same limits) |
| Edit — status | `published`/`draft`/`archived` | `active`/`inactive` | = in-app agent (`active`↔`published`, `inactive`↔`draft`) |
| Edit — who | every attached agent + the new scope | catalog gate only (review gate later) | = web app |
| Edit — outcome | published | staged suggestion | published |
| Delete | every attached agent; own drafts / own globals | — (no tool) | = web app |

Before this pass the first version published MCP edits as full-text replaces,
and still let agent managers create a global rule as a pending suggestion
(the web app refuses it). Both now match the table.

**How it's checked.** `test_mcp_instruction_parity.py` runs every create,
edit (rewrite, turn off, load mode, each scope change) and delete twice — once
through the web app's REST route, once through MCP — on twin instructions, for
all 11 personas, and requires the same answer *and* the same resulting text,
status, agent scope and live-ness. Against the previous MCP code it fails on
exactly the 5 agent-level managers creating a global rule, plus the anchor
contract. A contract test pins the in-app edit rules (`anchor_required` for a
missing or `""` anchor, `anchor_not_found` / `ambiguous_anchor` with the
current text echoed, whitespace-tolerant matching identical to the in-app
helper, `result_too_short`, `active`/`inactive`, `no_change`, no `disabled`
load mode).

## The cast

Built through the real API by `backend/tests/mocks/mcp_acl_world.py`:

| Persona | How they get access |
|---|---|
| `org_admin` | org owner (`full_admin_access`) |
| `instruction_admin` | custom org role: `manage_instructions` only |
| `agent_owner` | role `create_data_source`; created agent `OWNED` |
| `agent_manager` | resource grant `manage` on `ALPHA` |
| `agent_instruction_manager` | grant `manage_instructions` on `ALPHA` |
| `agent_evals_manager` | grant `manage_evals` on `ALPHA` |
| `agent_viewer` | member of `ALPHA` (view only) |
| `plain_member` | no grants (sees public `PUB` only) |
| `connection_manager` | `manage_data_sources` on `ALPHA`'s connection |
| `group_manager` | member of a group holding `manage` on `ALPHA` |
| `outsider` | admin of a different org |

Targets: agents `ALPHA`, `BETA` (private, admin's), `PUB` (public), `OWNED`;
instructions on each, one on `ALPHA`+`BETA`, and one global.

## Results

**In-process** — `test_mcp_builder_tools_acl.py` (9 tests, ~410 persona×target
cells) and `test_mcp_instruction_parity.py` (5 tests, ~400 REST-vs-MCP pairs);
every cell also checks the side effect over REST:

| Run | Result |
|---|---|
| ACL baseline (old code) | 9/9 failed — the 5 bugs above, plus the new tools missing |
| Parity against the first MCP version | create parity + anchor contract fail (see above) |
| Now | 14/14 pass |
| Break check: call-time gate disabled | `create_instruction` test fails on exactly the 3 cells only the gate protects |

**Live server over HTTP** (`tools/agent/mcp_acl_live.py matrix`, same 14 tests,
`httpx` instead of `TestClient`): 14/14 pass — [`matrix.json`](mcp-builder-tools-acl/matrix.json).
SSE transport (`Accept: text/event-stream`) spot-checked: same answers.

**Claude Haiku 5.5 as the MCP client** (`tools/agent/mcp_acl_live.py llm`):
7/8 — [`llm.json`](mcp-builder-tools-acl/llm.json). Graded on server state
(read back over REST) *and* the model's own final claim; server state was
right in all 8.

| Persona | Task | Outcome |
|---|---|---|
| agent_instruction_manager | change USD→EUR in one sentence, keep the rest | done with one anchored edit (`old_text: "Report amounts in USD."`); rest of the text intact |
| agent_manager | rewrite an `ALPHA` rule + deselect a table | done |
| agent_manager | make the `ALPHA` rule global | refused by `edit_instruction`; reported |
| agent_viewer | rewrite the `ALPHA` rule | no edit tool offered; read `can_edit=false`; reported |
| plain_member | deselect a `PUB` table | no `edit_schema` offered; saw `can_edit_schema=false`; reported |
| agent_owner | add a rule to `OWNED`, list its tables | done, live; graded FAIL only because the model ended without the `DONE:` line |
| instruction_admin | narrow the global rule to `PUB` | done |
| agent_instruction_manager | edit a rule shared with `BETA` | refused (needs every agent); reported |

## Open question (not changed)

An org-level instruction admin who isn't a member of a private agent can
**edit** an instruction on it, but can't **read** it — the web app hides it
from them (`GET /instructions/{id}` is "not viewable — even for admins"), and
the MCP tools mirror that. In the first Haiku run this showed up as the admin
being unable to find the rule it was asked to change. Worth a product call:
either let instruction admins read what they can edit, or stop them editing
blind.

## Reproduce

```bash
cd backend
# in-process
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true uv run pytest tests/e2e/rbac/test_mcp_builder_tools_acl.py tests/e2e/rbac/test_mcp_instruction_parity.py -n 8

# live server
export BOW_DATABASE_URL=sqlite:///$PWD/db/mcp_acl_live.db
uv run alembic upgrade head
BOW_ENCRYPTION_KEY=<fernet key> uv run python ../tools/agent/mcp_acl_live.py serve &   # :8765
uv run python ../tools/agent/mcp_acl_live.py matrix --out ../docs/feedback-loops/mcp-builder-tools-acl
ANTHROPIC_API_KEY=... uv run python ../tools/agent/mcp_acl_live.py llm --out ../docs/feedback-loops/mcp-builder-tools-acl
```

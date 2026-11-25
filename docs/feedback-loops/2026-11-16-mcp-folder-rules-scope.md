# Feedback loop — "MCP can't set folder-level instruction scope" (release 0.0.622)

A customer on 0.0.622 reported that `create_instruction` / `edit_instruction`
over MCP accept only `data_source_ids`, so a rule meant for a per-database
folder (`db-app`, one agent per environment) always lands on today's agents.
They asked for a `folder_id(s)` input.

## Verdict on the report

**The input exists in 0.0.622** (shipped in #1742; the `v0.0.622` tag carries
`folder_ids`, `add_folder_ids`, `remove_folder_ids` and `browse_agents`):

| Check (sandbox on `main` 65e8526f3, MCP files identical to `v0.0.622`) | Result |
|---|---|
| `tools/list` as org admin, folder manager and curator | `create_instruction`: `folder_id`, `folder_ids`, `scope`, `data_source_ids`. `edit_instruction`: `folder_ids`, `add_folder_ids`, `remove_folder_ids`, `scope`, `data_source_ids` |
| GPT client: "add one instruction to the db-app folder (not to individual agents!)…" | `create_instruction(folder_ids=[db-app])` → DB: folders `[db-app]`, agents `[]` |
| GPT client: "move it onto the db-usage folder instead … take it off the individual agents" | one `edit_instruction(add_folder_ids, remove_data_source_ids)` → folders `[db-usage]`, agents `[]` |
| A create with agents only | returns `folder_id: null, folder_ids: []` — the exact response quoted in the report |

The quoted response only exists on 0.0.622+, so the server was new and the
client was still using a tool list it cached before the upgrade (BOW's MCP
server sends no tools-changed notification). **Reconnecting the MCP client**
exposes the parameters.

## The real gap the loop found

The customer's goal is "one instruction per folder that auto-applies to every
environment agent inside it". Folder rules apply only in **reports scoped to
the folder** (`create_report agent_folder_ids`) — by design (see
`agent-folders.md`: a direct agent start activates that agent's context), and
so that a direct grant on one agent never hands out a private folder's rules.
MCP clients rarely scope that way, and nothing told them:

| Session (as an ordinary analyst, messy prompts) | Before | After |
|---|---|---|
| "total disputed $ on db-app prod this yr?" + 5 paraphrases, with a `db-app` folder rule "exclude QA merchant 7" | **6/6 wrong** ($123,869.87): 4 pinned to `db-app-prod`, 2 title-only; the rule never reached the session | **6/6 right** ($119,523.45): 3 scoped by folder at once, 3 pinned, read `missing_folder_rules`, and re-created the report with the folder |
| Admin: "add a rule on the db-app folder so all env agents incl future ones get it" | client told the admin it "will apply to agents in the folder, including new ones" | "applies to all agents in folder-scoped reports, including future agents" (from `applies_in`) |

Ground truth from the seeded SQLite: all 2026 disputes = 123,869.87; excluding
merchant 7 = 119,523.45.

## Fix

- `create_report` and `get_context` return `missing_folder_rules`
  (`folder_id`, `path`, `rule_count`, `agent_ids`) plus a hint naming the
  `agent_folder_ids` call, when the report uses agents filed in a folder whose
  live rules it does not apply. Only folders the caller can query are named.
- `create_instruction` / `edit_instruction` return `applies_in`; the
  `folder_ids` and `create_report` descriptions say when folder rules apply.
- Semantics unchanged: whether a folder rule should also apply to pinned or
  title-only reports that include the folder's agents is a product decision
  (it would extend folder knowledge to direct-grant users).

Test: `tests/e2e/rbac/test_mcp_folder_scope.py::test_a_report_that_misses_its_agents_folder_rules_says_so`
— failed before the change, passes after. It covers pinned, multi-agent and
title-only reports, folder-scoped reports (no entry), an inactive rule (not
"missing"), and a direct-grant guest (no folder named).

## Re-run

```bash
cd backend && uv run python ../docs/feedback-loops/mcp-agent-folders/seed.py
# per-database folders: db-app / db-usage, one agent per environment
OPENAI_API_KEY=... uv run python ../docs/feedback-loops/mcp-agent-folders/mcp_client.py \
  dana "total disputed \$ on db-app prod this yr?"
```

## Known side effects / not done

- A client that first pins an agent and then follows the hint leaves one
  unused report behind.
- A folder-scoped report covers every agent in the folder (prod and dev envs);
  narrowing a folder report to one environment while keeping its rules would
  need folder + agent intersection semantics, which reports don't have.

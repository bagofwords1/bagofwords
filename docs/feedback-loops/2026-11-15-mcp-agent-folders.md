# Feedback loop — agent folders in MCP report and instruction tools

MCP clients (Claude Desktop, Cursor, …) could not browse agent folders, could
not scope a report to one, and could only half-manage rules shared across
folders. One correctness bug sat underneath: `read_instruction` with a
`report_id` admitted a folder-only rule into every report, because it treated
"no agents" as global.

This loop reproduced that bug first, then built the feature until a GPT-driven
mock MCP client could do the whole lifecycle as five personas with messy,
human prompts — and the e2e suite held the same contract in-process.

## Bugs found (validated, each with a failing test first)

| # | Where | What a client saw | Tell |
|---|---|---|---|
| 1 | `backend/app/ai/tools/mcp/instructions.py` — `read_instruction` scope check (`instruction_scope.in_scope`) | A rule attached only to *Disputes* opened from an *Authorization* report; `get_context` for the same report did not list it | `in_scope` returned True whenever the rule had zero agents — every folder-only rule |
| 2 | `backend/app/services/folder_knowledge.py` — `can_read_knowledge_scope` | After a folder rule a report had used was detached from that folder, turned off, or deleted, the report became **404 for everyone, owner included** (`get_context`, forks, world views) | The gate required every recorded rule to still load *through that folder*; a detached/disabled/deleted rule never does |
| 3 | `instruction_service` detail schema → MCP `create/edit_instruction` | A curator's deletion-only edit was reported `approved` / `pending_review: false` while its build sat in `pending_approval` and `get_context` still served the old text | `suggestion_merge.covers()` treats a shorter suggestion that is a subsequence of the live text as "already applied" — true of every pure cut |
| 4 | `read_instruction` with `report_id` | Returned the *pending* row text while the report (and `get_context`) applied the approved version | Text came from the live row, not the build the report uses |
| 5 | `backend/app/services/build_service.py` — `promote_build` folder-scope gate | While a curator's edit on a Disputes+Authorization rule waited for review, the Disputes manager could not create **any** rule — even an agent-only one: `Folder scope unavailable`, and the new rule was rolled back (web app too) | Promotion re-syncs every rule whose live row differs from the build — including the other person's pending row — and demanded folder authority over all of them, moved or not |

Bug 3 also affects the web app's detail badge (`GET /instructions/{id}` says not
pending while `/instructions/pending-changes` lists it). The MCP tools no longer
depend on that badge; the web badge is left for a separate fix.

## What changed

| Piece | Where |
|---|---|
| `browse_agents` — browse/search folders and agents with ids and visible paths; hidden parents clipped; a direct agent grant reveals no parent or siblings; typo-tolerant, any-word fallback | `backend/app/ai/tools/mcp/folders.py`, `folder_service.browsable_folder_tree` |
| `create_report` takes `agent_folder_ids` + `data_source_ids` (mixed), persists the folders, returns `scope` + effective agents; title-only unchanged | `backend/app/ai/tools/mcp/create_report.py` |
| `get_context` returns `scope` (mode, folders with paths, pinned agents, effective agent ids) | `backend/app/ai/tools/mcp/get_context.py` |
| `read_instruction(report_id)` decides scope with the same builder `get_context` lists with, and returns the text the report applies | `instructions.py` |
| Every instruction view says `scope: global|targeted`, `folders`, `agents`, `hidden_target_count` (no private names) | `instruction_placement.py` |
| `create_instruction`: `folder_ids` (many) + `data_source_ids`; global only with explicit `scope: "global"`; `folder_id` still works | `instructions.py`, `schemas/mcp.py` |
| `edit_instruction`: `add/remove_folder_ids`, `add/remove_data_source_ids`, replace lists, `scope: "global"`; text-only edits keep placements; detaching the last target → `would_remove_last_target`; detaching the owning folder clears ownership | `instructions.py` |
| `list_instructions`: filters `folder_id`, `data_source_id`, `scope`; accepts `active`/`inactive` | `instructions.py` |
| Live-ness read from the main build, not the badge | `instructions._waiting_for_review` |
| Knowledge-scope gate refuses only on rules that still apply in the folder | `folder_knowledge.py` |
| Promotion needs folder/agent authority only over rules the build changes or moves | `build_service.promote_build` |
| `read_instruction.pending_review` uses the `/instructions/pending-changes` answer | `instructions._open_suggestion` |

Contract change for MCP clients: `create_instruction` without targets and
`edit_instruction` with `data_source_ids: []` no longer make a rule global; both
need `scope: "global"`. `tests/e2e/rbac/test_mcp_instruction_parity.py` now
sends that for the global cells.

## Loop A — deterministic (pytest, no external services)

```bash
cd backend
TESTING=true uv run pytest tests/e2e/rbac/test_mcp_folder_scope.py -m e2e --db=sqlite -n 4
```

`test_mcp_folder_scope.py` (11 tests, 13 cases) covers: the reported bug; one rule shared
across two folders and one agent — create, text edit, curator edit (pending),
detach one folder, refuse detaching the rest, explicit global — with retrieval
checked from four reports as a query-only user, a curator, a manager of one
target and a manager of all targets (`get_context` and `read_instruction` must
agree in every cell); explicit-destination create; list placements; hidden
targets; browse + folder/mixed/title-only reports; direct-grant leakage;
revoked folder never widening to Auto; reports staying readable after a rule
is turned off or deleted; curator cut/replace/insert all reported pending; a
curator's open suggestion not blocking another author.

| | Result |
|---|---|
| Before (bug repro, `read_instruction` unchanged) | reported bug test **failed** — `read_instruction disagrees with get_context` |
| Knowledge-gate fix stashed | lifecycle test **failed** — reports 404 after detaching a folder |
| Pending fix reverted | cut + replace-cut cases **failed**, insertion passed |
| Promotion fix absent | `test_a_pending_curator_change_does_not_block_other_authors` **failed** — `Folder scope unavailable` |
| After | **all pass** |

## Loop B — live sandbox + GPT mock MCP client

`docs/feedback-loops/mcp-agent-folders/`:

- `seed.py` — org *Acme Payments* through the real API: `Payments/{Disputes,
  Authorization}`, `Refunds`, `Marketing`, private `Executive`; six sqlite
  agents with real tables; personas `admin`, `dana` (query-only), `cory`
  (curator, custom role), `mo` (manages Disputes only), `alex` (manages every
  target), `gus` (direct grant on one Executive agent); an MCP API key each.
- `mcp_client.py` — a mock MCP client: JSON-RPC to `/api/mcp` with the
  persona's key, BOW's `tools/list` handed to `gpt-6-luna` (Responses API) as
  functions, generic system prompt that says nothing about folders.

```bash
cd backend && uv run python ../docs/feedback-loops/mcp-agent-folders/seed.py
OPENAI_API_KEY=... uv run python ../docs/feedback-loops/mcp-agent-folders/mcp_client.py \
  alex "hey can u add a rule for the disputes + auth stuff and also the refunds agent - amounts r stored in cents so always /100 before showing \$\$ to ppl. thx"
```

| Persona / prompt (verbatim) | What the client did | Verified |
|---|---|---|
| alex: "hey can u add a rule for the disputes + auth stuff and also the refunds agent - amounts r stored in cents…" | `browse_agents` ×4 → `create_instruction(folder_ids=[Disputes, Authorization], data_source_ids=[refunds])` | DB: `targeted`, both folders + agent, live |
| dana: "whats the total $ we lost on disputes this yr? only use the disputes folder stuff not everything" | `create_report(agent_folder_ids=[Disputes])` → `get_context` (rule listed) → `create_data` | Answer used `/100`: **$37,827.92** on 90 lost disputes |
| mo: "the cents->dollars rule shouldnt apply to disputes anymore, can u take it off disputes…" | `edit_instruction(remove_folder_ids=[Disputes])` | `permission_denied`; nothing changed; told the user why |
| cory: "…pls change 'major currency units' to just say dollars" | anchored cut | Found bug 3 (said "approved"); after the fix → `pending_approval`; reports keep the approved text |
| alex: "…pull it from authorization but keep it everywhere else" | `remove_folder_ids=[Authorization]` | Disputes + refunds kept; Dana's earlier reports still open (bug 2 fixed) |
| alex: "now unattach it from the disputes folder and from the refunds agent. just unlink it dont delete" | remove both | `would_remove_last_target`; client offered "turn it off or make it global" |
| admin: "the cents rule should apply to everything company wide…" | `edit_instruction(scope="global")` | `global` |
| cory: "add a note for both disputes and authorization: merchant_id 0 is our internal test merchant…" | any-word `browse_agents` → two-folder `create_instruction` | pending review (curator ≠ manager) |
| mo: "put a rule on disputes AND authorization: weekends dont count for SLA calcs" | two-folder create → refused (no rights on Authorization) → Disputes-only create | Found bug 5 (Disputes-only create also failed while Cory's suggestion was open); after the fix → created on Disputes, told the user Authorization was refused |
| gus: "start a report on the whole executive folder, i want kpis next to comp" | `browse_agents` finds no such folder → report on `exec_kpis` only | no folder, no comp agent, no "Executive" in any response |
| gus: "what data do i have access to here?" / "is there anything else in the executive area? comp data maybe?" | `browse_agents` | only `exec_kpis`, no folder, no sibling, no "Executive" |
| dana: "…which of our rules apply to auth? does that chargeback counting one apply here too?" | `list_instructions(folder_id=Authorization)` etc. | "scoped to Disputes, so it does not apply" |

Loop B also surfaced two client-realism misses, both fixed: a two-name search
("disputes auth") returned nothing (now falls back to any word), and
`list_instructions(status="inactive")` returned nothing (now accepts the
`edit_instruction` vocabulary).

## Not changed (by design)

- Folder rules apply only in reports whose scope selects that folder. A
  title-only report pins every agent individually, so a Disputes-folder rule
  does not apply there even though the disputes agent does — this is the web
  app's `instruction_scope` rule, and `get_context`/`read_instruction` agree.
- A manager of one target cannot detach the rule from their own folder; like
  the web app, any change to a shared rule needs authority over every target.

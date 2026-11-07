# Feedback loop — "the truncation of instruction is happening now on the MCP"

After the in-app `read_instruction` grep fix (#1489), a report said that long
instructions were also being cut off over MCP, where Claude Desktop, Claude Code
or Cursor connect to BOW's `/api/mcp`. This loop confirms the report. It found
three separate cuts, all on the MCP side. The in-app agent was not affected,
except for one edge case it shares with MCP: a single line too long for one
read.

## Root causes (validated)

| # | Where | What a client saw | Tell |
|---|---|---|---|
| 1 | `backend/app/ai/tools/mcp/instructions.py` — `list_instructions` | Every item's `text` was `item.text[:500]`, with no flag, no length and no hint in the tool description | A 3,099-char instruction listed as 500 chars, indistinguishable from a 500-char instruction |
| 2 | same file — `read_instruction` | The whole body went out as one tool result: 164,241 chars for a 160k instruction. MCP clients cap one tool result (Claude Code keeps ~25k tokens), so the client cut it, and the tool had no way to page or grep | `read_instruction` accepted only `instruction_id` |
| 3 | `backend/app/ai/tools/mcp/context.py` — `_select_tables_with_llm` | The table-selection prompt behind `create_data` / `inspect_data` / `create_artifact` / `edit_artifact` used `instructions_text[:2000]` | A rule like "revenue uses fct_orders" placed after char ~2,000 never reached the call that picks tables |
| 4 | `backend/app/ai/tools/implementations/read_instruction.py` `_window` (shared now) | A single line longer than one read was cut to its first chars, and the next line was appended straight after the cut. Paging then skipped the rest of the line | 60k-char one-line JSON blob: `text` = `{"k": 1, … "k": 1, \nend` |

Not affected: the JSON-RPC layer (`backend/app/routes/mcp.py` returns
`json.dumps(result)` whole), and the `[:500]` / `[:1000]` in
`mcp/base.py`, which only shorten the audit summary stored for the UI.

## The fix

- `backend/app/ai/tools/implementations/_instruction_window.py` (new): the
  windowed read (plain/head + outline, range, grep) moves out of the in-app
  tool so both `read_instruction` tools share one implementation. Budgets are
  unchanged (40k encoded chars per read, 16k head). A line too long for one
  read now comes back in pieces that fill the budget (sized by binary search,
  not the old worst case of 6 encoded chars per char). Each piece carries
  `clipped_line`, `clipped_line_length` and `next_col_offset`; `col_offset`
  continues the line, and nothing follows a cut piece.
- MCP `read_instruction`: takes `offset`, `col_offset`, `limit`,
  `grep_pattern`, `ignore_case`, `before`, `after` and `max_matches`, the
  in-app contract. It returns the window fields plus a `message` that says
  how to read on. An invalid regex returns `success: false`.
- MCP `list_instructions`: each item has `text_truncated` and `text_length`,
  and the description tells the client to call `read_instruction` for the
  full text. The 500-char preview stays, because the list can return 100
  items.
- Table selection gets the instructions in full. It is the same default model
  the coder step then sends the same text to.

## Loop A — deterministic (no external services)

```bash
cd backend
uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db
uv run pytest tests/e2e/test_mcp_long_instructions.py \
  tests/e2e/test_read_instruction_tool.py \
  tests/unit/test_mcp_table_selection_instructions.py -q -n 4
```

| | Result |
|---|---|
| Before (app changes stashed) | **19 failed**, 28 passed (the passes are pre-existing tests and small instructions) |
| After | **47 passed** |

The tests check general properties, not the reported example. The MCP tests
call `tools/call` on `/api/mcp` with a real API key. They check that list
text is a prefix of the live text, with `text_truncated` true exactly when it
is shorter, at lengths 30 to 20,000. They check that following `next_offset`
/ `next_col_offset` rebuilds the exact text at sizes 800 to 160,000, including
one-line blobs and non-ASCII text. They check that every response stays under
60k chars, that grep finds matches at the end of a body and deep inside a long
line, and that a rule anywhere in the instructions reaches the table-selection
prompt (LLM stubbed).

## Loop B — live, through a real MCP client

The backend runs on a fresh SQLite DB. The official `mcp` Python SDK connects
over streamable HTTP with an API key, as Claude Desktop or Cursor would.

```bash
cd backend
export BOW_DATABASE_URL=sqlite:///db/loop.db BOW_ENCRYPTION_KEY=<any Fernet key>
uv run alembic upgrade head
uv run uvicorn main:app --port 8000 &
uv run python ../docs/feedback-loops/mcp-instruction-truncation/seed.py   # admin, MCP on, API key, 3 instructions
uv run python ../docs/feedback-loops/mcp-instruction-truncation/probe.py
```

Full output: [`probe-before.txt`](mcp-instruction-truncation/probe-before.txt),
[`probe-after.txt`](mcp-instruction-truncation/probe-after.txt).

| Instruction | Before | After |
|---|---|---|
| medium (3,099 chars), in `list_instructions` | 500 chars shown, nothing marks it | `text_truncated=True text_length=3099` |
| long (160,079 chars), `read_instruction` | one 164,241-char result (above a client's per-result cap) | head + outline in 21,774 chars; 10 paged calls rebuild it exactly; grep finds the last line |
| giant one-line blob (120,048 chars) | one 150,348-char result | 5 calls (largest 40,872 chars) rebuild it exactly; grep finds the tail |

## Regression notes

- The in-app tool's behavior is unchanged except for the long-line case
  (pieces + `col_offset`) and the summary punctuation. Its existing suite
  (`tests/e2e/test_read_instruction_tool.py`) and the MCP parity/ACL suites
  pass unchanged.
- Not changed (follow-up): `grep_lines` runs the pattern with stdlib `re`
  on the event loop, in-app and now over MCP, so a catastrophic pattern can
  stall a worker. `app/utils/safe_regex.py` has the time-bounded matcher to
  reuse.
- Not a truncation, noted for scope: with the org setting "Agent chooses smart
  instructions" (`smart_instructions_on_demand`, default off) turned on, the
  MCP data tools' coder gets only `always` instructions. Every `intelligent`
  one is catalog-only (`instruction_context_builder.py`, "NEVER inline"), and
  the MCP path has no planner to `read_instruction` them, so
  `InstructionsSection.read_items` stays empty.
- Broad check: `tests/unit` plus the 55 MCP/instruction/skill/planner e2e
  modules ran with 11,109 passed. The 199 failures and 1 error are all in
  SharePoint, OBO-login, Sybase, Fabric and discovery-progress tests. The same
  files give the identical 199 failed / 1 error with this change stashed (this
  sandbox lacks `libodbc.so.2` and similar), so they are unrelated.

## Part 2: the instructions in scope, listed by `get_context`

Follow-up in the same PR. Before this, `get_context` returned no instructions at
all. An MCP client (which plans the work itself) learned the org's rules only
if it went looking through `list_instructions`, a management tool that covers
the whole org instead of the report's agents and cuts text at 500 characters.

| Piece | Behavior |
|---|---|
| `get_context` → `instructions` | Every instruction in the report's scope: global ones plus those on the report's agents, filtered by status, mode/channel and the user's table access. Built with the in-app `InstructionContextBuilder` (`build(on_demand=True)`), so scoping matches the agent. `always` instructions come with their text, most-used first, up to `INLINE_INSTRUCTIONS_BUDGET` = 100,000 encoded chars. Past it they are listed as `required: true` with no text. Smart instructions and skills are listed without text (`title`, `description`, `tables`, `references`, `tokens`). Optional entries are capped at 100, with `instructions_total` and `instructions_truncated`. Required entries are never dropped. |
| `read_instruction` | `instruction_ids` reads up to 20 at once, within a 100,000-char budget; the rest come back in `not_read_ids`, unknown or out-of-scope ones in `not_found_ids`. Window inputs work only with a single `instruction_id`. `report_id` limits reads to that report's scope and records an `on_demand` usage event for the report (so reads count toward ranking, and with smart instructions on, `ContextHub._load_prior_instruction_reads` hands them to the coder). |
| `create_data` / `inspect_data` / `create_artifact` / `edit_artifact` | `instruction_ids`: the instructions the client read go to the code writer in full (`ContextHub.add_instructions_for_coder`), scoped to the report by `load_instructions_by_ids`. With smart instructions off they are added to the keyword-matched ones. With smart instructions on they are the smart instructions the coder gets, which fixes the gap where the MCP coder got none. |
| Shared scope | `backend/app/ai/tools/mcp/instruction_scope.py`: one builder and scope check used by all three paths. |

**Loop A:** `uv run pytest tests/e2e/test_mcp_instruction_catalog.py -n 4`. All
14 fail before the change (no `instructions` field, no batch input, no
`instruction_ids`, no usage event) and pass after. Related suites (57 e2e +
22 unit modules on MCP, instructions, skills, smart instructions and the
context hub): 834 passed, 0 failed.

**Loop B (live, official `mcp` client):** `catalog_live.py` seeds two agents
(sales in the report, hr out of it) and six instructions, including a
111k-char `always` catalogue. Output in `catalog-live-output.txt`:
`get_context` is 3,617 chars and lists 9 entries. The 111k catalogue is
`required` without text (over budget), `hr_smart` is not listed, and a batch
read of all 7 text-less entries is 57,201 chars. A scoped read of `hr_smart`
is refused ("not found in this report"); without `report_id` it is readable,
as before.

**Notes**
- 100k chars of inline `always` text is about 25k tokens, the same as Claude
  Code's default per-result limit for MCP tools (`MAX_MCP_OUTPUT_TOKENS`).
  An org that fills the budget, on a report with many tables, can push
  `get_context` past what that client keeps. The constant is
  `INLINE_INSTRUCTIONS_BUDGET` in `backend/app/ai/tools/mcp/get_context.py`.
- MCP instructions resolve as channel `app`, because the MCP data tools'
  `ContextHub` has no head completion. An instruction scoped only to the `mcp`
  channel applies to neither. `get_context` follows the same rule
  (`MCP_INSTRUCTION_CHANNEL`) so it lists what the tools apply. Moving both to
  `mcp` is a separate change.

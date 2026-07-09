# Feedback Loop — connection tool calls show "notion_search" instead of "Searching Notion for customer"

New agentic UIs (Claude Code, Codex) label each tool call with a short,
human-readable line — "Searching Notion for churned customers" — instead of the
raw tool identifier. In BOW the long-tail connection tools (MCP, file sources,
web fetch) rendered the mechanical name/connection instead: an `execute_mcp`
call showed just `Notion`, and two different Notion calls in one turn were
indistinguishable. This loop validates the fix: connection tools now carry a
model-authored `title` that the chat UI renders in place of the raw name.

## Root cause (validated)

The rendering was never the problem — the *data* was missing. Connection tools
had no field for a human label, so the UI could only fall back to mechanical
strings:

- `execute_mcp` input schema exposed only `connection_id` / `tool_name` /
  `arguments` (`backend/app/ai/tools/schemas/execute_mcp.py`) — nothing the
  model could use to describe intent.
- `MCPTool.vue`'s `doneLabel` therefore fell back to the connection name
  (`frontend/components/tools/MCPTool.vue:148` in the pre-change file) → every
  Notion call rendered as `Notion`.
- The planner persists a tool call's raw arguments verbatim as
  `arguments_json` (`start_tool_execution_from_models`,
  `backend/app/project_manager.py:1265`), and the UI reads
  `arguments_json.*` — so a `title` argument, once added to the schema, flows
  end to end with no plumbing changes.

## The fix

Add an optional, model-authored `title` to every connection tool's input schema
and render it as the live status line, keeping each tool's existing icon/detail.

- Schemas gain `title` (`execute_mcp.py`, `search_mcps.py`, `file_tools.py` for
  list/read/search/write/attach, `web_fetch.py`). The field description tells
  the model to write a 3–6 word active-voice label and to omit ids.
- `prompt_builder_v3._build_system` gains one static COMMUNICATION line naming
  the connection tools and the labeling convention (cache-safe — it lives in the
  cached system prefix and never changes per turn).
- Frontend renders `arguments_json.title` when present, else the old label:
  `MCPTool.vue` (brand icon preserved), `GenericTool.vue` (universal fallback),
  and the file/web components (`ListFilesTool`, `ReadFileTool`,
  `SearchFilesTool`, `WebFetchTool`).

The hero tools (`create_data` / `create_artifact` / `edit_artifact`) are left
untouched — their staged progress UI is more informative than a one-line title.

## Loop A — deterministic reproduction (no external services)

`backend/tests/unit/test_tool_call_titles.py` pins the contract over the whole
connection-tool family: every tool advertises an optional string `title` in the
planner-facing schema, a model-authored title round-trips through the persisted
`arguments_json`, and omitting it never errors.

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
.venv/bin/python -m pytest tests/unit/test_tool_call_titles.py -q
```

**Observed (PASS):** `24 passed`. Negative control: `describe_tables` (an
untouched tool) has no `title` property — so the parametrized suite genuinely
discriminates and fails for any connection tool that forgets the field.

To watch it fail: stash the schema edits (`git stash -- backend/app/ai/tools/schemas`)
and re-run — every `test_connection_tool_advertises_optional_title[...]` case
fails on the missing `title` property.

## Loop A' — real-UI rendering (seeded, no LLM)

Because a completion normally requires a live LLM, the UI is exercised by
seeding a report whose assistant turn contains connection-tool executions with
titles (`backend/scripts/seed_tool_title_demo.py`), then screenshotting the real
report page.

```bash
tools/agent/boot_stack.sh --dev
cd backend && export TESTING=true TEST_DATABASE_URL="sqlite:///db/agent.db"
.venv/bin/python scripts/seed_tool_title_demo.py                 # after (with titles)
SEED_NO_TITLES=1 .venv/bin/python scripts/seed_tool_title_demo.py # before (fallback)
cd ../frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
  node ../tools/agent/shoot_tool_titles.mjs <report_url> ../media/pr/tool-titles-after.png
```

**Observed:** the API serves `arguments_json.title` on every block, and the UI
renders it. Before/after evidence in `media/pr/tool-titles-before.png` and
`media/pr/tool-titles-after.png`:

| before | after |
| --- | --- |
| `Found 2 tool(s)` | `Finding available Notion tools (2)` |
| `Notion` | `Searching Notion for churned customers` |
| `Notion` | `Reading the Churn Playbook page` |
| `Fetched https://example.com/pricing` | `Reading the pricing page` |

The two indistinguishable `Notion` lines become two distinct, legible actions.

## Loop B — live LLM confirmation (real Anthropic Haiku)

Confirms the LLM-facing contract: given only the real tool metadata (now
carrying `title`), Haiku fills `title` with a human-readable label on every
connection-tool call across a multi-step run.

```bash
cd backend
export ANTHROPIC_API_KEY=...    # a Haiku-capable key; never commit it
.venv/bin/python scripts/tool_call_titles_live_agent.py
```

The script drives `claude-haiku-4-5` through search_mcps → execute_mcp
(notion_search) → execute_mcp (notion_get_page) → web_fetch against in-memory
fakes, and asserts each connection call carries a non-empty title that is a
phrase (≥2 words) and never echoes the raw tool name. Prints `LIVE E2E: PASS`.

> Requires `ANTHROPIC_API_KEY`. It is intentionally not set in the base sandbox;
> the script exits 2 with a clear message when the key is absent.

## What this proves / regression notes

- **App:** a single optional schema field turns every present and future
  connection tool's status line from a mechanical name into a plain-language
  label, with no change to the persistence path — the planner already stores raw
  arguments and the UI already reads them.
- **Safety of the default:** the field is optional everywhere; a call without a
  title renders exactly as before (verified by the before screenshot and the
  `test_title_is_optional_at_construction` cases).
- **Live:** a real Haiku model produces good titles from the schema alone.

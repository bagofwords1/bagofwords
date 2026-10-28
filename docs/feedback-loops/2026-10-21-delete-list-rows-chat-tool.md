# Feedback Loop — "can we delete records from list via chat mode?"

**Status: IMPLEMENTED and VERIFIED (2026-10-21).** Loop A passes on SQLite; Loop B ran the full stack
(production frontend, real backend, real browser) with a **scripted** OpenAI-compatible model. A run
with a real model was not possible in this sandbox (the Anthropic key had no credit and the AWS
credentials were rejected), so the model's *choice* to call the tool is not covered here.

Before this change the agent could create lists, change their columns and insert/update rows, but
not delete rows: deletion existed only on the list page and its REST routes. The claim validated
here: in chat (and training) mode the agent can delete list rows, the user confirms on a card first,
and only the confirmed rows are deleted.

## Root cause (validated)

A missing capability, not a bug. `submit_<slug>` only inserts / updates / leaves rows unchanged
(`backend/app/services/agent_lists/records.py:346-464`); the only callers of
`services/agent_lists/service.py:delete_rows` were the REST routes
`DELETE …/rows/{row_id}` and `POST …/rows/delete` in `backend/app/routes/agent_lists.py`.

## The change

| Piece | Where |
|---|---|
| Tool schema (`row_ids` / `keys` / `all`, exactly one) | `backend/app/ai/tools/schemas/delete_list_rows.py` |
| Tool (chat + training, MANAGE on the owning agent, match → confirm → delete) | `backend/app/ai/tools/implementations/delete_list_rows.py` |
| `find_list` moved to shared helpers (used by `edit_list` too) | `backend/app/ai/tools/implementations/_list_tool_common.py` |
| Blocked for sub-agents | `backend/app/ai/sub_agent_profiles.py` |
| Training prompt line | `backend/app/ai/agents/planner/prompt_builder_v3.py` |
| Chat card (rows to delete, Delete / Cancel, en/es/he) | `frontend/components/tools/DeleteListRowsTool.vue`, mapped in `pages/reports/[id]/index.vue` and `pages/c/[token]/index.vue`; `locales/*.json` `tools.deleteListRows.*` |
| Design note | `docs/design/structured-extraction.md` §7.1 |

Behavior: rows are matched first (keys are normalized like submissions — case and extra spaces
ignored). No match → no card. Otherwise the card lists up to 20 rows (key first) with the count and
the list's total; the agent waits on `stream_user_confirmation` (240 s). Approval deletes exactly the
matched row ids through `delete_rows` on its own session, so a row added while the card is open is
kept. Cancel / timeout deletes nothing. Shared-artifact chat is refused.

## Loop A — deterministic (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/rbac/test_delete_list_rows_tool.py tests/e2e/rbac/test_list_schema_tools.py -m e2e --db=sqlite -q
```

Observed: `33 passed` (13 new + the 20 existing list-tool tests, unchanged after moving `find_list`).
The new tests drive the tool as AgentV2 does and answer the card through `ToolConfirmationService`:
catalog (chat + training, blocked for sub-agents); viewer refused with no card; invalid selections
and no-match show no card; cancel deletes nothing and returns the note; delete by key (admin and
manager) removes only those rows and reports the missing key; delete by row id; clear-all keeps the
schema; keys on a list without a key field are refused.

Can it fail? With the approval check disabled (`if False:`), the cancel test fails:
`assert (True is False)` — `1 failed, 12 passed`.

## Loop B — full stack, scripted model, real UI

```bash
BOW_ENCRYPTION_KEY=<stable fernet key> tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py --demo
# scripted model: always calls delete_list_rows once, then answers
STUB_DELETE_ARGS='{"list_id":"Vendors","keys":["Acme Corp","Initech"],"reason":"User asked to remove Acme Corp and Initech."}' \
  uv run python ../tools/agent/stub_llm_delete_rows.py &
# provider + "Vendors" list with 6 rows + chat report + run
BOW_ENCRYPTION_KEY=... TEST_DATABASE_URL=sqlite:///db/agent.db TESTING=true \
  uv run python ../docs/feedback-loops/2026-10-21-delete-list-rows-setup.py      # prints REPORT_ID
cd ../frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers BOW_CAPTURE_HE=1 \
  node tests/lists/delete-list-rows-flow.mjs <REPORT_ID> ../media/pr/ai-clever-goldberg-b58zui/approve
# second run, --skip-provider on the setup, then the driver with --cancel
```

Prompt (chat mode): *"Please delete Acme Corp and Initech from the Vendors list."*

Observed:

```
approve: confirm card visible; preview rows: 2 badge: 2 of 6 rows
         result: Deleted 2 rows from Vendors
         rows via API: total 4 ['globex', 'hooli', 'stark industries', 'umbrella']
cancel:  confirm card visible; preview rows: 2 badge: 2 of 6 rows
         result: Delete cancelled — nothing was deleted
         rows via API: total 6 (all kept)
```

Evidence (`media/pr/ai-clever-goldberg-b58zui/`): `approve/01-confirm-page.png` (card in a chat-mode
report), `approve/01-confirm-card.png`, `approve/02-result-card.png`, `approve/04-card-he.png` (RTL),
`approve/05-card-mobile.png`, `cancel/02-result-card.png`.

### Found and fixed on the way

The first Loop B run showed the finished card reading **"2 of 0 rows"** with no key icon: the total
and columns came only from the confirmation payload, which is gone once the tool ends. The tool
result now carries `total` and `columns`; Loop A asserts `total`, and the re-run shows "2 of 6 rows".

### Environment notes

- The sandbox Anthropic key returned `credit balance is too low`; AWS credentials returned
  `UnrecognizedClientException`. With a working key, run the setup with
  `--base-url <gateway> --model <id>` (or register the provider in the UI) and the same prompt.
- `boot_stack.sh --stop` reported the backend stopped while it kept running; a `kill -9` was needed
  before the restart could bind :8000.

## What this proves / regression notes

From chat, an agent manager can have rows deleted from a list only after confirming on a card that
shows them; exactly those rows go, cancel keeps everything, and viewers get no card. Not covered:
a real model's tool choice (blocked on credentials), the Postgres leg of Loop A (CI runs it), and
the shared `/c/<token>` read-only rendering (mapped, not screenshotted).

# Feedback Loop — MCP 'ask' approvals stopped working under multiple workers

The approval card for an `ask`-policy MCP tool did nothing when clicked: Allow
once / Always allow / Deny / Always deny left the card on screen, the run kept
spinning, and ~4 minutes later it failed with *"the approval request timed
out"* — even though the user answered in under two seconds.

## Root cause

Pending approvals lived in per-process dicts (`PENDING_CONFIRMATIONS` /
`PENDING_CONFIRMATION_META` in `app/ai/tools/confirmation.py`), but the app
serves requests from several uvicorn workers (`start.sh` passes `--workers`,
default `min(CPUs, 4)`; `main.py` uses 20 in dev). The run registers its future
in **its own** worker; the approval POST is load-balanced to **any** worker.
On any other worker `get_confirmation_meta` returns `None`, so the endpoint
answered `404 Confirmation not found or expired`. The frontend logged that to
the console and nothing else — indistinguishable from a dead button.

## Loop A — reproduction (real LLM + real MCP server, 4 workers)

Stack: `uvicorn main:app --workers 4` on SQLite, `yarn dev` frontend, an
Anthropic provider with **Claude Opus 4.8** as the org default (Haiku 4.5 as
small default), and a standalone FastMCP "workboard" server on `:9500` exposing
`list_boards` / `get_board_items` (policy `allow`) and `create_item`
(policy **`ask`**). Playwright drives the real chat UI and logs every `/api/`
response.

| Experiment | Before |
|---|---|
| 5 UI runs, click "Allow once" | 4 resolved, **1 froze** (`404`) |
| Same pending confirmation, 10 POSTs over fresh TCP connections | **1×200, 9×404** |
| 4 UI runs with `--workers 1` | 4/4 resolved |

The 9-of-10 probe is the proof: one confirmation id, one completion id, valid
auth — only the worker that registered it could see it. Browser clicks mostly
worked only because keep-alive tends to pin them to the worker already
streaming the run; anything that breaks connection reuse (proxy pool, HTTP/2,
reload, multiple pods) fails.

Aftermath of a frozen click, from the log: the run hung 11:45:23 → 11:49:24
(the 240s `ask` timeout), reported `blocked_by_policy: ask`, and the model told
the user the item wasn't created. The mock server confirms `create_item` never
fired.

## The fix

`tool_confirmations` (new table) is now the source of truth:

* `execute_mcp` inserts a pending row before emitting `tool.confirmation`, then
  polls it every 3s while waiting (`_ASK_POLL_S`). The in-process future is kept
  purely as a same-worker fast path.
* The resolve endpoint writes the decision to the row — a conditional
  `UPDATE … WHERE status='pending'` so two racing clicks yield one decision —
  and is **idempotent**: a second click returns `already_resolved: true` with
  the decision on record instead of a 404.
* When the run stops waiting it marks a still-pending row `expired` (after one
  final read, so a click inside the last poll window is still honored). A late
  click then gets `410`, not silence.
* `MCPTool.vue` surfaces a failed resolve inline (`mcp-approval-error`) and
  leaves the buttons clickable, instead of swallowing the error in a
  `console.warn`.

## Loop B — verification (same 4-worker stack)

```
5 UI runs, click "Allow once"     → 5/5 resolved; 4 woken by the DB poll,
                                    i.e. the approval landed on another worker
                                    (the case that used to freeze)
10 POSTs over fresh connections   → 1×200 then 5×200 already_resolved (was 9×404)
Deny once                         → run resumes declined, row status='denied'
Always allow                      → row remember=1, user preference persisted,
                                    next run executes with no card at all
Unanswered card                   → row pending → expired at the 240s timeout;
                                    late click → 410 "Confirmation expired"
Forced 410 in the UI              → inline "This approval request expired…",
                                    buttons still enabled for a retry
```

Suites: `tests/unit/test_tool_policy_resolution.py`,
`tests/e2e/rbac/test_rbac_tool_policies.py` (+2 new tests: resolution with no
local future, and expired → 410), `tests/e2e/test_custom_api_tools.py`,
`tests/e2e/test_mcp_agent_tools.py`, `tests/e2e/test_mcp_tools.py`,
`tests/unit/test_execute_mcp_routing.py`, `tests/unit/test_mcp_context_injection.py`
— 91 passed. Migration `toolconf01` round-trips (downgrade + upgrade).

## Sandbox notes (not part of this change)

* **`BOW_ENCRYPTION_KEY` unset + multiple workers = random 401s.** It defaults
  to a per-process generated Fernet key (`bow_config.py`), which is also the JWT
  secret (`core/auth.py`), so each worker signs with a different secret and a
  token only validates on the worker that issued it. Same per-process-state
  family as the bug above; pin the env var for any multi-worker run.
* **`POST /api/llm/models` 500s** with
  `AttributeError: 'LLMService' object has no attribute 'create_model'`
  (`routes/llm.py`). Pre-existing; attach models via `POST /llm/providers`
  instead.

## Known limits after this change

A reloaded page still loses the card for a pending approval — the row now
exists to rehydrate it, but no endpoint exposes pending confirmations for a
report yet, so the run waits out its timeout. Expired/answered rows are never
pruned; they are small, but a retention sweep would be reasonable.

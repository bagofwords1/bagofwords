# Feedback Loop — saved queries keep running after an agent or connection is renamed

## Problem

Saved step and entity code reaches its source by name:
`ds_clients["<agent>:<connection>"]`. The key is built on every run from the
current names (`DataSourceService.construct_clients`), and a rename was a
plain field update. So renaming an agent (or a connection) stranded every
dashboard and saved query that spelled the old name: each run failed with
`KeyError: '<old agent>:<connection>'` until every query was edited by hand,
by its owner.

## Change

- **Storage.** `data_sources.previous_names` and `connections.previous_names`
  (JSON list, migration `formernames01`). A mapper hook records the old name
  on every ORM write of `name` (`app/models/former_names.py`); renaming back
  removes the name from the list. No backfill: it applies to all saved code,
  whenever it was written.
- **Run.** `construct_clients` leaves on each client the keys it had under a
  former name (`client_keys.former_client_keys`). The code executor hands the
  code a `ClientsDict` (`sandbox/namespace.py`, both executor modes): a
  former key answers a direct lookup (`[key]`, `.get(key)`, `key in`), while
  iteration, `len` and `.values()` see each client once. They are not added
  to the dict the prompt lists, so new code is written with current names.
- **Which old keys a source answers to.** Every combination of its own and
  its connections' former names (and the `::fast` sibling, and the bare
  `"<agent>"` alias of a single-connection agent). Never a key that is
  current, that two of its connections could claim, or that another live
  agent carries or used to carry: that code fails on a missing key rather
  than reach a different source. The bare `"<agent>"` key is bound to
  the connection that had it when the name was given up (stored with the
  former name): if that connection is later replaced or detached, the bare
  old key is not answered.
- **Viewer data policy.** `sources_for_client_keys` reads former names the
  same way (current name first, then former), so "which sources does this
  step use" follows the rename as the run does. Before, a key no source
  matched counted as every source in the organization.
- **Also follows the rename:** the fork rule that decides whether a failed
  hydration keeps its code (`fork_service`), and the agents a published
  dashboard's chat picks from its code (`dashboard_chat._release_sources`):
  a former name counts there only as a literal client key, never as a word
  that merely appears in the SQL.

## Verification

Sandbox: real backend + frontend (`tools/agent/boot_stack.sh --dev`), SQLite,
seeded with `seed_org.py --demo --sqlite-sources 1`. The LLM is the repo's
stub (`tools/agent/stub_llm.py`) with one change (`mkstub.py`): generated code
uses the `"<agent>:<connection>"` key from a file, as the real coder does. No
real LLM was available in this sandbox.

Scripts are in `docs/feedback-loops/2026-11-18-client-keys-survive-rename/`
(Playwright through the UI; `api.py` for setup and the published dashboard).

Setup, same for both runs: chat "Show orders by region" on agent `Sales DWH`
→ the saved step is
`ds_clients['Sales DWH:sqlite-2'].execute_query(...)` → Add to Dashboard →
Publish. Refresh works (1 of 1).

| Step (through the UI unless noted) | Before (main) | After |
|---|---|---|
| Rename agent `Sales DWH` → `Sales` in Agents → Settings | saved | saved; `previous_names = ["Sales DWH"]` |
| Dashboard → Refresh data | **"Dashboard refresh failed — 0 of 1 queries refreshed"** | "Dashboard refreshed — 1 of 1 queries refreshed" |
| Published dashboard, owner refresh (API) | `failed`, `KeyError: 'Sales DWH:sqlite-2'` | `success` |
| Rename the connection `sqlite-2` → `warehouse` (API), refresh both | — | 1 of 1; `success` |
| New chat on the renamed agent | — | new step uses `ds_clients['Sales:warehouse']` |
| Saved step code after all of it (DB) | — | unchanged, still `'Sales DWH:sqlite-2'` |
| What the prompt lists (`check_clients.py`) | — | `Sales`, `Sales:warehouse` only |
| Policy: sources the old-key code runs on (`check_clients.py`) | — | the renamed agent only |

A rename made **before** the upgrade has no history (that is the state a
deployment is in when it already broke): the refresh still fails after the
upgrade. Renaming back to the old name fixes it at once (it always did), and
renaming forward again is then recorded and keeps working. Verified in the
after run (fail → rename back: 1 of 1 → rename again: 1 of 1).

Screenshots (seeded data): `before-refresh-failed.png`, `after-refresh-ok.png`,
`rename-in-settings.png`.

### Second run, after review

The review fixes (old keys answer lookups only; the bare key stays its
connection's; chat roster by client key) were run through the same stack and
database: with the agent now `Sales`, the dashboard refresh is 1 of 1; a
third rename in Agents → Settings (`Sales` → `Revenue`) is recorded as
`{"name": "Sales", "connection_id": "<the connection>"}`, and the saved
`'Sales DWH:sqlite-2'` step still refreshes (1 of 1, published dashboard
`success`). Iterating the dict handed to code yields `Revenue` and
`Revenue:warehouse` only.

## Tests

- `tests/e2e/test_client_key_survives_rename.py` — through the API: agent
  rename (pair key and bare key), connection rename, both renamed more than
  once, rename back, a name never held still fails, an old name taken by
  another agent is not answered by the renamed one, code that walks every
  client reaches each once after a rename, and the bare old key is not
  answered by a replacement connection.
- `tests/e2e/dashboards/test_dashboard_chat.py` — a former name joins an
  Auto dashboard's chat roster only as a client key.
- `tests/unit/test_former_client_keys.py` — the key rules, the policy
  mapping, and the run in both executor modes.

## Not verified

- PostgreSQL (no Docker in this sandbox); CI runs both.
- Snowflake itself. Listing a second database on a Snowflake connection also
  changes how unqualified `SCHEMA.TABLE` names resolve (the first database
  listed is the default); that is separate from the key and not changed here.

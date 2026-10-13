# Feedback Loop — search_agents regex matching freezes the web worker

A live stack trace showed the app's main thread stuck in
`search_agents.py → match_quality`, so the single web worker stopped answering
health checks and requests. This loop reproduces the freeze through the real
chat UI, then shows it fixed.

## Root cause (validated)

`compile_query_patterns` (`backend/app/ai/tools/implementations/search_agents.py`)
turned model-written query terms into stdlib `re` patterns that `match_quality`
ran **synchronously on the event loop** against every candidate agent:

1. **Globs** — `fnmatch.translate(f"*{s}*")` gives
   `(?s:(?>.*?sales)(?>.*?order).*)\Z`. A *failed* `search()` retries from every
   start position, so it is quadratic in the haystack length.
2. **Haystack** — the weak haystack was *all of an agent's active table names
   joined into one line*. 3,000 tables ≈ 70 KB.
3. **Raw regex** — any term containing a metacharacter was compiled verbatim.
   A backtracking term (`(\w+_?)+#`) takes **7 s against three table names**.
4. The tool's declared `timeout_seconds=25` never fires: the matching is
   synchronous CPU work, so nothing else on the loop runs, `/health` included.

Side bug found on the way: because names were space-joined, a glob could match
*across* two tables (`*sales*order*` matched `sales_fact order_lines`).

## Setup

```bash
cd backend && uv sync --extra dev && mkdir -p db
export BOW_DATABASE_URL='sqlite:///db/app.db' BOW_ENCRYPTION_KEY=<fernet key>
uv run alembic upgrade head && uv run python main.py &        # single worker
cd ../frontend && yarn dev &
cd ../backend && uv run python ../tools/agent/seed_org.py --db-path db/app.db
```

- A SQLite agent with 3,000 tables
  (`create table t0000_sales_fact_region …`), added via `POST /api/data_sources`
  (`type: sqlite`). Its tables start **inactive**, and search_agents only reads
  active names. Activate them with
  `POST /api/data_sources/{id}/bulk_update_tables {"action":"activate"}`.
  With inactive tables there is no freeze, which is a trap when reproducing.
- **LLM**: the sandbox Anthropic key had no credit, so the model is a scripted
  OpenAI-compatible stub (`scripts/search-agents-regex-hang/llm_stub.py`,
  registered as a `custom` provider at `http://127.0.0.1:8799/v1` and set as
  default). Its first planner turn calls `search_agents` with `$STUB_QUERY`,
  and later turns answer in text. Backend, tool, DB and UI are all real.
- Driver: `scripts/search-agents-regex-hang/chat_health_probe.js` sends the
  prompt through the chat UI (Playwright) and polls `/health` every 500 ms.

## Loop A — deterministic

```bash
cd backend && TESTING=true BOW_DATABASE_URL='sqlite:///db/unit.db' \
  uv run pytest -q tests/unit/test_search_agents_matching.py
```

With the old `search_agents.py` restored: `10 failed, 18 passed` in 140 s.
Every boundedness test hit the 20 s `pytest-timeout` guard, and the two
"glob must not span two table names" cases failed. After: `28 passed` in 0.9 s.

## Loop B — live UI, `/health` probed during the run

| Query (from the stub) | `search_agents` duration | Worst `/health` latency |
|---|---|---|
| `*sales*order*detail*`, **before** | **28,923 ms** (past its own 25 s timeout) | **28,022 ms** |
| `*sales*order*detail*`, after | 1,437 ms | 520 ms |
| `(\w+_?)+#` + `sales`, after | 877 ms, agent still found via `sales` | 912 ms |
| `*sales*region*` (should match), after | 807 ms, `Found 1 agent` | 734 ms |

In-process, matching all 3,000 names now takes about 2.6 ms per query (it was
about 29 s). The remaining ~1.2 s is the tool's `setup_ms`: loading and
rendering the agent's schema, which predates this bug.

## The fix

- **Globs** use `_GlobPattern`: it splits on `*` and finds the literal pieces in
  order with one forward scan per line, so cost is linear in the haystack. `?`
  still matches one character.
- **Raw regex terms** go through the `regex` package with a 50 ms per-search
  `timeout`. A term that times out is dropped for the rest of that search, so
  the timeouts can't add up across agents. The other terms keep matching.
  `regex` is now a direct dependency (it was already locked through tiktoken).
- **Weak haystack** is newline-joined, so each table name is matched on its own.

## Same bug in describe_tables / create_widget (fixed)

`describe_tables` (and `create_widget`'s `tables_by_source`) pass the model's
regex-looking terms verbatim as `name_patterns` to
`SchemaContextBuilder.build`. That builds the schema context and matched each
pattern against every table name with stdlib `re.search`. It had three match
sites: agent tables, the training-mode BOW catalog, and agent Lists.

Live run with the stub calling `describe_tables` with `["(\\w+_?)+#"]`:

| | Tool | Worst `/health` |
|---|---|---|
| before | never finished; the worker had to be killed | **no response within 120 s** (client timeout) |
| after | 1,384 ms, `Described 0 tables` | 689 ms |
| `["t000[0-4]_.*"]`, after | 706 ms, `Described 5 tables` | 637 ms |

Fix: the bounded matcher moved to `app/utils/safe_regex.py`
(`compile_bounded` / `compile_bounded_all`) and is used by `search_agents` and
by all three builder match sites. `create_data` builds its patterns from
`re.escape`d names only, so it is unaffected.

Loop A: `tests/e2e/test_schema_name_pattern_bounds.py`. With the old builder,
all 3 runaway cases hit the 60 s guard; after, `5 passed` in 14 s. Existing
builder suites (multi-connection, column visibility, Kerberos overlay, agent
lists, MCP placement, prompt rendering): `153 passed`.

## Not addressed

- `connection_catalog_common.compile_patterns` uses `fnmatch.translate`,
  anchored and per name, so the quadratic case doesn't apply.

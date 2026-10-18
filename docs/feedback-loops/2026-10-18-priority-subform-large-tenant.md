# Feedback Loop — Priority: "show 5 order lines" hangs on a real-size company

Asking for a few rows of a Priority subform (purchase order lines,
`PORDERITEMS`) never returned on a production-size tenant: the step sat on
"Running" until the 5-minute tool limit, then failed with
`Read timed out. (read timeout=120)`. The same questions worked against the
4-order mock, which is why nothing caught it.

## Root cause (validated)

A subform has no endpoint of its own, so the connector reads it through its
parent (`PORDERS?$expand=PORDERITEMS_SUBFORM(...)`). `build_subform_request`
deliberately kept `$top`/`$skip`/`$orderby` back (inside `$expand` they would
act per order) and applied them after flattening — so
`PORDERITEMS?$top=5` was sent as `PORDERS?$expand=PORDERITEMS_SUBFORM` with
**no limit at all**: every purchase order of the company, with all its lines,
in one response. On a real tenant Priority does not finish that within the
120 s HTTP timeout; each code retry sent the same request again.

Two more things made it worse, both seen in a real trace:

- The executor's query timeout is the org setting (default **180 s**), so the
  5-minute tool limit allowed barely one attempt.
- After a connector error, the code retry sometimes came back as an
  explanation followed by the code ("The previous attempt failed because …
  \n\ndef generate_df…"). The coder only stripped a fence at the very start,
  so the prose ran as Python: `SyntaxError … (<string>, line 1)`, one more
  wasted attempt. Captured with a temporary log line on the failing code.

## Loop A — deterministic reproduction (no LLM)

The mock gained a **large-tenant mode** (`tools/priority/mock_server.py
--orders N`): purchasing forms (SUPPLIERS, PORDERS, PORDERITEMS), N generated
sales and purchase orders (1–8 lines each, two years up to 2026-10-15, all
synthetic), and a response cost proportional to the parent rows it returns
(expanded rows cost more), the way a real tenant behaves. `--log` writes every
request it received as JSON lines.

```bash
cd backend
uv run python ../tools/priority/mock_server.py --orders 6000 --port 8901 --log /tmp/prio.jsonl &
# call PriorityErpClient.execute_query directly (get_schemas() first)
```

| Query | Before | After |
|---|---|---|
| `PORDERITEMS?$select=…&$top=5` | **120.1 s, ReadTimeout** | 0.5 s, 5 rows — sent `PORDERS?…&$orderby=ORDNAME&$top=5` |
| `PORDERITEMS?…&$orderby=CURDATE desc&$top=10` (parent column) | hang | 0.8 s — `$orderby=CURDATE desc,ORDNAME&$top=10` sent to PORDERS |
| `PORDERITEMS?…&$orderby=KLINE asc&$top=5` (line column, no filter) | hang | 3.0 s, refused: needs more than ~2,400 PORDERS rows at this server's pace; says which `$filter` to add |
| `PORDERITEMS?$filter=CURDATE ge 2026-07-01…&$orderby=QPRICE desc&$top=5` | hang | 47 s, 5 rows — 18 pages, 872 orders read |
| `PORDERS?$select=…&$expand=PORDERITEMS_SUBFORM(…)` (no `$top`/`$filter`) | hang | 0.3 s, refused: more than 500 rows match |
| same query, budget forced to 15 s | — | 12 s, "did not finish … narrow the $filter on PORDERS" |

## Loop B — real chat turns, Claude Haiku 5.5

`tools/agent/priority_large_tenant_e2e.py` creates the Priority connection,
switches its tables on, sends ordinary user questions (each in its own
report, none mentions how the connector works) and reads the outcome back
from the database. Backend booted per the sandbox skill; model set with
`HAIKU_MODEL_ID=claude-haiku-5-5 tools/agent/setup_haiku_llm.py`; key from an
env var only.

Before (old code, same mock, same model):

| Prompt | Result |
|---|---|
| show me 5 rows from purchase order items | **510.6 s, no data** — two attempts, each `Read timed out (read timeout=120)`; the mock logged each unbounded request running **290 s** |

After:

| Prompt | Time | What happened |
|---|---|---|
| show me 5 rows from purchase order items | 10.1 s | `PORDERITEMS?$top=5…`, query 0.55 s |
| PORDERSITEMS TOP 5 | 62.6 s | ranked by line total over the last 3 months (stated), query 47.6 s |
| what are the 10 most recent purchase order lines? | 42.3 s | `$orderby=CURDATE desc&$top=10`, query 0.8 s (this run predates the coder fix: 2 wasted retries) |
| Which 5 parts did we spend the most on … last 3 months? | 49.3 s | 3,221 lines in the window, aggregated in pandas |
| top 5 most expensive purchase order lines | 50.3 s | ranked the latest 2,000 lines and **said so**, offering to widen |
| how much did we spend per vendor this year? top 10 | 16.3 s | used PORDERS totals directly (5 s), no lines needed |
| list all purchase order lines for part P-0007 from last month | 22.2 s | parent date filter + part filter, 18 lines |
| תראה לי 5 שורות מפריטי הזמנות רכש | 9.8 s | 5 rows, answered in Hebrew |

## The fix

`backend/app/data_sources/clients/priority_erp_client.py`

- `_query_subform` — a request with a row limit and no sort on line columns
  (none, or parent columns only) reads PORDERS in pages with `$top`/`$skip`
  and the requested parent order (keys break ties), and stops once it has
  enough lines. Anything else reads every matching parent, page by page.
- `_check_full_read_fits` — for a full read, the first page's pace says how
  many parents fit in the remaining time; one request for a single key past
  that point (`parent_probe_path`) says whether more match. If so it stops
  right away with why (no `$top` / sorted on a line column / too many match
  the filter) and which filter to add — with an example date three months
  back from today, never a fixed date.
- `apply_query_timeout` — the client adopts the connection's query timeout
  (less a margin) as its budget; every request is bounded by what is left,
  and running out raises a message the agent can act on, not a raw timeout.
- `_check_expand_size` — an explicit `$expand` with no `$top` is refused when
  more than 500 rows match; a flattened result notes "already flattened, one
  row per line, no nested column" (models otherwise try to unnest it).
- System prompt: a **cost rule** for subforms (what makes a read fast or
  slow, parent-date sorting for "latest", use the parent form when no line
  detail is needed, change a refused query instead of re-running it).

`backend/app/data_sources/clients/priority_odata_query.py` — `PostOps` now
records whether the sort is on parent columns, whether the parent read is
filtered, and the parent filter itself; `with_parent_page` and
`parent_probe_path` build the paged and probing requests.

`backend/app/ai/agents/coder/coder.py` — `strip_code_response` takes the
fenced block wherever it is, or starts at the first `def`/import line when
prose precedes the code; used by all four code generators.

## What this proves / regression notes

- Existing unit tests (`test_priority_odata_query.py`,
  `test_priority_erp_client.py`, coder tests): 185 passed, unchanged.
- Not yet added: regression tests for the paging/probe/budget paths and the
  coder extraction (deliberately deferred; the cases above are the spec).
- Mock timings are a model, not a measurement of any one server; the probe
  adapts to the real server's pace.

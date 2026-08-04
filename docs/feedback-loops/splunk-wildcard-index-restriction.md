# Feedback Loop — Splunk wildcard-index restriction (bug reproduction)

Reproduces a customer-reported failure of the Splunk connector against a
hardened deployment where **wildcard index searches (`index=*`) are
administratively rejected** (common on Splunk Cloud / ES-hardened roles).
Customer symptoms: schema indexing "finds 4-6 objects", but every
`create_data` / `inspect_data` errors with *"you have to specify a specific
index, no global wildcards"*, and the user has no way to know the index —
"I'd expect it to learn it in the schema indexing".

No product code is changed by this loop — it is a **reproduction harness**
for `backend/app/data_sources/clients/splunk_client.py`, whose discovery and
prompts are built on `index=*` everywhere.

## Root cause chain (all in `splunk_client.py`)

1. The ONLY place real index names are learned is the wildcard catalog
   search `| tstats count where index=* by index, sourcetype`. When the
   deployment rejects wildcards, it fails.
2. `_catalog_pairs()` then falls back to `| metadata type=sourcetypes
   index=*` and **hardcodes `index: "*"`** for every sourcetype. The failure
   is only `print()`ed — discovery silently degrades. Tables come out named
   `*::<sourcetype>`.
3. `_sample_fields()` maps index `*` → `search index=* …` → also rejected →
   every table is thin (0 columns), and each thin table's description tells
   the agent to run `search index=* sourcetype=… | fieldsummary` itself.
4. The `system_prompt()` teaches `index=*` and uses it in examples, and
   there is no other way to learn index names (no `GET
   /services/data/indexes`, no config field to scope indexes) — so the agent
   either emits the forbidden wildcard (error) or guesses index names
   (empty results), and finally asks the user for the index.

## Reproduction environment

```bash
cd tools/splunk
docker compose up -d          # real Splunk 9.3 (if :8000 clashes with the
                              # backend, override ports to expose only 8088/8089)
python3 seed_splunk.py        # 13k events across 5 index::sourcetype pairs

# The "hardened customer deployment": a guard proxy on :8091 in front of the
# management port that rejects any search containing a wildcard index with a
# Splunk-style ERROR ("Search not executed: You must specify a specific
# index… Global wildcard index searches (e.g. index=*) are not permitted"),
# passes `| metadata` and index-scoped searches through.
python3 wildcard_guard_proxy.py
```

Then boot the app (enterprise license required — Splunk is an enterprise
data source), configure an Anthropic **Claude 4.5 Haiku** model, and connect
a `splunk` data source with `host=http://127.0.0.1:8091`.

## Observed (2026-08-04, Splunk 9.3.14, Claude 4.5 Haiku)

Schema indexing — "found objects, learned no index":

```
test_connection -> "Connected successfully. Found 5 tables."
discovered tables (5):        # metadata fallback: index collapsed to '*'
  *::access_combined  cols=0
  *::log4j            cols=0
  *::json_app         cols=0
  *::auth_audit       cols=0
  *::collectd         cols=0
backend log: "Splunk field sample failed for *::<st>: … You must specify a
specific index …" x5   (tstats catalog + all field sampling blocked)
```

Chat turn ("Show me a count of error events by host from the logs for the
last 24 hours"), from `tool_executions`:

| tool | outcome |
|---|---|
| `describe_tables` | success — 5 thin `*::` tables, descriptions say `search index=*` |
| `create_data` | error — index-less searches hit non-default indexes → empty DataFrame |
| `inspect_data` | error — **"Splunk search error: Search not executed: You must specify a specific index for this search. Global wildcard index searches (e.g. index=*) are not permitted…"** (verbatim customer error) |
| `create_data` (retry) | error — the model *guessed* `index=main` / `index=default` (data is in `app`) → empty |

Final agent message asks the user: *"Can you confirm which Splunk index
contains the log4j events?"* — the customer-reported dead end.

Guard-proxy log of the agent's actual SPL during the turn:

```
BLOCKED search index=* sourcetype="log4j" | head 1000 | fieldsummary   (x2)
allowed search index=main    sourcetype="log4j" … | stats count by host   -> empty
allowed search index=default sourcetype="log4j" … | stats count by host   -> empty
```

## Fix directions this repro validates

- Enumerate indexes via REST (`GET /services/data/indexes`) — works with no
  search at all — and run the tstats catalog per-index instead of `index=*`.
- Never store `*` as an index in the metadata fallback; surface catalog
  degradation as a visible warning instead of a `print`.
- Optional `indexes` config field on `SplunkConfig` to scope discovery.
- Drop/conditionalize the `index=*` guidance in `system_prompt()` and thin-
  table descriptions.

A fix is correct when, against the guard proxy, discovery yields
`<real_index>::<sourcetype>` tables with sampled fields and the same chat
prompt completes without any BLOCKED line in the guard log.

## Fix verified (2026-08-04, same environment)

The fix (`splunk_client.py`: REST index enumeration via `GET
/services/data/indexes`, explicit OR-list tstats catalog, no `index=*` in
field sampling or prompts, wildcard-error hint listing known indexes, plus an
optional `indexes` scope on `SplunkConfig`) was validated against the same
guard proxy and Claude 4.5 Haiku:

```
discovery:
  BLOCKED | tstats count where index=* ...            # fast-path attempt only
  allowed | tstats count where (index=app OR ... OR index=web) by index, sourcetype
  allowed search index=web sourcetype="access_combined" | head 500 | fieldsummary
  ... (per-index sampling for all 5 sourcetypes)
tables: web::access_combined (16 cols), app::log4j (14), app::json_app (15),
        security::auth_audit (14), metrics::collectd (12)   # real indexes, no '*::'

chat ("count of error events by host, last 24h"):
  allowed search index=app sourcetype="log4j" level=ERROR | stats count by host | sort - count
  tool_executions: create_data success (12 rows x 2 cols, bar_chart) — one shot,
  no inspect_data error, no clarifying question, zero blocked agent searches
unit tests: 20 passed (15 pre-existing + 5 restricted-deployment cases)
e2e: tests/e2e/test_data_source.py + test_connection.py — 11 passed
```

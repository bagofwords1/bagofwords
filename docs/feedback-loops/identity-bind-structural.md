# Feedback Loop — "identity-param filter check is a text heuristic; `WHERE 'a@x'='a@x'` passes"

Pentest finding 828508752 (high, WP3b). Claim under test: a saved query that
declares an identity parameter (`source: identity`, `viewer.email`) is only
checked for the viewer's value appearing *somewhere after* the first
WHERE/ON/HAVING. A tautology, an OR-widened filter or a negation passes that
check, so the query serves every owner's rows to every viewer.

## Root cause (validated)

- `backend/app/ai/code_execution/query_params.py` `_query_binds_value` (base
  ~L535): after stripping comments, `any(n in tail for n in needles)` where
  `tail` starts at the first predicate keyword. No grammar: `'a@x' = 'a@x'`,
  `region='EU' OR owner='a@x'`, `note <> 'a@x'` all contain the needle.
- Callers: `services/query_service.py` (builder run ~L653, viewer run ~L897),
  `services/step_service.py` (~L400). All three raise `ParamError` on a non-None
  result, so the fix only had to change the decision.
- `verify_identity_binds_in_queries` also carried an unreachable older
  implementation after its final `return None` (removed).

## Fix

Structural decision in `backend/app/ai/code_execution/identity_bind.py`
(rule documented in the module docstring and in
`docs/design/custom-query-rls.md` → "Identity parameters"):

- Captures are tagged with the client class (`CapturedQuery`, a `str`
  subclass) in `QueryCapturingClientWrapper.execute_query`, so the check picks
  the grammar: the read-only guard's `CLIENT_DIALECTS` for SQL, generic sqlglot
  for SOQL / unknown SQL-looking text, a small tokenizer for Splunk SPL.
- Parse failure / unsupported shape → the legacy heuristic, unchanged, plus a
  structured `identity_bind_check_fallback` warning.

## Loop A — deterministic (pytest, no services)

`backend/tests/unit/test_identity_bind_structural.py` drives the public
`verify_identity_binds_in_queries`.

```bash
cd backend && ENVIRONMENT=production TESTING=true .venv/bin/python -m pytest -q --db=sqlite \
  --disable-warnings tests/unit/test_identity_bind_structural.py
```

| Code | Result |
|---|---|
| Base (`origin/ai/friendly-dijkstra-2e8qmj`, `CapturedQuery` shimmed as plain `str`) | **168 failed**, 169 passed. All 150 SQL bypass cases (15 shapes × 2 values × 5 clients), 3 SOQL and 5 SPL bypasses are accepted; the dead-code check fails. The passes are the legitimate shapes and the non-SQL containment cases, which must pass on both. |
| Fix | **337 passed** |

## Loop B — live backend (HTTP, real SQLite source)

Two users seeded through the real API (`tools/agent/seed_org.py --invite
bob@example.com`). The loop creates a SQLite source holding 6 orders for
`admin@`, `bob@` and `carol@`, and a report with one query whose param `email`
is `source: identity, identity_binding: viewer.email`. Then it calls
`POST /api/queries/{id}/run`: viewer mode as each user, and builder mode (as the
owner) with edited code. Sibling agents held :8000/:3000, so the backends ran
on :8031 (base) and :8032 (fix), each with its own DB. No LLM is involved
(builder runs execute the submitted code directly).

```bash
# from backend/ of the tree under test
../docs/feedback-loops/identity-bind-structural/launch.sh 8032 /path/after.db &
.venv/bin/python ../tools/agent/seed_org.py --base-url http://localhost:8032 \
    --invite bob@example.com --db-path /path/after.db > seed.json
TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:////path/after.db \
  .venv/bin/python ../docs/feedback-loops/identity-bind-structural/loop.py http://localhost:8032 seed.json
```

Before (`identity-bind-structural/before.txt`):

```
viewer admin@example.com      legit query           -> 2 rows, owners=['admin@example.com']
viewer bob@example.com        legit query           -> 2 rows, owners=['bob@example.com']
builder legit  owner = :email                  -> 2 rows, owners=['admin@example.com']
builder taut   :email = :email                 -> 6 rows, owners=['admin@example.com', 'bob@example.com', 'carol@example.com']
builder OR     region='EU' OR owner=:email     -> 4 rows, owners=['admin@example.com', 'bob@example.com', 'carol@example.com']
builder neg    owner <> :email                 -> 4 rows, owners=['bob@example.com', 'carol@example.com']
builder fallbk NOT INDEXED, owner = :email     -> 2 rows, owners=['admin@example.com']
builder fallbk NOT INDEXED, :email = :email    -> 6 rows, owners=['admin@example.com', 'bob@example.com', 'carol@example.com']
viewer bob@example.com        after OR edit         -> 4 rows, owners=['admin@example.com', 'bob@example.com', 'carol@example.com']
```

After (`identity-bind-structural/after.txt`):

```
viewer admin@example.com      legit query           -> 2 rows, owners=['admin@example.com']
viewer bob@example.com        legit query           -> 2 rows, owners=['bob@example.com']
builder legit  owner = :email                  -> 2 rows, owners=['admin@example.com']
builder taut   :email = :email                 -> REFUSED: identity parameter 'email' was not applied as a filter in every executed query; refusing to serve unscoped results
builder OR     region='EU' OR owner=:email     -> REFUSED: identity parameter 'email' was not applied as a filter in every executed query; refusing to serve unscoped results
builder neg    owner <> :email                 -> REFUSED: identity parameter 'email' was not applied as a filter in every executed query; refusing to serve unscoped results
builder fallbk NOT INDEXED, owner = :email     -> 2 rows, owners=['admin@example.com']
builder fallbk NOT INDEXED, :email = :email    -> 6 rows, owners=['admin@example.com', 'bob@example.com', 'carol@example.com']
viewer bob@example.com        after OR edit         -> 2 rows, owners=['bob@example.com']
```

The `NOT INDEXED` rows show the fallback path. sqlglot cannot parse SQLite's
`NOT INDEXED`, so the legacy heuristic decides, exactly as before. A scoped
query still runs. **A tautology hidden behind an unparseable clause still
passes**, which is the documented residual risk of the chosen fallback. The
backend log for those runs carries:

```
{"level": "WARNING", "logger": "app.ai.code_execution.identity_bind", "message": "identity_bind_check.fallback",
 "event": "identity_bind_check_fallback", "client_type": "SqliteClient", "param": "email", "query_preview": "SELECT owner, item FROM orders NOT INDEXED WHERE owner = 'admi..."}
```

Note: the last line of `before.txt` was captured before `loop.py` gained the
"reset to the scoped step" builder run that precedes the OR edit. On the base
the OR edit is accepted either way, so the OR step becomes the default step
and bob is served other owners' rows.

## Non-SQL paths

API connectors with no grammar (Mongo, Qlik keyword calls, MCP) keep plain
containment, with no warning, as before. Unit cases:
`test_non_sql_connectors_keep_containment_semantics`. Splunk dashboard and
saved-search envelopes, and SPL with subsearches, `append`, `join` or macros,
fall back with a warning (`test_unparseable_falls_back_to_legacy_and_warns`).
No Splunk or Salesforce instance was used live. Those grammars are covered by
unit tests only.

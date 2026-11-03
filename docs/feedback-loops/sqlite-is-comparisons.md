# Feedback Loop — valid SQLite comparisons stop demo validation

Demo creation succeeds, but subsequent validation queries stop with “Could not
verify which tables this query reads”. Reproduced on 0.0.607 with sqlglot
27.29.0. The source is SQLite and all referenced tables are enabled.

## Root cause

SQLite supports general expressions on either side of `IS` and `IS NOT`,
including columns and function calls. sqlglot 27's inherited `_parse_is`
accepts only a primary/null expression on the right, rejecting expressions
such as `a IS b` and `a IS NOT abs(b)`.

`backend/app/ai/code_execution/table_scope.py:168` parses the final SQL and
rejects parsing failures. The wrapper converts that rejection into a terminal
`TableAccessError` (`code_execution.py:312`), stopping validation. The policy
is appropriate for unverifiable SQL; the parser lacks valid SQLite syntax.

Reference: [SQLite expression operators](https://www.sqlite.org/lang_expr.html#operators_and_parse_affecting_attributes).

## Loop A — deterministic regression

In a fresh checkout:

```sh
cd backend
uv sync --frozen --extra dev
mkdir -p db
export BOW_DATABASE_URL=sqlite:///db/app.db
export TESTING=true
uv run pytest tests/unit/test_sqlite_guard_comparisons.py \
  tests/unit/test_table_scope_guard.py tests/unit/test_sql_read_only_guard.py \
  -q --tb=short --disable-warnings
```

The new tests seed an isolated SQLite data source; they need no application
database, LLM, or external credentials. Coverage includes columns, functions,
arithmetic, NULL/boolean literals, subqueries, CTEs, joins, CASE expressions,
comments and strings. The real client/executor path verifies the NULL truth
table and records the original query unchanged. Negative cases check nested
disabled-table reads, malformed SQL, writes, and multiple statements.

Before the fix, the initial 43 new cases produced **10 failed / 33 passed**.
Every failure was a valid SQLite query rejected by the table-scope parser.
After the fix and two additional executor cases, the full focused suite
produced **653 passed / 0 failed** (45 new cases, 608 existing guard cases).

## The fix

`backend/app/ai/code_execution/sql_parser.py` adds a guard-local SQLite dialect
extension that parses the right operand into the existing expression tree.
Both guards use `parse_guard_sql`. It preserves `IS [NOT] DISTINCT FROM`,
NULL/boolean predicates, and nested table/function nodes for normal inspection.
Other dialects continue through the existing sqlglot parser. The SQL sent to
the data source is not rewritten, and the global sqlglot SQLite dialect is
not modified.

This is a parser correction, not a permission exception. Disabled tables and
malformed queries remain refused. Retry limits and terminal-error policy are
unchanged.

## Loop B — original-query confirmation

During the investigation, read the encrypted persisted tool results through
the application's decryption helper in a read-only production transaction.
Keep credentials on the server and query contents out of logs and commits.
Feed each original `executed_queries[0]` to the patched `check_sql` and
`check_table_scope` locally with the original seven enabled tables.

Observed **3/3 original, unmodified captured queries passed both guards**:
48,371, 72,581, and 23,110 characters. Before the patch their parser failures
included column-to-column `IS`, date-function `IS NOT`, and numeric predicates.
The queries were checked in memory, not executed or saved locally. No
production code, settings, or data were changed.

## Limits / follow-ups

The full production datasets were not rerun, so this does not validate their
business consistency or every SQL expression's database result. The separate
UI issue that labels terminal failed code as an extra attempt remains outside
this backend parser change. No dependency upgrade, deployment, or UI change
is included.

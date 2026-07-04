# AGENTS Guidelines for backend tests

The recurring failure mode in this suite is **overfit tests**: tests that
encode one reproduction scenario or today's incidental output instead of the
behavior we actually promise. Overfit tests pass while the feature is broken
for every input except the one in the test, and they break on harmless
refactors — both destroy trust in the suite. The rules below exist to prevent
that.

## Layout & how to run

| Dir | What belongs there | Run |
|-----|--------------------|-----|
| `unit/` | Pure logic, single service/helper, no HTTP | `uv run pytest tests/unit` |
| `e2e/` | Full API flows through `test_client` (routes → services → DB) | `uv run pytest -m e2e --db=sqlite` |
| `ai/` | Agent/planner behavior (needs `OPENAI_API_KEY_TEST`) | `uv run pytest -m ai` |
| `evals/` | LLM quality evals | manual |
| `integrations/` | Real third-party credentials (`integrations.json`) | manual / CI-gated |
| `fixtures/` | Shared pytest fixtures — **use these, don't hand-roll seeding** | — |

- `--db=sqlite` (default, fast) / `--db=postgres` (testcontainers) /
  `--db=external` (pre-set `TEST_DATABASE_URL`, for sandboxes without Docker).
  CI runs **both** sqlite and postgres — your test must pass on both; never
  rely on sqlite quirks (case-insensitive LIKE, loose typing, insertion order).
- The autouse `run_migrations` fixture builds the schema per test; set
  `TESTING=true` when running anything by hand.

## Anti-overfit rules

1. **Test the contract, not the implementation.** Go through the public
   surface — HTTP routes via `test_client` (e2e) or a service's public methods
   (unit). Never assert on private helpers, internal call order, or
   `mock.assert_called_once_with(...)` chains for code we own. If renaming an
   internal function breaks your test while behavior is unchanged, the test is
   wrong.
2. **Assert invariants, not incidental values.** Assert the fields and
   properties the behavior promises (`status_code == 403`, `error_code ==
   "..."`, `len(rows) > 0`, `total == created_count`) — not exact
   human-readable message strings, not exact float formatting, not response
   ordering unless ordering IS the contract, not IDs beyond "present and
   well-formed".
3. **Generalize regression tests past the bug report.** A repro test that only
   encodes the reported scenario ("second admin sees 0 tables") should land as
   the general invariant ("any org admin with a valid token sees the canonical
   catalog"). Name the test after the behavior, not the incident. Vary the
   magic values from the report — if the test only passes with the reported
   values, you've tested the anecdote.
4. **Mock at boundaries only**: LLM providers, OAuth/identity providers,
   external data-source drivers (ODBC/HTTP), SMTP, clocks. Everything inside
   the app (services, models, DB) runs real — that's what e2e means here.
5. **Seed through fixtures/API, not raw SQL.** Use `tests/fixtures/*`
   (`create_user`, `login_user`, `create_organization`, …) which go through the
   real endpoints. Direct DB writes are a last resort for states the API can't
   produce, and need a comment saying why.
6. **Every test must be able to fail.** Before committing, break the code (or
   stash the fix) and watch the test fail for the right reason. A test born
   green against broken code is worthless. No `assert True`, no try/except
   swallowing the assertion, no asserting only that "no exception was raised".
7. **Both roles, not just admin.** For anything permission-adjacent, cover the
   non-admin path too — permission gaps are this codebase's recurring bug
   class (see `e2e/rbac/`).
8. **No time/order flakiness.** No `sleep`-based synchronization, no wall-clock
   assertions (use the clock utilities — see `unit/test_clock.py`), no
   dependence on test execution order; every test owns its data (unique names
   via `uuid`, as the fixtures already do).
9. **Snapshot/golden assertions need review.** Never paste actual output into
   the expected value without verifying by hand that the output is *correct*,
   not merely *current*.

## Placement guide

New endpoint or flow → `e2e/`. Pure logic extracted from a service → `unit/`.
Planner/tool behavior → `ai/`. Needs real third-party credentials →
`integrations/` (secrets via env/`integrations.json`, never committed).
Reproduction for a reported bug → start from the **sandbox-feedback-loop**
skill, land the generalized version per rule 3.

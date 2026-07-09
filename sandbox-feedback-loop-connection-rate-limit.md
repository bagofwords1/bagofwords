# Sandbox Feedback Loop — Per-Connection Request Rate Limit (EE)

Runnable feedback loop that confirms the enterprise `connection_rate_limit`
feature works end-to-end against a live server and in the real UI.

The feature: EE customers can cap how many requests a connection accepts per
minute / hour / day, set in the Connection detail modal. The budget is
connection-global (shared across all users) and enforced as a **hard block** on
agent data queries; each breach is written to the enterprise audit log once per
window.

---

## What was built

- `connection_rate_limit` enterprise feature flag (`app/ee/license.py`).
- `connections.rate_limit_enabled / rate_limit_per_minute|hour|day` columns
  (NULL/0 => "no limit"), + `connection_rate_limit_counters` table for
  fixed-window counting (no Redis in the stack → counters live in Postgres).
- `app/services/connection_rate_limit_service.py` — increment-then-check across
  the configured windows (narrowest first); hard-blocks via `RateLimitExceeded`,
  which propagates out of the query wrapper like any query error. Enforcement
  runs only when a usage context is present (the agent data-query path), so
  indexing / connection-test paths are exempt automatically.
- Breach → `connection.rate_limit_exceeded` audit entry, once per window
  (under→over transition), so a throttled connection can't flood the log.
- PUT `/connections/{id}` gates the fields behind the license and persists
  0/None as "unlimited"; the detail payload exposes them.
- Connection detail modal gains a licensed "Request rate limit" section
  (lock icon + upsell when unlicensed), mirroring the auto-reindex block.

---

## Environment setup (fresh sandbox)

App targets **Python 3.12** (backend) and **yarn** (frontend).

```bash
cd backend
uv sync --extra dev
export BOW_DATABASE_URL="sqlite:///db/app_sandbox.db"
mkdir -p db
uv run alembic upgrade head           # single head: ...-> connratelimit01
```

The feature is license-driven, so the sandbox needs an **active enterprise
license**. This loop used a real signed enterprise key (org "James",
`tier=enterprise`, `features=[]` → inherits all tier features incl.
`connection_rate_limit`) via `BOW_LICENSE_KEY`; the committed public key verifies
it. Community mode (no key) is used to capture the locked/upsell UI state.

```bash
BOW_LICENSE_KEY="bow_lic_…" BOW_DATABASE_URL="sqlite:///db/app_sandbox.db" \
  uv run python main.py --config <config-with-signups-enabled>.yaml
```

Server log confirms load: `Enterprise license active: James, tier: enterprise`.

---

## Backend end-to-end check (live HTTP)

`backend/scripts/verify_connection_rate_limit.py` drives the whole flow over
real HTTP against a running server:

```bash
uv run python scripts/verify_connection_rate_limit.py
```

Asserts (**12/12 passing**):

1. License active, enterprise tier (`GET /license`).
2. Connection created.
3–4. Detail exposes rate-limit fields, defaulted off / None.
5. `PUT /connections/{id}` rate-limit config → 200.
6–8. Persisted: `enabled` + `per_minute=3`, `per_hour=0` (= no limit), `per_day=5000`.
9. Negative cap rejected (400/422).
10. **4th request in the minute window is blocked** (`RateLimitExceeded`).
11. Blocked window is `minute`.
12. **Audit logged exactly once per window** (not once per blocked request).

Expected tail:

```
==== SUMMARY ====
12/12 passed
ALL PASSED
```

---

## UI evidence (live app, Playwright)

Real ConnectionDetailModal against the running app (backend :8000 + Nuxt :3000):

- **Licensed** — "Request rate limit" section enabled, showing the persisted
  caps fetched from the backend: Per minute `60 req/min`, Per hour
  `1000 req/hour`, Per day `5000 req/day`.
- **Unlicensed (community mode)** — the same row is locked: lock icon +
  "Per-connection rate limiting is an enterprise feature." and a
  non-interactive toggle. The per-window inputs are hidden.

---

## Automated tests

- `backend/tests/e2e/test_connection_rate_limit.py` — **3/3 passing**: EE gate
  (402 unlicensed) + persistence (0 => unlimited); enforcement + audit-once-per-
  window; disabled connection never throttles (and writes no counters).
- Regression-checked green: `test_connection_auto_reindex.py` (3, shared PUT
  path) and `test_usage_limits.py` query-path cases (6, shared `execute_query`
  wrapper — the added rate-limit check is a clean no-op when the feature is off).

---

## Status

- [x] Migration runs on fresh SQLite; folds the two open heads to a single head.
- [x] Enterprise license loads and unlocks the feature.
- [x] Live e2e over HTTP: **12/12 passing**.
- [x] UI captured licensed (populated) and unlicensed (locked/upsell).
- [x] pytest: 3 new + regression suites green.
- [x] No license material committed.

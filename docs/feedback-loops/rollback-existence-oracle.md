# Feedback Loop — rollback reveals whether a hidden build exists

A scoped instruction manager could distinguish an existing unauthorized build (403) from a nonexistent build (404). Aikido finding 828741817 reports the same existence oracle; the preceding hardening already removed agent names from denial messages.

## Root cause (validated)

`backend/app/routes/build.py:504` invokes build-level permission enforcement. Its 403 propagated to the caller, while missing/foreign builds returned 404 earlier in the route. This change normalizes only object-level authorization denial; the route-wide permission decorator still rejects callers without any instruction-management authority independently of the requested ID.

## Loop A — deterministic reproduction

From `backend`, with Python 3.12 and project dependencies installed:

```sh
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db PYTHONPATH=$PWD python -m pytest tests/e2e/rbac/test_rbac_builds.py -k rollback_denials --db=sqlite -q
```

Before the fix, the public API regression failed with `assert [403, 404] == [404, 404]`. All users, grants, agents, and instructions are seeded through existing fixtures and real APIs. No external credentials are needed.

## The fix

Catch only HTTP 403 from rollback object-level enforcement, log the reason internally, and return the same 404 payload as missing builds. Other errors propagate normally. No mutation runs after a denied check.

After the fix, the full build RBAC module passed on SQLite: **5 passed**. The regression checks identical JSON responses for denied/missing IDs, unchanged build contents after denial, and successful rollback by an authorized administrator.

## What this proves / regression notes

This closes the response-status/body oracle for scoped managers without weakening rollback permissions. It does not claim constant-time responses or change table listing, indexing, or build-history visibility. Live Aikido retesting requires deployment and remains a separate verification step.

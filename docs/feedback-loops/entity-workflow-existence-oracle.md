# Feedback Loop — entity workflow reveals hidden entity existence

Aikido finding 828562259 reports differing suggest/withdraw responses for hidden and missing entity IDs. The owner lookup already normalizes non-owned and missing entities, but source lineage was checked earlier in the permission decorator and emitted a typed 403.

## Root cause (validated)

`backend/app/core/permissions_decorator.py` calls `guard_route` before workflow ownership checks. `backend/app/services/bow_source_access.py` rejected unreadable entity lineage with 403, while `EntityService._get_owned_entity` returned 404 for missing IDs.

## Loop A — deterministic reproduction

From `backend`, using Python 3.12 and installed project dependencies:

```sh
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db PYTHONPATH=$PWD python -m pytest tests/e2e/rbac/test_edit_and_export_authority.py -k workflow_denials --db=sqlite -q
```

Before the fix: **2 failed**, one per action, with `assert 403 == 404`. Users and entities are API-seeded. The test sets server-generated monitoring lineage directly because authoring APIs intentionally cannot supply it.

## The fix

Opt the suggest/withdraw permission decorators into concealed entity source denials. Unreadable lineage returns the same 404 body as missing entities and logs the denial internally. Existing lineage enforcement and route-wide permission checks remain active; other endpoints retain their responses.

After the fix, the combined entity-authority and build-RBAC modules passed: **17 passed, 0 failed** on SQLite.

## What this proves / regression notes

The regression compares complete status/body responses, and verifies an authorized owner can both suggest and withdraw. It complements the rollback regression in the same PR. Timing uniformity and live pentest retesting are not claimed. PostgreSQL verification remains blocked because the local Docker storage is full.

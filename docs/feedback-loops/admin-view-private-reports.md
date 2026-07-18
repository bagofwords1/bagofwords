# Feedback Loop — "admin full access should be able to see all reports and artifacts"

An org admin (`full_admin_access`) opening another member's private report
(`visibility: none`) by URL gets 403 → the UI shows "404 Report not found".
Reproducing this also surfaced that artifact routes enforced **no object-level
check at all** — any org member (even a user from another org) could read and
mutate any artifact by ID. This loop validates both and proves the fix.

## Root cause (validated)

1. **Admin blocked from viewing**: the `owner_only` branch of
   `requires_permission` (`backend/app/core/permissions_decorator.py`) denies
   non-owners before RBAC resolution ever runs — `FULL_ADMIN` is never
   consulted, so `GET /reports/{id}` 403s for admins on private reports.
2. **Artifact routes checked nothing**: the decorator extracts `report_id`,
   `completion_id`, `data_source_id`, `widget_id`, `memory_id`,
   `instruction_id`, `query_id` — but **not `artifact_id`**. For every
   `/artifacts/{artifact_id}` route (`model=ArtifactModel, owner_only=True`)
   the object was never loaded: no org scoping, no ownership check. Verified
   live: a non-owner member's `GET`/`PATCH` on someone else's artifact
   returned 200, cross-org `GET` returned 200.
3. `GET /artifacts/report/{report_id}/latest` had no `model=` at all (no org
   scoping), and `GET /artifacts/report/{report_id}` was org-scoped but not
   visibility-scoped.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/rbac/test_admin_view_reports_artifacts.py -q
```

Observed on pre-fix code — all five tests FAIL:

```
FAILED ...::test_org_admin_can_view_any_private_report_and_artifact
        # admin GET /reports/{id} → 403 "Only the owner can perform this action"
FAILED ...::test_non_admin_member_still_denied_private_report_and_artifact
        # bystander GET/PATCH /artifacts/{id} → 200 (no check ran)
FAILED ...::test_shared_viewer_keeps_artifact_read_access
        # viewer PATCH /artifacts/{id} → 200 (mutation via visibility)
FAILED ...::test_admin_view_bypass_does_not_grant_mutation
        # admin PATCH /artifacts/{id} → 200
FAILED ...::test_admin_of_another_org_cannot_view
        # cross-org GET /artifacts/{id} → 200 (org filter never applied)
```

## Loop B — live UI confirmation

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py
# invite owner@example.com as a member (seed_org's invite payload predates the
# organization_id/role body — invite via POST /organizations/{org}/members and
# register with the invite_token from the memberships table), then as the
# member: POST /api/reports (private by default) + POST /api/artifacts.
# As admin@example.com, open http://localhost:3000/reports/<report_id>.
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

Before: the admin sees **"404 — Report not found"**
(`media/pr/admin-access-reports-artifacts/before-admin-report.png`).
After: the admin sees the report and its dashboard artifact, identical to the
owner's view (`after-admin-report.png`; owner reference:
`reference-owner-report.png`). Admin `PUT /reports/{id}` and
`PATCH /artifacts/{id}` still return 403 after the fix.

## The fix

`backend/app/core/permissions_decorator.py`:

- Extract `artifact_id` into the decorator's `object_id` chain, so artifact
  routes actually load + org-scope + ownership-check their object.
- In the `owner_only` branch:
  - **Admin view bypass**: when the caller is not the owner and every required
    permission is read-only (`view_*`), resolve RBAC (memoized per request)
    and skip the ownership gate for `full_admin_access` holders. Mutating
    permissions keep the owner-only gate, for admins too.
  - **Artifacts inherit report visibility**: objects with no
    `artifact_visibility` of their own but a `report_id` (Artifact) resolve
    visibility from their parent report (loaded org-scoped), and the report's
    owner counts as their owner — so shared/internal viewers keep read access
    through `allow_public` routes.

`backend/app/routes/artifact.py`: `GET /artifacts/report/{id}` and
`GET /artifacts/report/{id}/latest` now use
`model=Report, owner_only=True, allow_public=True`;
`POST /artifacts/report/{id}/add-visualization` is owner-only.

Re-run of Loop A after the fix:

```
5 passed
```

## What this proves / regression notes

- Org admins can open any report/artifact in their org by ID (view-only);
  non-admin members are still denied on private objects; shared/internal
  viewers keep read access; nobody but the owner can mutate; cross-org access
  404s. Report/artifact list views intentionally stay explicit-only —
  matching the data-source precedent (`get_member_data_source_ids`).
- Full re-runs, all green: `tests/e2e/rbac` + `test_rbac*.py` (181 passed,
  8 pre-existing skips), `test_report*.py`, `test_report_sharing.py`,
  `test_doc_artifacts.py`, `test_public_routes.py`, `test_artifact_*`,
  `test_report_rerun_artifact.py` (73 passed).
- Known follow-up (not fixed here): `POST /api/artifacts` takes `report_id`
  in the body, which the decorator cannot see — any member with
  `update_reports` can still create an artifact on someone else's report.
  Needs a service-level ownership check.

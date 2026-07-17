# Feedback Loop — shared artifacts always show the creator's data; viewers cannot run steps as themselves

Reported behavior: "when creating an artifact, it uses the steps data in the
backend of the user that created the artifact. If the artifact is shared, I
want an option that the steps were run from the other user identity."
This loop validates the feature that fixes it: a shared-dashboard viewer can
click **Run** to re-execute the artifact's queries into their own per-user
results (`step_user_results`), with a share-dialog toggle ("Run on my
behalf") controlling whose data-source credentials execute.

## Behavior claims (validated)

- Viewing serves snapshots, never live queries: artifacts render each query's
  default step's stored `Step.data` (`app/services/report_service.py`
  `get_public_step`, `app/services/query_service.py`
  `get_default_step_for_query`). Before this feature every viewer saw the
  snapshot materialized under the creator's credentials, with no way to run
  as themselves.
- The fix: `POST /r/{report_id}/run` (`app/routes/report.py`) →
  `ReportService.viewer_rerun_report_steps` → `StepService.
  run_step_to_user_result`, writing to `step_user_results` keyed by
  `(step_id, user_id)`. The shared `Step.data` snapshot is never mutated;
  owner reruns (`StepService.rerun_step`) hard-delete the cached viewer rows.
  `reports.shared_run_identity` ('viewer' | 'creator', set via
  `PUT /reports/{id}/visibility/artifact`) picks the credential identity.
- Snapshot withholding (`app/services/viewer_data_policy.py`): in
  viewer-identity mode on user-scoped connections (`auth_policy !=
  'system_only'`) the creator snapshot is credential-differentiated data, so
  non-owner readers (including anonymous) get `snapshot_withheld=true` with
  empty data until they run as themselves, and the share/scheduled emails
  skip the snapshot-rendered PDF attachment (link-only). System-only
  sources and creator mode keep the previous behavior.

## Loop A — deterministic reproduction (pytest, no external services)

```bash
cd backend
TESTING=true ENVIRONMENT=production uv run pytest \
  tests/e2e/rbac/test_viewer_run_shared_artifacts.py -q --db=sqlite
```

Observed on current code: `8 passed`. The suite asserts the general
invariants (per-viewer isolation, share-visibility gating incl. 401/403/404,
identity persistence + `executed_as` stamping, cross-org refusal of
creator-credential runs on public links, invalidation on owner rerun,
snapshot withholding in viewer-identity mode — public, in-app and anonymous
reads plus the email-PDF policy gate — and the system-only exemption).
Before the feature commit these routes/tables do not exist (404s / missing
column), so the suite fails wholesale — the reported behavior.

## Loop B — live UI confirmation (full stack + Playwright)

```bash
tools/agent/boot_stack.sh
cd backend && TESTING=true ENVIRONMENT=production \
  TEST_DATABASE_URL=sqlite:///db/agent.db \
  uv run python ../tools/agent/seed_org.py            # admin@example.com

# invite+register viewer@example.com (member) — the seed_org --invite flag
# currently sends an invite body without organization_id and 422s; invite via
# POST /api/organizations/{org}/members + /api/auth/register with the
# invite_token read from db/agent.db (see seed_org.pending_invite_token).

cd backend && TESTING=true ENVIRONMENT=production \
  TEST_DATABASE_URL=sqlite:///db/agent.db \
  uv run python ../tools/agent/seed_shared_report.py   # prints {report_id,...}
# Seeds a report owned by admin with artifact_visibility='internal', one
# query whose DEFAULT STEP stores an obviously stale snapshot
# ("2023-10 (creator snapshot)" rows) while its saved code produces fresh
# 2024 rows when re-executed, and a page artifact rendering viz[0] as a
# table (same graph shape as tests/e2e/test_report_rerun_artifact.py).

PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
  node tools/agent/verify_viewer_run.mjs <report_id> <out_dir>
# driver: logs in owner → opens /reports/{id} → Share Dashboard
# modal (captures toggle off/on, asserting the PUT persists via the toast),
# logs in viewer → /r/{id} (waits for the stale rows, captures, clicks Run,
# waits for the 2024 rows, captures), re-opens /r/{id} as owner (waits for
# the stale rows again → isolation proof).
```

Observed output — every wait doubles as an assertion (a wrong state times
out): viewer saw `2023-10 (creator snapshot)` before Run and `2024-01/02/03`
after; the owner's `/r` view still showed the stale snapshot afterwards.

Evidence (committed under `media/pr/claude-artifact-steps-user-identity-hxe3tx/`):

| # | Screenshot | Shows |
|---|------------|-------|
| 1 | `1-share-modal-toggle-off.png` | Share Dashboard modal with the new "Run on my behalf" toggle (off = viewer credentials) |
| 2 | `2-share-modal-toggle-on.png` | Toggle on → `shared_run_identity='creator'`, "Sharing updated" toast |
| 3 | `3-viewer-before-run.png` | Viewer on `/r/{id}`: stale creator snapshot + **Run** button in the top bar |
| 4 | `4-viewer-after-run.png` | After clicking Run: fresh 2024 rows, "Refreshed just now" |
| 5 | `5-owner-after-viewer-run.png` | Owner on `/r/{id}` after the viewer's run: still the untouched creator snapshot (and no Run button — owners refresh via /rerun) |

## Loop C — per-user credentials against real Postgres RLS

Loop B proves storage isolation but both identities execute the same
pandas code — it cannot show *credential-differentiated data*. Loop C does,
using a real `user_required` Postgres source where row-level security
returns different rows per database login.

```bash
# 1. Postgres (no Docker in cloud sandboxes, so run the local PG 16 binary
#    as the postgres system user; a testcontainer works identically):
su postgres -c '/usr/lib/postgresql/16/bin/initdb -D /tmp/bow-pg/data -U postgres --auth=trust'
su postgres -c '/usr/lib/postgresql/16/bin/pg_ctl -D /tmp/bow-pg/data -l /tmp/bow-pg/pg.log \
  -o "-p 55432 -c listen_addresses=localhost -k /tmp/bow-pg" start'
psql -h localhost -p 55432 -U postgres <<'SQL'
CREATE DATABASE salesdb;
\c salesdb
CREATE USER alice PASSWORD 'alice-pass-1';
CREATE USER bob   PASSWORD 'bob-pass-1';
CREATE TABLE monthly_revenue (month text NOT NULL, revenue int NOT NULL, sales_rep text NOT NULL);
INSERT INTO monthly_revenue VALUES
  ('2024-01',1250,'alice'),('2024-02',1810,'alice'),('2024-03',2140,'alice'),
  ('2024-01', 310,'bob'),  ('2024-02', 420,'bob'),  ('2024-03', 375,'bob');
ALTER TABLE monthly_revenue ENABLE ROW LEVEL SECURITY;
CREATE POLICY per_rep ON monthly_revenue FOR SELECT USING (sales_rep = current_user);
GRANT CONNECT ON DATABASE salesdb TO alice, bob;
GRANT USAGE ON SCHEMA public TO alice, bob;
GRANT SELECT ON monthly_revenue TO alice, bob;
SQL

# 2. user_required auth is enterprise-licensed: generate a throwaway RSA
#    keypair, overwrite backend/app/ee/license_public_key.pem with the public
#    half (LOCAL ONLY — restore with git checkout afterwards), start the
#    backend with BOW_LICENSE_KEY=bow_lic_<RS256 JWT tier=enterprise> (same
#    shape as tests/e2e/test_license.py::_create_test_license).

# 3. Seed the data source + report + per-user credentials, materialize the
#    owner snapshot (runs as alice via the owner's stored my-credentials):
cd backend && TESTING=true ENVIRONMENT=production \
  TEST_DATABASE_URL=sqlite:///db/agent.db \
  uv run python ../tools/agent/seed_rls_report.py   # prints {report_id,...}

# 4. Drive the UI:
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
  node tools/agent/verify_rls_run.mjs <report_id> <out_dir>
```

Observed: the owner's snapshot holds alice's rows (1250/1810/2140,
`sales_rep=alice`); the viewer gets NO snapshot — the withholding banner
("This dashboard runs with your credentials") replaces the data, and the
driver asserts none of alice's numbers are present; the viewer's **Run**
under `run_identity='viewer'` returns **bob's rows** (310/420/375,
`sales_rep=bob`) — same SQL, different database login, RLS decides; after
the owner flips "Run on my behalf" (`run_identity='creator'`), the same
viewer's Run returns **alice's rows** and the viewer's result row is
stamped `executed_as='creator'`. The owner's shared snapshot is unchanged
throughout (verified via API).

| # | Screenshot | Shows |
|---|------------|-------|
| C1 | `rls-1-owner-alice-rows.png` | Owner: shared snapshot, alice's rows |
| C2 | `rls-2-viewer-snapshot-withheld.png` | Viewer before Run: snapshot withheld, run-with-your-credentials banner |
| C3 | `rls-3-viewer-own-credentials-bob-rows.png` | Viewer after Run (viewer identity): bob's rows via RLS |
| C4 | `rls-4-viewer-run-on-behalf-alice-rows.png` | Viewer after Run with "Run on my behalf" on: alice's rows, `executed_as='creator'` |

## What this proves / regression notes

- The two UI surfaces work end-to-end against the real stack: the share-dialog
  toggle persists, and a viewer's Run re-executes the saved step code and
  swaps only *their* view of the data.
- Isolation invariant holds in the UI, not just the API: the owner's view is
  unchanged after a viewer run.
- Environment notes for reruns: Playwright in cloud sandboxes needs
  `executablePath: '/opt/pw-browsers/chromium'` when the pinned
  `@playwright/test` revision differs from the pre-provisioned browsers;
  fresh orgs land on `/onboarding` (skip it before navigating); and
  `tools/agent/seed_org.py --invite` has a pre-existing 422 (missing
  `organization_id` in the invite body) unrelated to this change.

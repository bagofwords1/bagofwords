# Feedback Loop — "org setting to enable/disable public sharing of dashboards outside the org"

Request: add an organization setting that turns off "anyone with the link"
sharing. When off, the share dialog must stop offering it, the backend must
refuse it, and dashboards that are already shared publicly must stop being
reachable from outside the organization.

## Root cause (validated)

Nothing let an admin limit sharing to their own org. A report owner could set
`artifact_visibility` / `conversation_visibility` to `'public'`, and the
shared-viewer check returned early for it:

- `backend/app/services/report_service.py` `_check_visibility` —
  `if visibility == 'public': return  # Anyone can view`. Every shared-viewer
  path calls this check (`/r/{id}` and its artifacts/queries/steps/exports,
  `/c/{token}`, artifact chat, artifact resources, file access, MCP).
- Public shares can be created on four paths: `set_visibility`,
  `publish_report` (toggle), `update_report(status='published')` and
  `toggle_conversation_share`. All four write `'public'`.

## Design

- New setting `allow_public_sharing` (`FeatureConfig`, **on by default**) in
  `backend/app/schemas/organization_settings_schema.py`.
- `backend/app/services/public_sharing_policy.py` reads the setting
  (`public_sharing_allowed`) and enforces it (`assert_public_sharing_allowed`,
  which raises 403 `sharing.public_disabled`).
- **Existing links:** `_check_visibility` treats a stored `'public'` as
  `'internal'` while the setting is off. Anonymous visitors get 401 and other
  orgs get 403. Members of the report's org keep access. The stored value is
  left alone, so turning the setting back on restores the same links.
- **New links:** the four write paths above refuse a change *to* public.
  Re-sending an unchanged `'public'` is still allowed, because the share
  dialog's display toggles (data tab, chat, run identity) re-send the current
  visibility. Turning sharing off always works.
- `GET /reports/{id}` returns `public_sharing_allowed` for the dialogs.
- **UI:**
  - `ShareModal.vue`: "Anyone with link" is disabled ("Turned off by your
    organization's admin"). A dashboard whose link is suspended shows as
    "Organization", with an amber note.
  - `ShareConversationModal.vue`: the enable toggle is disabled, with a note.
  - The setting is listed under AI settings → Workspace.
  - Strings are in en/es/he.

## Loop A — deterministic reproduction (no external services)

`backend/tests/e2e/rbac/test_public_sharing_policy.py` covers four users: an
org admin, a member who owns the reports, a second member, and an admin of
another org.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/rbac/test_public_sharing_policy.py -m e2e --db=sqlite -q
```

On the base code: **8 failed, 2 passed**. The 2 that pass guard against
over-blocking and are expected to pass both before and after. Failure reasons:

```
KeyError: 'allow_public_sharing'                                   # setting missing
AssertionError: ('/api/r/<id>', ...) assert 200 == 401             # anonymous still reaches the dashboard
AssertionError: assert 200 == 401  get('/api/c/<token>')           # ...and the conversation
AssertionError: {"share_type":"artifact","visibility":"public",...} assert 200 == 403   # new public share allowed
```

**After the fix:** 11 passed, on SQLite and Postgres
(`--db=postgres` with `TEST_DATABASE_URL` pointing at a local Postgres 16).

**Mutation checks** (each test can fail for the right reason):

| Mutation | Caught by |
|---|---|
| Block even an unchanged `'public'` re-send | `test_existing_public_share_can_still_be_edited_or_turned_off` |
| Suspended public → `'none'` (members lose access) | the 3 "cuts off" / "re-enabling" tests |
| Read the setting globally instead of per org | `test_policy_does_not_affect_other_org_or_non_public_shares` |
| Drop the raw-dict branch in the reader | `test_setting_stored_without_metadata_is_honored_and_repairable` (200 ≠ 401) |
| Drop `_repair_partial_feature` | same test (GET settings → 500 `ResponseValidationError`) |

## Loop B — live sandbox (real stack, real browser)

Steps:

1. `tools/agent/boot_stack.sh --dev`
2. `seed_org.py --invite member@example.com`
3. Seed three reports through the API:
   - a public dashboard,
   - a public conversation,
   - a dashboard that was never shared.
4. Drive Playwright (Chromium) as the admin, the member, and an anonymous
   visitor.

| Check | Base code (setting stored off) | New code, setting on | New code, setting off |
|---|---|---|---|
| Settings → Workspace shows "Public link sharing" | absent | toggle on | toggle off; the click saved `value:false` |
| Anonymous `GET /api/r/{id}` | **200** | 200 | **401**, page shows "Sign in required" |
| Anonymous `GET /api/c/{token}` | **200** | 200 | **401** |
| Org member opens `/r/{id}` | 200 | 200 | **200** |
| New `PUT visibility public` | **200** | 200 | 403 `sharing.public_disabled` |
| Share button on the public dashboard | "Anyone with link" | "Anyone with link" | "Organization" + amber note |
| "Anyone with link" option | enabled | enabled | `aria-disabled=true`; clicking sends no request |
| Data-tab toggle on the suspended dashboard | — | — | `PUT … 200`, "Sharing updated" |
| Sidebar "Share conversation" (not shared) | — | — | toggle disabled + note; clicking sends no request |
| Hebrew (RTL) dialog | — | — | renders right-to-left, note translated |
| Setting turned back on in the UI | — | — | anonymous `/r` and `/c` → 200 again |

Screenshots: `media/pr/public-sharing-setting/`.

### Found during Loop B

The "before" run stored `allow_public_sharing: {"value": false}` while the base
code was serving. The base code doesn't know the key, so it saved the bare dict
with no `name`/`description`. Back on the new code, that caused two failures:

1. The policy reader saw a dict, not a `FeatureConfig`, and treated it as
   truthy. The stored "off" was **not enforced**.
2. `GET /organization/settings` returned 500 (the response model needs
   `name`/`description`). `PUT` also returned 500, so an admin could not fix
   it from the UI.

The same thing happens to any org that wrote the key through the API on an
older build, or after a downgrade and upgrade. Fixes:

- The reader handles a raw dict (`value` defaults to on only when absent).
- `OrganizationSettingsService._repair_partial_feature`, part of the settings
  sync that runs on every read and before every write, rebuilds such an entry
  from the schema default and keeps the org's value and state. This applies
  to every top-level feature setting, not just this one.

The new regression test covers it. Live check: after restarting on the fix,
the org loaded its settings (value `false`), both links returned 401, and
flipping the toggle in the UI restored them.

## Review finding: file tokens minted before revocation

Reported in review (code tracing): `GET /files/{id}/embed?token=…` only
checked the token's signature, the file's org and the bound report's existence.
A token an anonymous viewer got from `/r/{id}/files/{file_id}/embed_token`
before the admin turned public links off kept serving the file for the rest
of its one-hour lifetime.

- **Reproduced:** two new tests in `test_public_sharing_policy.py` failed with
  `assert 200 == 403`. One turns the setting off, the other unshares the
  dashboard.
- **Fix:**
  - `/r/{id}/files/{file_id}/embed_token` now marks a token `pub` when the
    viewer isn't a member of the report's org (anonymous, or another org).
    For such a viewer, the public link was the only way in.
  - `/files/{id}/embed` re-runs the anonymous `_check_visibility` gate for
    `pub` tokens on every request, and returns 403 if it no longer passes.
  - Tokens minted by org members carry no mark and keep working, the same as
    their page access.
- **Mutation checks:** never setting the mark fails both tests; always setting
  it fails the member-token assertion.
- **Results:** 13/13 in this file. File-access, pentest-authz, upload-hardening
  and sharing suites pass on SQLite (281) and Postgres (84). The SQLite run
  also had 3 failures not caused by this fix:
  - 2 OAuth-overlay tests fail the same way without the fix (the sandbox lacks
    `libodbc`);
  - `test_admin_sees_every_file` failed once under `-n 4`; its whole module
    passed 3/3 times on its own with the fix.

## What this proves / regression notes

- **Regression runs:** 798 passed across every e2e module that touches
  visibility, publish, conversation share or org settings (SQLite), plus 196
  passed on both SQLite and Postgres after the settings-sync change.
- **Unrelated failures (pre-existing):**
  - `tests/e2e/test_report_activity.py::test_clarify_pause_reports_awaiting_user`
    failed once under `-n 4` and passed on 3 isolated reruns.
  - `tests/unit/test_sandbox_feedback_loop.py::TestUnsafePythonIsRetryable::test_violation_then_clean_code_succeeds`
    and `tests/unit/test_query_timeout.py::test_timeout_triggers_retry_and_recovers_on_smaller_query`
    fail identically on the base commit (checked in a separate worktree).
- **Out of scope, left as is:**
  - `/thumbnails/{filename}` is unauthenticated whatever the visibility
    (unguessable filenames). This change does not touch it.
  - `frontend/components/PublishModal.vue` is not mounted anywhere. Its
    endpoint is blocked server-side anyway.
- In the sandbox, the hand-written seed artifacts show "Loading…" in the
  viewer with the setting on and off alike. That's a renderer/seed quirk,
  not this change. The evidence is the page shell vs. the sign-in wall.

# Feedback Loop — per-user report endpoints: share recipients admitted, denials audited

PR #1350 opens the per-user report endpoints (`POST /reports/{id}/viewed`,
`POST|DELETE /reports/{id}/star`, `GET /reports/{id}/fork_status`) to anyone who
can see the report, including people it was shared with directly or through a
group. Review found two regressions in the new gate. A denial no longer wrote the
`access.denied` audit record that the old `owner_only` decorator path wrote.
The gate also turned every `AppError` into "not found". This loop shows the HTTP
contract and the audit trail before and after the review fixes.

## Root cause (validated)

- `backend/app/routes/report.py:47` `_require_report_viewer` replaced the
  decorator's `owner_only=True, allow_public=True` gate. The decorator calls
  `_audit_access_denied` (`backend/app/core/permissions_decorator.py:16`) before
  each 403. The new helper raised 404 without that call, so outsiders probing
  reports left no trace in the audit log.
- The same helper had `except AppError: allowed = False`. The only `AppError`
  `user_can_view_report` is expected to raise is the 403
  `AppError.forbidden(ErrorCode.ACCESS_DENIED)` from
  `bow_source_access.assert_read` (`backend/app/services/bow_source_access.py:38`).
  Every other typed failure was hidden behind a 404.

## Loop A — HTTP sandbox (no external services)

The backend runs alone on non-default ports, with a scratch SQLite database
and a fake in-process enterprise license (groups and the audit API are
enterprise-gated). This mirrors `backend/tests/e2e/conftest.py`. `launch.py`:

```python
import sys, uvicorn
from app.ee import license as ee_license
fake = ee_license.LicenseInfo(licensed=True, tier="enterprise", org_name="sandbox",
                              features=["audit_logs", "custom_roles"], license_id="sandbox-fake")
ee_license._cached_license = fake
ee_license._cache_initialized = True
ee_license.set_db_license_key = lambda key: None   # keep the fake across startup DB sync
uvicorn.run("main:app", host="127.0.0.1", port=int(sys.argv[1]), workers=1)
```

`drive.py` works only through the real API. It registers an owner, then invites
three members (`group_member`, `conv_recipient`, `outsider`) through
`POST /organizations/{org}/members`. Each invitee registers with the pending
invite token, which stands in for clicking the invite link. The script then
creates a group containing `group_member` and two reports. One report's
conversation is shared with the group and the other is shared directly with
`conv_recipient`. Next it calls the four per-user endpoints as each role. Last,
as the admin, it reads `GET /api/enterprise/audit?action=access.denied&user_id=<outsider>`.

```sh
S=<scratch dir>; cd backend
TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///$S/loop.db .venv/bin/python -m alembic upgrade head
# PYTHONPATH matters: a script outside backend/ otherwise imports `app` from
# whatever checkout the venv's .pth points at (another worktree, here).
PYTHONPATH=$PWD TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///$S/loop.db \
  .venv/bin/python $S/launch.py 8137 &
.venv/bin/python $S/drive.py http://127.0.0.1:8137 $S/loop.db
```

### Before: PR head `bc1340647`

```
owner      -> group report: viewed/star/unstar/fork_status = [200, 200, 200, 200]
group mbr  -> group report: viewed/star/unstar/fork_status = [200, 200, 200, 200]
outsider   -> group report: viewed/star/unstar/fork_status = [404, 404, 404, 404]
owner      -> conv report : viewed/star/unstar/fork_status = [200, 200, 200, 200]
recipient  -> conv report : viewed/star/unstar/fork_status = [200, 200, 200, 200]
outsider   -> conv report : viewed/star/unstar/fork_status = [404, 404, 404, 404]
audit API status: 200
access.denied audit rows for outsider: 0          <-- 8 denials, no audit trail
```

### After: review fixes

```
owner      -> group report: viewed/star/unstar/fork_status = [200, 200, 200, 200]
group mbr  -> group report: viewed/star/unstar/fork_status = [200, 200, 200, 200]
outsider   -> group report: viewed/star/unstar/fork_status = [404, 404, 404, 404]
owner      -> conv report : viewed/star/unstar/fork_status = [200, 200, 200, 200]
recipient  -> conv report : viewed/star/unstar/fork_status = [200, 200, 200, 200]
outsider   -> conv report : viewed/star/unstar/fork_status = [404, 404, 404, 404]
audit API status: 200
access.denied audit rows for outsider: 8
   access.denied {"permission": "view_reports", "endpoint": "get_fork_status"}
   access.denied {"permission": "view_reports", "endpoint": "unstar_report"}
   access.denied {"permission": "view_reports", "endpoint": "star_report"}
   access.denied {"permission": "view_reports", "endpoint": "mark_report_viewed"}
   ... (same four again for the second report)
```

The response contract is the same in both runs. Recipients get 200, outsiders
get 404 (the same as a missing id) and the owner is unchanged. Only the audit
trail differs.

## The fix

- `_require_report_viewer` (`backend/app/routes/report.py:47`) now takes the
  endpoint name. When it denies access to a report that exists, it calls
  `_audit_access_denied(db, user, org, "view_reports", endpoint)` before raising
  404. This matches what the decorator records.
- `except AppError as e:` treats only `status_code == 403` (forbidden / access
  denied) as "not allowed" and re-raises anything else
  (`backend/app/routes/report.py:71-75`).

## Regression tests (`backend/tests/e2e/test_report_activity.py`)

- `test_conversation_share_admits_per_user_endpoints` and
  `test_group_share_admits_per_user_endpoints` check that the recipient gets
  200, the outsider gets 404 and the owner gets 200. Through the audit API,
  they also check that the outsider produced exactly one `access.denied` row
  per endpoint and the recipient produced none. Members are seeded with the
  shared `invite_user_to_org` fixture, which asserts a 200 on invite. Groups
  are seeded with `create_group` and `add_user_to_group`, under the
  `enterprise_license` fixture. The group test no longer has a `pytest.skip`
  path.
- `test_per_user_endpoints_hide_only_access_denials` monkeypatches the
  visibility check at its module. A 403 `AppError` gives 404 on all four
  endpoints. An unrelated `AppError` (400, `test.visibility_broken`) comes back
  as itself.

With the route file reverted to PR head, the three tests fail for the expected
reasons: the audit lists are empty, and the response is `assert 404 == 400`.
With the fix in place, they pass.

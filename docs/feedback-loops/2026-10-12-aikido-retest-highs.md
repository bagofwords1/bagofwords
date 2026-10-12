# Feedback Loop — Aikido retest: invite-as-admin, framed artifact shell, build-diff, deferred wake-ups

Four retest findings against base `91c185d23` (branch `ai/friendly-dijkstra-2e8qmj`):

| Id | Claim | Verdict |
|----|-------|---------|
| 828552157 | A `manage_members` delegate can still mint an admin by **inviting** one (the #1328 guard only covers PUT) | **Real — fixed** |
| 828759199 | An attacker page can iframe `https://<app>/mcp-artifact-app.html`, become its parent and run artifact code in the app origin | **Real — fixed** |
| 829412122 | `GET /builds/{id}/diff/details` discloses builds the list hides | **List and reads differ on purpose; no hidden instruction is served — no code change, evidence test added** |
| 829403536 | Deferred jobs keep running as a deactivated user | **Waits / scheduled prompts / check-ins were fine; three other jobs were not — fixed** |

## Root cause (validated)

1. **Invite escalation.** `POST /organizations/{id}/members` (`backend/app/routes/organization.py:25`) is gated only by
   `manage_members`, and `OrganizationService.add_member` (`backend/app/services/organization_service.py:332`) took the
   caller's `role` verbatim. The role string becomes a RoleAssignment immediately for a registered user
   (`_assign_system_role`) and on acceptance for a pending invite (`core/auth.py` `on_after_register`), so `role=admin`
   is a `full_admin_access` grant. The route also passed `current_user.id` where the service expects the user object.
2. **Framed shell.** `mcp-artifact-app.html:114` trusts `event.source === window.parent`. That is right for MCP hosts,
   but the same file is served by URL from the app origin (`backend/app/core/spa.py` in production, Nuxt's `public/`
   in dev) with no `frame-ancestors` / `X-Frame-Options`, so any page that frames it *is* its parent.
   Legitimate embedders, from code: MCP hosts get the shells as `ui://` **resource text** (`routes/mcp.py`
   `resources/read` → `_load_html_bundle`) and render them in their own sandbox origin; the in-app artifact frame is a
   **srcdoc** built by `frontend/utils/artifactIframe.ts`; `artifact-sandbox.html` is only **read from disk** for
   headless previews (`create_artifact.py` `SANDBOX_HTML_PATH`). Nothing frames the URL copy.
3. **Build diff.** `GET /builds` drops any build containing an instruction on an agent the caller can't access
   (`build_service.list_builds`, `accessible_data_source_ids`), while `/builds/{id}`, `/contents`, `/diff`,
   `/diff/details` serve the build with rows narrowed by `_instruction_read_filter` (`routes/build.py:88`). Every build is
   a whole-org snapshot, so 404-ing list-hidden builds would also 404 `/builds/main` and every training build for
   per-agent managers (the reason recorded in `2026-10-11-pentest-build-wait-training-authz.md`). What matters is that
   no read of such a build carries the hidden instruction — verified below.
4. **Deferred jobs.** `principal_can_act_in_org` is checked at fire time by wait wake-ups (`wait_service.py:80`) and
   scheduled prompts (`scheduled_prompt_service.py:486`); check-ins use the equivalent `checkin_policy.check_access`
   (`is_active` + `principal_belongs_to_org`). Not checked: the scheduled report refresh
   (`report_service.py:3294`, runs queries as the scheduling user and mails subscribers), the eval
   wake-on-finish (`test_run_service.py:1183`, agent turn as the origin user) and the nightly user dream
   (`dreams/runtime.py:278` `_gate`, memory writes + planned check-ins). Connection-level refreshes
   (`custom_query_service.scheduled_refresh`) and system sweeps don't act as a user.

## Loop A — deterministic reproduction (pytest)

```bash
cd backend   # the shared venv's .pth points at another worktree; run from THIS backend/ so cwd wins:
python -c "import app;print(app.__file__)"   # must print <this worktree>/backend/app/__init__.py
ENVIRONMENT=production TESTING=true python -m pytest -q --db=sqlite --disable-warnings \
  tests/e2e/rbac/test_member_management_escalation.py tests/unit/test_artifact_shell_framing.py \
  tests/e2e/test_mcp_ui_resource_delivery.py tests/e2e/test_deferred_jobs_deprovisioned.py \
  tests/e2e/test_user_dream.py tests/e2e/rbac/test_build_route_authority.py
```

Against base (`git archive 91c185d23`, new test files copied in): **25 failed, 47 passed**, plus a collection error for
`test_mcp_ui_resource_delivery.py` (imports the new `MCP_HOST_DELIVERED_MARKER`). The failures are exactly the
negative cases — `test_delegate_cannot_invite_as_admin[pending_email|registered_user]`, 17 framing checks (every path
spelling incl. `/mcp-artifact-app.html/`, the Nuxt rules, the shell gate), the `deactivated`/`removed` cases of the
report-refresh and eval-wake tests, and `test_no_dream_for_a_user_who_can_no_longer_act[deactivated|removed]`. The
positive controls (admin invites admin, delegate invites member, CSV import, header scoping, active users still run,
build reads) pass on base too. With the fix: **all pass**.

`test_builds_hidden_from_list_serve_only_visible_rows` passes on base (no change for 829412122); it fails when
`_instruction_read_filter` is short-circuited to `return None` (`/contents leaks SECRET ds_b …`), so it can fail.

## Loop B — live stack (non-default ports, private scratch dir)

Two backends side by side, each `uvicorn main:app` with `SERVE_FRONTEND=1` (the production static path) and its own
sqlite DB: base on `:18432`, fix on `:18431`. An "attacker" origin on `:19431` (`python -m http.server` over
`harness/retest-highs/{attack,host}.html`), the fix's shell copy re-served with no headers on `:19432`, and the Nuxt
dev server on `:13431` (`BOW_API_TARGET=http://127.0.0.1:18431`).

### (a) invite as admin — `harness/retest-highs/invite_escalation.py <api> <db> <admin>`

```
=== BEFORE (base 91c185d23) ===
delegate perms: ['manage_members']
delegate POST /members role=admin -> 200
membership row for attacker: ('admin',)
attacker accepted invite -> full_admin_access=True
audit member.invite* rows: [('member.invited', '{"email": "attacker-…", "role": "admin"}')]
[control] delegate POST role=member -> 200
[control] admin POST role=admin -> 200
=== AFTER (fix) ===
delegate perms: ['manage_members']
delegate POST /members role=admin -> 403 {"detail":"You cannot grant permissions you do not hold"}
membership row for attacker: None
audit member.invite* rows: [('member.invite.denied', '{"missing_permissions": ["full_admin_access"]}')]
[control] delegate POST role=member -> 200
[control] admin POST role=admin -> 200
```

### (b) framing in Chromium — `harness/retest-highs/run_framing.mjs`

```
before            csp=null xfo=null  stolen="http://localhost:18432 cookie="  frame.origin=http://localhost:18432 pwned=http://localhost:18432
before:mcp-host   origin=null ok="legit artifact rendered via MCP host"
after             csp="frame-ancestors 'none'" xfo=DENY  stolen=null  frame.origin=null
                  console: Refused to frame 'http://localhost:18431/' because an ancestor violates … "frame-ancestors 'none'".
after:mcp-host    origin=null ok="legit artifact rendered via MCP host"
after-no-headers  stolen=null  frame text="This artifact viewer only runs inside an MCP host."
```

Before, the forged artifact runs in the app origin. After, Chromium refuses to render the frame; and the same shell
served **without** the header (defense in depth) refuses to start its runtime. The MCP-host path (resource text from
`POST /api/mcp resources/read` in a sandboxed srcdoc) renders on both.

Headers on every serving path (`curl -D -`):

| path | base `:18432` | fix `:18431` (spa.py) | fix via Nuxt dev `:13431` |
|---|---|---|---|
| `/mcp-artifact-app.html` | none | `frame-ancestors 'none'` + `DENY` | `frame-ancestors 'none'` + `DENY` |
| `/mcp-visualization-app.html` | none | same | same |
| `/artifact-sandbox.html` | none | same | same |
| `/mcp-artifact-app.html/` | none (still 200) | same | — |
| `/index.html`, `/favicon.ico` | none | none (scoped) | none |

In-app rendering (`harness/retest-highs/seed_artifact.py` + `run_in_app.mjs`, through Nuxt dev, logged in):
`{"inApp":{"text":"In-app artifact renders","frameUrl":"about:srcdoc"},"pageErrors":[]}`.

Screenshots: `media/pr/beautiful-wright-su9lb9-retest-highs/{attack-before,attack-after,attack-after-no-headers,mcp-host-after,in-app-artifact}.png`.

### (c) build list vs diff

`tests/e2e/rbac/test_build_route_authority.py::test_builds_hidden_from_list_serve_only_visible_rows[manager_a|member]`:
the builds that carry an instruction on an inaccessible agent are absent from `GET /builds?status=all`, and every
`/contents`, `/diff`, `/diff/details` read of them (both directions, against the previous main and each other) returns
200 without the instruction's text, edited text or id; `/diff/details` counts equal the rows shown.

## The fix

- `organization_service.add_member` runs `_assert_can_invite_with_role` → `rbac_service._assert_can_grant` with the
  system role's permissions (shared `_system_role_permissions` helper, also used by `_assert_can_set_member_role`).
  Audited as `member.invite.denied`. `create_organization` opts out (`enforce_role_guard=False`) because the creator
  has no membership to resolve yet. The route passes the user object. CSV import inherits the guard (fixed `member` role).
  Other membership creators were checked and need nothing: SCIM / LDAP / OIDC group sync / SAML / Entra JIT use
  `member` or a full-admin-only setting (`auto_invite_role`, `PUT /organization/signup-policy` is
  `full_admin_access`) or the stored invite role (now guarded at creation). Pending pre-assigned roles/groups go through
  the RBAC routes, already guarded.
- `spa.py` adds `Content-Security-Policy: frame-ancestors 'none'` + `X-Frame-Options: DENY` for the three shell files,
  matched on the resolved file name; `nuxt.config.ts` routeRules mirror it. `'none'` rather than `'self'` because the
  app never frames these URLs.
- `routes/mcp.py` stamps `<script>window.__BOW_MCP_HOST_DELIVERED__=true;</script>` as the first script of every
  bundle served through `resources/read`; `mcp-artifact-app.html` / `mcp-visualization-app.html` return before
  registering the message listener when it is missing. The URL copy can therefore never run host-fed code, even
  behind a proxy that strips headers.
- `report_service.scheduled_rerun_report_steps`, `TestRunService._fire_eval_wake`, `DreamRuntime._gate` check
  `principal_can_act_in_org` and skip. The report refresh keeps its schedule so reactivating the user resumes it.

## What this proves / regression notes

- Related suites with the fix (`tests/e2e/rbac`, membership, membership import, org settings, MCP incl. OAuth,
  artifacts, builds, git-sync builds, scheduled prompts/refresh, SCIM, waits, check-ins, dreams, plus the touched unit
  tests): **1115 passed, 0 failed** (`-n 4`).
- The 2026-10-11 iframe loop (`2026-10-11-upload-traversal-embed-xss-iframe-origin.md`) served the static shell to a
  "legit" harness host. That copy now shows "only runs inside an MCP host"; replay its legit case with the
  `resources/read` text as in `harness/retest-highs/host.html`.
- Not verified: a real third-party MCP host (Claude, Cursor, VS Code) — the host behaviour is simulated by
  `host.html`. Observed while doing so (pre-existing, not changed here): the shell loads
  `/libs/artifact-globals.js` with a dynamic `script.src`, which `_load_html_bundle` does not inline, so inside a real
  host that path resolves against the host's origin.

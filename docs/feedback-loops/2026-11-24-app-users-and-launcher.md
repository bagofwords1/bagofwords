# Feedback Loop — App users and the app launcher

Validates, end to end on a running stack, that someone who arrives through an
embedded artifact ("app") is an **app user**: they get the apps shared with
them and a launcher, not the BOW workspace; that an admin (or a directory
group) can give them the workspace later; and that nobody who already had the
workspace lost it. Design: `docs/design/embedded-artifacts.md` §12.

## What was added

- **Permission and role**: `access_workspace` (withholdable), the built-in
  `app_user` role, migration `appuser01` (backfill onto every existing role,
  seed the role, `application_embedding_config.new_user_role`,
  `memberships.created_via_host_application_id`). Both permission resolvers
  grant the baseline only alongside the permission.
- **The gate**: `backend/app/core/workspace_access.py`, called from
  `current_user` and `get_current_organization`; MCP and chat platforms too.
- **Where the role comes from**: `host_identity.apply_new_user_role` (JIT in
  assertion / IdP-token modes) and the SSO popup's sign-up intent
  (`POST /api/embed/{host}/sso/intent`, applied at `…/sso/code`).
- **Launcher**: `GET /api/apps`, `GET /api/apps/available`,
  `POST /api/apps/access-request`; `frontend/pages/apps/index.vue`; redirect in
  `middleware/permissions.global.ts`; back arrow on `/d/{ref}`.
- **Admin**: "New people join as" on a host application, "App user" and
  "Joined via …" in the members list, "Use the workspace" in the role editor.

## Loop A — deterministic (no services, no LLM)

```bash
cd backend
TESTING=true ENVIRONMENT=production uv run pytest \
  tests/unit/test_workspace_access.py \
  tests/e2e/rbac/test_app_user_session.py \
  tests/e2e/dashboards/test_app_users.py \
  tests/e2e/org_isolation/test_oidc_org_mapping.py \
  -n 4 -m "e2e or not e2e" --db=sqlite -q
```

What they pin down (see each file's docstring for the contract):

| Case | Test file |
|---|---|
| Everyone who had the workspace keeps it; built-in roles; role editor offers the permission | `test_app_users.py` |
| 12 workspace routes refuse an app user (typed 403) and are untouched for a member | `test_app_users.py` |
| An app user keeps their account, the launcher and the artifacts shared with them | `test_app_users.py` |
| Role change is effective on the next request; only a member manager can make it | `test_app_users.py` |
| Roles add up: direct role, group role, custom role with / without the permission | `test_app_users.py` |
| A host application gives the people it creates its role (default, `app_user`, `member`); an invitation keeps its role; an existing account keeps its role | `test_app_users.py` |
| SSO popup: account it creates → host's role; plain BOW sign-up → member; existing member / admin untouched; an intent for another application does not count | `test_app_users.py` |
| App 1 then app 2 in another host application: still an app user, both listed | `test_app_users.py` |
| Launcher lists exactly published ∩ readable ∩ enabled-host; thumbnail withheld where the snapshot is; access request reaches member managers once and closes on grant | `test_app_users.py` |
| whoami says `app_user` with no baseline; invite as app user; per-organization gate; MCP; API keys | `test_app_user_session.py` |
| An OIDC `member` mapping rule does not lift an app user; an `admin` rule does | `test_oidc_org_mapping.py` |
| The allowlist itself (20 open, 23 closed paths) | `tests/unit/test_workspace_access.py` |

Each new assertion was seen failing before the code it covers existed or with
it reverted (the gate, the baseline condition, the sign-up intent).

## Loop B — live stack, real browser, real LLM

```bash
tools/agent/boot_stack.sh --dev            # then stop its backend and start:
cd backend && TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/agent.db \
  uv run python ../tools/agent/app_users/backend_enterprise.py   # sandbox-only license fake (domain sign-up)
uv run python ../tools/agent/seed_org.py --demo --invite member@example.com
uv run python ../tools/agent/app_users/seed_apps.py > /tmp/apps.json
# PUT /api/organization/signup-policy {enabled, allowed_domains:["acme.example"]}
# stand-in host product on its own origin:
BOW_SSO_HOST_APP=<Acme Portal public id> BOW_ARTIFACT=<Sales Assistant id> \
  uv run python ../tools/agent/embed/erp_host.py --port 4173
# make member@example.com an app user (PUT …/members/{id} {"role":"app_user"}), then:
cd ../tools/agent/app_users
for s in 01_launcher 02_popup_signup 03_admin 04_promote 05_visuals 06_host_application 07_chat; do
  node $s.cjs /tmp/app-users/shots
done
```

`seed_apps.py` creates three host applications (SSO popup, host assertion, one
disabled) and five published artifacts, of which exactly two are apps
`member@example.com` can open. Results on this branch (48 checks, all pass):

| Script | What it proves | Checks |
|---|---|---|
| `01_launcher` | An app user lands on `/apps` after sign-in; sees exactly their two apps; `/`, `/reports`, `/dashboards`, `/agents`, `/settings/members`, `/onboarding` all send them back; the app's back arrow returns to the launcher; "Request full access" sends; **no** refused API call from the launcher or the app | 15 |
| `02_popup_signup` | A brand-new person signs up inside the host product's popup (sign-up keeps the way back), the app appears in the host product; the same browser then opens BOW itself and gets the launcher; whoami says `app_user` with no permissions; `/api/reports` answers 403 `workspace.access_required` | 10 |
| `03_admin` | Members list: "App user" and "Joined via Acme Portal"; sidebar "Apps"; a member's launcher has "Open workspace" and no access request | 5 |
| `04_promote` | Admin picks **Member** on the row (one assignment added, one removed); the person's existing session opens the workspace; picking **App user** sends their next navigation back to the launcher | 6 |
| `05_visuals` | Role editor offers "Use the workspace"; Hebrew launcher is RTL, translated, no i18n warnings, back arrow at the right edge; empty state; phone width has no horizontal scroll | 8 |
| `06_host_application` | The host application row says "new people join as App user"; its form offers the choice | 2 |
| `07_chat` | Real LLM (Claude Haiku 5.5): an app user asks the app's chat "How many customers…" and gets 59, from the data; nothing the chat needed was refused | 2 |

Lower layers checked alongside: the members API returns
`role: "app_user"`, `roles: ["app_user"]`,
`created_via_host_application: "Acme Portal"` for the person created in the
popup; the admin's inbox holds one `workspace_access_request`.

## What the loop caught

- `plugins/fetchPermissions.client.ts` fell back to the *member* baseline when
  whoami returned an empty permission list, so an app user's UI would have
  believed it had the workspace. The redirect now reads the session's role.
- The artifact page reads `GET /api/organization/settings` (feature switches);
  the first gate refused it. It is in the allowlist, read-only.
- Sign-up dropped `?redirect=`, so a person who registered inside the popup
  landed on BOW's home and the popup never completed. Sign-up now returns
  where sign-in would have.
- The dashboard test corpus replays schema-only migrations unstamped:
  `appuser01` had to be idempotent in both directions and listed in
  `tests/fixtures/dashboard_corpus/alembic_at.py`.

## Known gaps

- Not in the corpus list: `dailyquota01` (a pre-existing gap — `whoami` and
  the organizations list fail on a corpus database), which is why the whoami
  assertions live in `tests/e2e/rbac/` and not beside the rest.
- A pre-existing `[intlify] Not found parent scope` warning on `/d/{ref}`
  (unrelated to this change; the launcher itself is clean).
- Not built: launch URL back to the host product, a per-artifact "standalone"
  switch, and a credentials screen for app users on personal-credential data
  sources. See the design doc.

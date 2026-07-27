# Feedback Loop — "load job info from Entra ID and optionally put it in the user's context"

Sync a signed-in user's Microsoft Entra ID profile (job title, department,
company, …) into their per-org context on login, let an org admin choose which
attributes are included from the **Identity Providers** settings page (with live
sample values pulled from their own profile), and surface those attributes to
the agent inside the existing `<user_profile>` context block. This doc validates
the whole path against the **real** `bow14.onmicrosoft.com` tenant and a **real**
Anthropic model.

## What was built

- **Permissioning finding (validated):** the "job info" fields (`jobTitle`,
  `department`, `companyName`, `officeLocation`, and even the `employee*` fields)
  are all readable on the signed-in user's **own** `/me` with the default-granted
  delegated **`User.Read`** scope — no admin consent. The only Entra "employee"
  field that needs elevated access is `employeeLeaveDateTime`
  (`User-LifeCycleInfo.Read.All` + an admin role); it is deliberately excluded
  from the allowlist. See `app/schemas/organization_settings_schema.py`.
- **Per-org toggle** stored in `OrganizationSettings.config["entra_profile_sync"]`
  (no bow-config change), gated by `manage_identity_providers`
  (`app/routes/organization_settings.py`).
- **Graph fetch** `app/ee/oidc/graph_client.py::resolve_user_profile` +
  token helper/refresh `app/ee/oidc/profile_service.py`.
- **On-login sync** into `Membership.profile_attributes` (new JSON column,
  migration `entraprof01`) — `app/services/auth_providers.py`.
- **Context injection** into the existing `<user_profile>` block —
  `app/ai/agents/planner/prompt_builder_v3.py::_format_user_profile`.
- **Settings UI** (`frontend/pages/settings/identity-provider.vue`): SCIM + LDAP
  made collapsible (collapsed by default); new **Entra ID Profile Sync** section
  with an enable toggle, per-attribute checkboxes showing **live sample values**
  from the admin's own profile, and add/remove attribute controls.
- **UserProfileModal** shows the synced attributes read-only under "Directory
  profile".

## Loop A — deterministic reproduction (no external services)

Unit tests prove the `<user_profile>` rendering (the context contract) without
any network:

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_prompt_builder_v3_user_profile.py -q
# 13 passed — includes:
#   test_profile_attributes_injected_into_user_profile_block
#   test_profile_attributes_skip_empty_and_flatten_nested
#   test_user_profile_omitted_when_attrs_empty_dict
```

## Loop B — live confirmation (real Entra + real Anthropic)

Secrets via env only (`T`, `CID`, `SEC`, demo creds, `ANTHROPIC_KEY`,
`BOW_ENTRA_CLIENT_SECRET`). Never commit them.

1. **Graph `/me` returns job info with plain `User.Read`** (ROPC for `demo1`):

   ```
   scope: https://graph.microsoft.com/User.Read  → /me HTTP 200
     jobTitle: hello world
     companyName: company
     usageLocation: IL
   ```

2. **The real sync code path stores it** — `sync_profile_on_login(...)` against
   live Graph persisted to `Membership.profile_attributes`:

   ```
   {"jobTitle": "hello world", "companyName": "company", "usageLocation": "IL"}
   ```

3. **Endpoints** (`GET .../entra-profile-sync/preview` and
   `/users/me/instructions`) return the live values; the settings UI renders them
   as sample values next to each checkbox (see assets).

4. **Agent uses the context** — with a real Anthropic model, asking *"What do you
   know about me via my profile attributes?"* answered:

   > | Job Title | hello world | · | Company | company | · | Usage Location | IL |

   proving the synced attributes reached the planner's `<user_profile>` block.

### UI evidence

- `assets/entra-profile-sync/01-sections-collapsed.png` — SCIM + LDAP collapsed,
  Entra section collapsed.
- `assets/entra-profile-sync/02-expanded-live-samples.png` — toggle on,
  checkboxes with live values (job title "hello world", company "company",
  usage location "IL").
- `assets/entra-profile-sync/03-add-remove-check.png` — Company unchecked, City
  added via the add-attribute control.
- `assets/entra-profile-sync/04-userprofile-modal.png` — Directory profile in
  Account Settings.

## What this proves

The permission premise (default `User.Read`, no admin consent), the per-org
opt-in, the on-login fetch+store, and the `<user_profile>` context injection all
work against a real Entra tenant, and a real LLM consumes the result.

## Follow-up loop — customer-style api:// login scopes (OBO fallback)

The original Loop B validated Graph access with a token minted directly for
`User.Read` (ROPC). Real SSO deployments that use data-source OBO request
`api://<client-id>/access_as_user` at login, so the token BOW stores has the
app's own API as its audience — Graph `/me` rejects it with
`401 InvalidAuthenticationToken (Invalid audience)`. Symptom: the settings
preview shows "Couldn't load live values from Microsoft Graph", every
attribute renders "not set", and the on-login sync never stores anything.

Fix (`app/ee/oidc/profile_service.py`): on Graph 401, exchange the login token
for a Graph `User.Read` token via OBO (jwt-bearer grant, provider client
credentials, `offline_access` for a refresh token), persist it on the user's
OAuth account, retry once. `app/core/auth.py::oauth_callback` now also updates
the stored tokens on every login (previously only the first login ever wrote
them).

Validated live against `bow14.onmicrosoft.com` with the full sandbox
(backend + frontend + real browser SSO as `demo1`, login scopes including the
`api://` scope, no `offline_access`):

1. Settings → Identity Provider preview: backend log shows
   `GET /v1.0/me → 401` → OBO token `POST → 200` → `GET /v1.0/me → 200`; the
   UI renders live samples (`jobTitle: hello world`, `companyName: company`).
2. Second SSO login with sync enabled → `Membership.profile_attributes =
   {"jobTitle": "hello world", "companyName": "company"}`.
3. `oauth_accounts` now holds a Graph-audience token **with a refresh token**,
   so later previews survive login-token expiry.
4. `uv run pytest tests/unit/test_entra_profile_obo.py` — 6 tests cover the
   fallback, assertion choice, reauth error, non-401 passthrough, and the OBO
   request shape.

No tenant-side changes were needed: `User.Read` was already on the app
registration and consent already granted (required anyway for the data-source
OBO scopes).

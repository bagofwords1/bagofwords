# Feedback Loop — Google profile sync (the Google counterpart of Entra profile sync)

Sync a signed-in user's Google profile (name, and — for Workspace accounts —
directory job title, department, organization) into their per-org context on
login, let an org admin choose which attributes are included from the
**Identity Providers** settings page (with live sample values), and surface
those attributes to the agent inside the existing `<user_profile>` context
block. Mirrors `entra-profile-sync.md` with Google userinfo + the People API
in place of Microsoft Graph.

## What was built

- **Data sources:** two Google endpoints, both covered by the
  `openid profile email` scopes the Google login already requests:
  - OAuth2 **userinfo** — `displayName`, `locale`, `hostedDomain` (`hd`).
    Always available, no extra setup.
  - **People API** `people/me?personFields=names,organizations,locations,locales`
    — the Workspace admin-set directory profile (`jobTitle`, `department`,
    `organization`, `location`), preferring the `metadata.primary` entries.
    Requires the People API to be enabled on the OAuth client's GCP project;
    a 400/403/404 degrades to userinfo-only fields instead of failing
    (`app/ee/oidc/google_profile_service.py::_resolve_google_profile`).
- **Per-org toggle** stored in `OrganizationSettings.config["google_profile_sync"]`,
  gated by `manage_identity_providers`, with an allowlist
  (`GOOGLE_PROFILE_SYNC_ALLOWED_FIELDS`) and default subset in
  `app/schemas/organization_settings_schema.py`.
- **On-login sync** into the existing `Membership.profile_attributes` JSON
  column (no migration needed). Both the built-in `google` provider branch and
  generic OIDC providers whose issuer is `accounts.google.com`
  (`_is_google_provider`) run the sync — `app/services/auth_providers.py`.
  The Entra and Google on-login gates now share one provider-agnostic helper
  (`_sync_provider_profile_on_login`), and the Membership write is shared via
  `profile_service.store_profile_attributes`.
- **Token lifecycle:** no OBO analog needed (Google login tokens already work
  against userinfo/People). A 401 falls back to a refresh-token exchange and
  retries once; with no refresh path it raises `GoogleReauthRequired`, which
  the settings preview surfaces as a "sign in with Google again" hint. (The
  default Google login flow requests no offline access, so the preview
  between logins is expected to hit this after ~1h.)
- **Routes** `GET/PUT /organization/identity/google-profile-sync` (+ `/preview`)
  mirror the Entra trio (`app/routes/organization_settings.py`,
  `organization_settings_service.py`).
- **Settings UI:** the inline Entra section on
  `frontend/pages/settings/identity-provider.vue` was extracted into a reusable
  `components/ProfileSyncSection.vue` (+ shared `composables/useProfileSync.ts`)
  and instantiated twice — Entra and Google — with per-provider i18n keys,
  allowlists and endpoints. `UserProfileModal` and the planner's
  `<user_profile>` injection needed no changes (already provider-agnostic).

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_google_profile_sync.py \
             tests/unit/test_entra_profile_obo.py \
             tests/unit/test_prompt_builder_v3_user_profile.py -q
# 32 passed — Google: People-API flattening (primary org preference, location
# composition), userinfo/People merge, People-API-disabled 403 degrade,
# 401 → refresh retry, refresh-less 401 → GoogleReauthRequired, non-401
# passthrough, empty-value dropping, provider detection. Entra suites stay
# green after the shared-store refactor.
```

## Loop B — full-stack sandbox run (real app, real browser, real LLM)

Full sandbox: backend + frontend + Playwright browser + **real Anthropic
model** (Claude 4.5 Haiku), driven end-to-end through the UI.

**Egress caveat (why the issuer is mocked):** this validation ran in a
sandbox whose egress policy resets all *browser* TLS connections
(server-side httpx/curl egress works — verified: `curl accounts.google.com`
→ 302, Chromium → `ERR_CONNECTION_RESET` for every external host). Real
`accounts.google.com` login is therefore impossible from the sandbox browser.
Service-account impersonation of the test Workspace user was also probed and
is authorized only for the `https://mail.google.com/` scope (token endpoint:
`unauthorized_client` for profile scopes), and the web OAuth client can't use
the device flow. So the login screen and Google API endpoints were served by
a **local mock issuer** (`http://127.0.0.1:9099`, Google-shaped responses,
clearly labeled as a mock), wired in via a `google-mock` OIDC provider whose
issuer path contains `accounts.google.com` (exercising `_is_google_provider`)
and the `BOW_GOOGLE_USERINFO_URL` / `BOW_GOOGLE_PEOPLE_URL` env overrides.
Everything else — app, DB, sync code, UI, LLM — is real. The one premise NOT
live-validated against Google is the real People API response shape, which
the unit tests pin to the documented person schema.

Steps and evidence:

1. **Seed:** UI signup `bow@bagofwords.com` (auto-creates Main Org), enable
   **Google Profile Sync** on Settings → Identity Provider (defaults:
   displayName, jobTitle, department, organization). Saved config verified in
   sqlite: `organization_settings.config.google_profile_sync =
   {"enabled":true,"fields":[...]}`.
2. **Login:** fresh browser context → "Sign in with google-mock" → mock
   consent page → callback. fastapi-users linked the OAuth account to the
   existing user by email and the backend logged:

   ```
   Google profile sync: stored 4 attribute(s) for user 8cfeed34-… in org b077e4b4-…
   ```

   DB check: `memberships.profile_attributes = {"displayName": "Bow Demo",
   "jobTitle": "Head of Data", "department": "Analytics",
   "organization": "Bag of Words"}` — the People API `organizations[primary]`
   entry correctly flattened.
3. **Preview:** Settings → Identity Provider now renders **live sample
   values** next to each checkbox (from the stored token via
   `GET …/google-profile-sync/preview`).
4. **Agent uses the context:** with the real Anthropic model (backend log
   shows `POST https://api.anthropic.com/v1/messages 200`), asking *"What do
   you know about me from my directory profile attributes?"* answered with
   exactly the synced values — Display Name "Bow Demo", Job Title "Head of
   Data", Department "Analytics", Organization "Bag of Words" — proving the
   attributes reached the planner's `<user_profile>` block.

### UI evidence

- `assets/google-profile-sync/01-section-defaults.png` — Google section next
  to the Entra one, toggle on, default fields, "sign in with Google to
  preview" hint.
- `assets/google-profile-sync/02-mock-google-login.png` — the sandbox mock
  issuer's login page.
- `assets/google-profile-sync/03-preview-live-samples.png` — live sample
  values after the Google login.
- `assets/google-profile-sync/04-agent-uses-context.png` — the agent's answer
  built from the synced attributes.

## What this proves / what to check against real Google

The per-org opt-in, both login-branch hooks, fetch+flatten+store, preview,
the shared Entra/Google refactor, and the `<user_profile>` injection all work
end-to-end in the real app with a real LLM. Against a real Google tenant the
remaining checklist is: (1) enable the **People API** on the OAuth client's
GCP project (otherwise only userinfo fields sync — by design), (2) confirm
the Workspace directory profile is populated/visible, and (3) sign in with
Google twice — the second login runs the sync with the org toggle on.

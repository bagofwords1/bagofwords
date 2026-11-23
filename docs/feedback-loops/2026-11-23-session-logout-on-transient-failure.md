# Feedback Loop — "Everyone has to log in again every few minutes"

Reported by users on a Google SSO deployment: they are sent back to the sign-in
page every few minutes. Claim validated here: the frontend throws away a valid
7-day session whenever the session check (`GET /api/users/whoami`) fails twice
in a row for *any* reason (502/503, timeout, connection reset). A backend
restart, a deploy or a proxy blip is enough. Google SSO is not involved: the
loop reproduces with password sign-in.

## Root cause (validated)

- `@sidebase/nuxt-auth@0.9.3`, `dist/runtime/composables/local/useAuth.js:102`:
  `getSession()` catches every error and runs `data.value = null;
  rawToken.value = null`. A `watch` in `useAuthState.js:19` mirrors `rawToken`
  into the `auth.token` cookie, so the cookie is deleted.
- `getSession()` runs when the tab becomes visible again (the library's
  `DefaultRefreshHandler.visibilityHandler`, default `enableOnWindowFocus: true`)
  and on every full page load. In production the SPA is served by the backend
  process (`backend/app/core/spa.py`), so an already-open tab is exactly what
  survives a restart and then hits the failing check.
- `$fetch` (ofetch) retries a failed GET once, so a single failure is absorbed;
  two in a row sign the user out. Validated live, see Loop A.
- The JWT itself is fine: `backend/app/core/auth.py:1520` mints it for 7 days,
  and the Google callback (`backend/app/services/auth_providers.py:583`) uses the
  same strategy. Google's own access-token expiry (`OAuthAccount.expires_at`) is
  never checked when authenticating a request.
- Side finding: `frontend/nuxt.config.ts` configured `auth.session.enableRefreshOnWindowFocus`
  / `enableRefreshPeriodically`. nuxt-auth 0.9 reads `auth.sessionRefresh.*`, so
  those keys were ignored (the library defaults happened to match).

Not addressed here (separate decision): `RevocableAuthenticationBackend.logout`
(`backend/app/core/auth.py:1532`) bumps `session_epoch`, so "Sign out" on one
device signs the user out on all of them. Verified with two tokens: after
logging out with A, both A and B get 401.

## Loop A — live reproduction (stubbed failures, no external services)

```bash
tools/agent/boot_stack.sh --dev
(cd backend && uv run python ../tools/agent/seed_org.py)
BOW_EMAIL=admin@example.com BOW_PASSWORD='<seed password>' \
  node docs/feedback-loops/2026-11-23-session-logout-on-transient-failure.verify.mjs
```

DevBox: skip the first two commands and `source tools/agent/devbox_env.sh` instead.

The script ([`.verify.mjs`](2026-11-23-session-logout-on-transient-failure.verify.mjs))
signs in, makes `/api/users/whoami` fail N times through a Playwright route stub,
refocuses the tab (or reloads the page), lets the backend "recover", and checks
whether the user is still signed in.

Before the fix:

```
PASS  control: whoami healthy                whoami=[ok] tokenAfterFailure=true afterRecovery=/onboarding signedIn=true (expected true)
PASS  502 once (absorbed by ofetch retry)    whoami=[FAIL,ok] tokenAfterFailure=true afterRecovery=/onboarding signedIn=true (expected true)
FAIL  502 twice in a row                     whoami=[FAIL,FAIL] tokenAfterFailure=false afterRecovery=/users/sign-in signedIn=false (expected true)
FAIL  network error twice in a row           whoami=[FAIL,FAIL] tokenAfterFailure=false afterRecovery=/users/sign-in signedIn=false (expected true)
FAIL  backend down (deploy/restart)          whoami=[FAIL,FAIL] tokenAfterFailure=false afterRecovery=/users/sign-in signedIn=false (expected true)
FAIL  page reload while backend down         whoami=[FAIL,FAIL,FAIL,FAIL] tokenAfterFailure=false afterRecovery=/users/sign-in signedIn=false (expected true)
PASS  401 (session really revoked)           whoami=[FAIL] tokenAfterFailure=false afterRecovery=/users/sign-in signedIn=false (expected false)

SOME TRIALS FAILED
```

## The fix

1. `frontend/patches/@sidebase+nuxt-auth+0.9.3.patch` (applied by
   `patch-package` in `postinstall`): the `getSession()` catch clears the
   session and token only when the response is **401**. Any other failure keeps
   the token and the last session data, logs a warning, and the next check
   recovers. `whoami` answers a bad or revoked credential with 401
   (`backend/app/core/auth.py`, `current_user`), so real revocation, logout on
   another device and expiry still sign the user out.
2. `frontend/nuxt.config.ts`: `auth.session.enableRefresh*` →
   `auth.sessionRefresh.{enableOnWindowFocus,enablePeriodically}`, same values.
   `.nuxt/refreshHandler.ts` now shows
   `new DefaultRefreshHandler({"enablePeriodically":false,"enableOnWindowFocus":true})`.

`frontend/` is copied whole before `yarn install --frozen-lockfile` in the
`Dockerfile` and CI, so `patches/` is picked up with no build change. Verified
by deleting `node_modules/@sidebase/nuxt-auth` and reinstalling:
`Applying patches... @sidebase/nuxt-auth@0.9.3 ✔`.

After the fix (dev server restarted):

```
PASS  control: whoami healthy                whoami=[ok] tokenAfterFailure=true afterRecovery=/onboarding signedIn=true (expected true)
PASS  502 once (absorbed by ofetch retry)    whoami=[FAIL,ok] tokenAfterFailure=true afterRecovery=/onboarding signedIn=true (expected true)
PASS  502 twice in a row                     whoami=[FAIL,FAIL] tokenAfterFailure=true afterRecovery=/onboarding signedIn=true (expected true)
PASS  network error twice in a row           whoami=[FAIL,FAIL] tokenAfterFailure=true afterRecovery=/onboarding signedIn=true (expected true)
PASS  backend down (deploy/restart)          whoami=[FAIL,FAIL] tokenAfterFailure=true afterRecovery=/onboarding signedIn=true (expected true)
PASS  page reload while backend down         whoami=[FAIL x14] tokenAfterFailure=true afterRecovery=/onboarding signedIn=true (expected true)
PASS  401 (session really revoked)           whoami=[FAIL] tokenAfterFailure=false afterRecovery=/users/sign-in signedIn=false (expected false)

ALL PASS
```

## What this proves / regression notes

- Only a 401 from the session check signs a user out; outages of any length no
  longer do. A real 401 still does.
- Page load during an outage: nothing could load, so the user sees the sign-in
  page, but the token is kept. One reload after recovery goes straight back
  into the app (checked separately).
- That case makes 14 `whoami` calls in the first ~5 s and then stops (about 7
  callers on app load, each retried once). Before the fix it was 4, because
  the first failure wiped the token and later callers skipped the request.
  Measured over 15 s: a bounded burst, not a loop.
- `plugins/fetchPermissions.client.ts` re-resolves permissions when session
  data changes. A failed check now leaves session data untouched, so
  permissions stay as they were instead of dropping to `null`.
- The patch is pinned to 0.9.3. `patch-package` fails install loudly if the
  file no longer matches after an upgrade; re-run this loop then. Upstream
  nuxt-auth may need the same fix.
- Infra follow-up: a 401-only logout hides the symptom. Two failed `whoami`
  calls in a row still mean the backend was unreachable, so check readiness
  probes, graceful shutdown and restart frequency on affected deployments.

# Feedback Loop — "logout on an Entra sso_only instance signs the user straight back in"

On an `auth.mode: sso_only` instance with a single enabled OIDC provider
(Entra), signing out was pointless: the user landed back inside the app with a
fresh session and no prompt. Same for session expiry.

## Root cause (validated)

PR #1064 added a single-provider auto-start to `frontend/pages/users/sign-in.vue`:
when the mode is `sso_only` and exactly one provider is enabled, `onMounted`
presses the provider button for the user. It was built for the embedded flow
(an app sends the browser to `/authorize`, the auth middleware bounces it to
sign-in, and the user should get a zero-click SSO hop), but it fired on every
visit to the sign-in page.

Sign-out lands on `/`, which requires auth, so the middleware bounces to
`/users/sign-in?redirect=/`. The auto-start ran, BOW's authorize URL carries no
`prompt=` parameter, BOW's logout never touches the provider, and the user's
Microsoft session was still alive — so Entra returned a code silently and the
callback signed them in again.

The existing guards (`?error`, `?local=true`) only stop the loop on a *failed*
round trip. A successful one, which is what logout produces, had nothing.

## Fix

Scope the auto-start to the flow it was written for. `embeddedAuthorizeFlow`
is true only when the sign-in page's `redirect` target has the path
`/authorize` (the OAuth consent page) or a `login_hint` is present; the
auto-start now requires it. A plain visit, a post-logout landing, and an
expired-session bounce all render the provider button instead.

No config, no backend change. The embedded flow is byte-for-byte unchanged.

## Verification (sandbox)

Backend on `configs/bow-config.dev.entra.yaml` semantics (`sso_only`, Entra the
only enabled provider, real discovery document, dummy client secret); first
user seeded in hybrid mode, then the backend restarted in `sso_only`.
Playwright routes every `login.microsoftonline.com` request to `abort()` and
counts the attempts, so "left for the provider" is observed without a real
tenant round trip.

| Scenario | Before | After |
|---|---|---|
| Sign out (`POST /api/auth/jwt/logout` 204, land on `/`) → `/users/sign-in?redirect=/` | 1 navigation to Entra, page never rendered | 0 navigations, "Sign in with Microsoft" button rendered |
| Direct visit to `/users/sign-in` | (same as above) | 0 navigations, button rendered |
| Unauthenticated `/authorize?client_id=…&login_hint=someone@example.com` → bounced to sign-in with the consent URL as `redirect` | 1 navigation to Entra | 1 navigation to Entra, `login_hint=someone@example.com` on the authorize URL |

The "before" row was reproduced by running the same scripts against the
unpatched file (`git stash`), then re-running after `git stash pop`.

## Not covered

- The post-sign-in redirect stash (`bow:postSignInRedirect`) could not be read
  in the embedded scenario because the tab had already left for the aborted
  provider URL. That code path is untouched by this change.
- An embedding app that opens BOW's root URL directly, rather than through
  `/authorize`, now sees the button instead of a zero-click hop. #1064 was
  written for the `/authorize` path, so this is believed to be nobody's flow.

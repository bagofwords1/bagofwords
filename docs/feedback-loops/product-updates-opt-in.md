# Feedback Loop — "Send me product updates" opt-in for the instance admin

Self-hosted instances report anonymous usage telemetry, but until now there
was no way to reach the person running an install (release notes, security
updates). This change adds an explicit, unticked opt-in for the instance's
first admin and tags licensed-instance telemetry with the license id. The claim
validated here: **a user's email leaves the instance only when the admin ticked
the box, never when they declined, never for later users, and never when
telemetry is off.**

## Behaviour (validated)

| Surface | Who sees it | What is sent |
|---|---|---|
| Sign-up checkbox (`frontend/pages/users/sign-up.vue`) | Only while the instance is unclaimed (`/api/settings` → `product_updates_prompt`) | Ticked: `$set {email, product_updates: true}` + `product_updates_subscribed`. Unticked: nothing. |
| Onboarding welcome (`frontend/pages/onboarding/index.vue`) | First admin who signed up without seeing the checkbox (SSO), until answered | Same, `source: onboarding`. "Skip" sends nothing. |
| Settings → General → Product updates | Admins (`manage_settings`; instance superuser on multi-org) | Opt-in as above; opt-out sends `$set {product_updates: false}` — never the email. |
| Every telemetry event | — | `license_id` (license JWT `sub`) on licensed instances. Never `org_name`. |

Server-side guards (`backend/app/services/product_updates_service.py`):
`prompt_available()` (telemetry enabled and not the cloud deployment),
`record_signup_choice()` re-checks "exactly one user exists" so a later
sign-up cannot set the instance answer, and `record_decision()` sends only on
a real subscription change.

## Bug found by the live loop

`Telemetry.identify()` called `Posthog.identify()`, which posthog-python 6.x
(the pinned client, 6.9.3) no longer has — every call raised `AttributeError`,
logged and swallowed (`backend/app/core/telemetry.py:_do_identify`). The unit
fake had an `identify` method, so Loop A was green while the live stack sent no
email at all. Fixed by calling `Posthog.set()` (sends `$set`), and the fake
now mirrors the real client surface (no `identify`), so Loop A fails on the
old code.

## Loop A — deterministic (pytest, no external services)

PostHog is replaced by a recording fake at the client boundary and its thread
pool by an inline executor (`backend/tests/e2e/test_product_updates.py`).

```bash
cd backend
export TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db
uv run pytest tests/e2e/test_product_updates.py -m e2e --db=sqlite -q
```

Before (backend changes stashed): `10 failed`. After: `10 passed`.

Mutation checks (each re-run of the file, then reverted):

| Mutation | Caught by |
|---|---|
| drop the "exactly one user" check | `test_a_later_signup_cannot_set_the_instance_answer` |
| send email on opt-out / keep it stored | `..._email_sent_only_when_ticked[False]`, `test_opting_out_sends_a_flag_and_never_the_email` |
| send a flag when the admin declines | `..._email_sent_only_when_ticked[False]` |
| ignore telemetry-off / cloud | `test_unavailable_instances_never_prompt_or_send[*]` |
| no `license_id` in event properties | `test_licensed_instance_telemetry_carries_license_id_not_org_name` |
| old `Posthog.identify` call | `..._sent_only_when_ticked[True]`, `test_opting_out_...` |

Related suites, unchanged behaviour: telemetry model id, LDAP/SAML auth,
license key/limits, user onboarding, domains → `159 passed`.
i18n locale sweep (`playwright.i18n.config.ts`) → `30 passed`.

## Loop B — live stack, telemetry pointed at a local stub

No credentials. The real backend (production config, `TESTING` unset, fresh
SQLite DB) sends telemetry to a local HTTP stub instead of the real ingest host.

```bash
# 1. stub: logs every POST body (gunzipped) as one JSON line
python3 posthog_stub.py 8765 stub.log &
# 2. backend on a fresh DB, telemetry -> stub
cd backend
export ENVIRONMENT=production BOW_DATABASE_URL=sqlite:///db/sandbox_pu.db \
       BOW_POSTHOG_HOST=http://127.0.0.1:8765
unset TESTING
rm -f db/sandbox_pu.db*; uv run alembic upgrade head; uv run python main.py &
# 3. frontend
cd ../frontend && yarn dev &
# 4. drive the UI with Playwright (sign up, tick/untick, onboarding, settings)
```

`posthog_stub.py` is a 20-line `http.server` handler that appends
`{"path", "body"}` per request; reset the DB between scenarios.

Observed stub traffic (product-updates events only, plus the total):

| Scenario | Result |
|---|---|
| A. First user ticks the box at sign-up | `$set {"email": "dana.admin@example-corp.com", "product_updates": true}` + `product_updates_subscribed {source: signup, license_id: lic_…}`; onboarding does not ask again; Settings shows "Sent to dana.admin@…" |
| A2. Same admin turns it off | `$set {"product_updates": false}` + `product_updates_unsubscribed`; email occurrences in stub: **0** |
| B. First user leaves it unticked | 16 ordinary events, **0** email occurrences, **0** `product_updates` mentions |
| C. First user created without the checkbox (SSO stand-in), ticks it on onboarding → Next | `$set {email: sam.sso@…, product_updates: true}` + `product_updates_subscribed {source: onboarding}` |
| D. Telemetry disabled in bow-config | `/api/settings` → `product_updates_prompt: false`, no checkbox; registering with `product_updates: true` → stub log **0 bytes** |

Screenshots: `media/pr/product-updates-opt-in/` (sign-up before/after, Hebrew
RTL, ticked, onboarding after sign-up, onboarding fallback, settings toggle,
telemetry off).

## What this proves / regression notes

- The email is sent only on an explicit tick, by the first admin, with
  telemetry on; declining, later users, and telemetry-off send nothing.
- Licensed instances now carry `license_id` on every event (community: none).
- Not built on purpose: a license check-in that keeps reporting when telemetry
  is off — that needs to be in the license terms first.
- The sweep needs the pre-installed browser in this sandbox
  (`launchOptions.executablePath=/opt/pw-browsers/chromium`), because the
  project pins a different Playwright build.

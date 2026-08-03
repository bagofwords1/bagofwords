# Bag of Words UI surface

Route inventory, access gating, and the traps that make a naive sweep produce wrong findings.
Regenerate the route list with `find frontend/pages -type f -name '*.vue' | sort` — the file
tree is the router.

## Contents

- [Page groups](#page-groups) — pick one per audit session
- [Access gating](#access-gating) — what redirects where, and why a route may bounce
- [Traps](#traps) — app-specific behavior that fakes a bug or hides one
- [Existing coverage](#existing-coverage) — what the current suite already asserts

## Page groups

Audit one group per session. Groups are ordered roughly by user-facing value.

### Core app

| Route | Notes |
|---|---|
| `/` | Home. Requires `view_reports`. Report creation entry point. |
| `/reports`, `/reports/[id]` | The chat/report surface. Richest control set in the app; also the most stateful — expect to hand-drive rather than sweep. |
| `/queries`, `/queries/[id]` | Saved queries. |
| `/dashboards` | |
| `/projects`, `/projects/[id]` | |
| `/agents`, `/agents/new`, `/agents/new/[id]/{context,schema}` | Data source agents. Multi-step wizard — audit as a flow, not as independent pages. |
| `/files` | |
| `/automations`, `/scheduled-tasks` | |
| `/instructions` | |
| `/evals`, `/evals/runs/[id]` | |
| `/monitoring`, `/monitoring/cost`, `/monitoring/diagnosis` | The two subpages need `manage_settings`. |
| `/excel` | |

### Settings and admin

All under the `settings` layout. Permission from `definePageMeta`:

| Route | Required permission |
|---|---|
| `/settings/general`, `/settings/overview`, `/settings/pii`, `/settings/smtp`, `/settings/license`, `/settings/integrations` | `manage_settings` |
| `/settings/models` | `manage_llm` |
| `/settings/members` | `view_members` |
| `/settings/audit` | `view_audit_logs` |
| `/settings/instructions` | `manage_instructions` |
| `/settings/identity-provider` | `manage_identity_providers` |
| `/settings/integrations/verify/[verify_code]` | `create_reports`, and `default` layout — the odd one out |

Credential-gated: SMTP, identity-provider, license, and OAuth integrations cannot be
exercised end to end without real external credentials. Audit them up to the point where the
form would submit, and record that boundary rather than reporting a failure.

### Auth and onboarding

`/users/sign-up`, `/users/sign-in`, `/users/verify`, `/users/forgot-password`,
`/users/reset-password`, `/organizations/new`, `/onboarding`, `/onboarding/llm`,
`/onboarding/data`, `/onboarding/data/[ds_id]/{context,schema}`.

These need a **logged-out** sweep (`--storage none`) — with an admin session they redirect.
Highest blast radius in the app; also partly covered by the existing suite.

### Out of scope by default

- `/old_agents/*` (11 routes) — superseded, but still linked from `layouts/data.vue`, so
  reachable. Confirm with the user before spending time here.
- `/reports/[id]/legacy`, `/i18n-smoke` — legacy and test fixture respectively.
- `/r/[id]`, `/c/[token]` — public share links, need a shared resource to exist first.

## Access gating

Three global middlewares run on every navigation (`frontend/middleware/`). A route that
bounces is usually one of these, not a bug — check before writing it up:

**`auth.global.ts`** — an authenticated but unverified user is pinned to `/users/verify`.
If every route in your sweep redirects there, your seeded user was never verified; fix the
fixture rather than filing 30 findings.

**`onboarding.global.ts`** — admins with incomplete, undismissed onboarding are redirected to
`/onboarding` from everywhere except `/users/*`, `/organizations/new`, `/r/*`. Non-admins are
never nudged. This is the single most common cause of a bogus "page redirects to onboarding"
finding. `sweep.mjs --login` dismisses it and warns if the dismissal did not take; if you
build a session another way, click "Skip onboarding" and wait for the URL to change
(`frontend/tests/fixtures/auth.ts` and `tools/agent/login_and_capture.mjs` both show it).

**`permissions.global.ts`** — enforces `meta.permissions` and `meta.resourcePermissionAny`.
Note it **deliberately does not block when permissions have not loaded yet**. A page can
therefore render briefly and then redirect once permissions arrive. If a route's outcome
differs between runs, this race is the first thing to suspect — raise `--settle` and re-run
before calling it flaky.

## Traps

**`networkidle` never fires.** The app holds polling and websocket connections. Use
`waitUntil: 'load'` plus an explicit settle delay. The existing `global.setup.ts` comments on
this; `sweep.mjs` already does it.

**Pages spin until `/api/settings` resolves.** A form is not present at DOM-ready. Wait for a
specific element, not for the page.

**The message input is `[contenteditable="true"]`, not a textarea.** Fill it by typing, not
`fill()`.

**Send in the report view gates on multiple conditions** — non-empty text, a data source or
uploaded file, no upload in flight, a selected model. A disabled send button is usually
correct; check all four before reporting it. The draft does not survive a reload.

**Escape closes modals by eating the draft.** Click the page background instead
(`page.mouse.click(60, 60)`).

**Provider names must be unique including soft-deleted ones** — a 409 on a name you just
"deleted" is existing behavior, not a new bug (though whether the UI explains that is fair
game).

**Model checkboxes have no accessible label.** Resolve them by walking ancestors until the
text contains `Model ID:`.

**Empty states are undertested.** Most of this app's pages have only ever been exercised with
data present. Sweeping a fresh database is where the yield is highest — and it means the
order of your audit matters: sweep empty first, then seed, then sweep again.

## Existing coverage

`frontend/tests/` runs under `playwright.config.ts` as a dependency chain: `setup` (creates
admin, writes `tests/config/admin.json`) → `onboarding` → `members` and `features` in
parallel → `visibility`.

For an audit, prefer the agent stack (`boot_stack.sh` + `seed_org.py`) and `sweep.mjs
--login` — it is the same setup the `qa` and `ui-evidence` skills use, and `seed_org.py`
gives you a member account for permission checks that the Playwright `setup` project does
not. `tests/config/admin.json` works as a session source if that suite has already run.
Either way, do not write a third login flow.

Coverage is thin and partly hollow — e.g. `tests/home/home-menu.spec.ts` navigates and
asserts nothing. Treat the presence of a spec as weak evidence that an area works. Two
helpers in `tests/utils/helpers.ts` reference `/users/login`, which is not a route in
`pages/` (the page is `users/sign-in.vue`); anything calling `login()` from there is broken
or unused. Worth confirming during an auth-group audit.

Only 22 components carry `data-testid`, concentrated in custom queries, projects, triggers,
and webhooks. Everywhere else you are selecting by role and text, which is why `sweep.mjs`
records a selector per control up front — deriving them twice invites drift between the
expectation table and the click run.

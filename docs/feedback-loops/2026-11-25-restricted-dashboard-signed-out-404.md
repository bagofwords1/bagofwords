# Feedback Loop — "I shared the dashboard but they get 404"

An owner shares a published dashboard with a colleague as **Can edit**,
general access **Restricted**. The colleague clicks the link and lands on
`/not_found` ("Page not found … or you don't have access to it") with no way
forward. Claim validated: when the link is opened **signed out**, a restricted
dashboard is indistinguishable from a missing one, and the page sends the
visitor to `/not_found` instead of offering sign-in.

## Root cause (validated)

- `backend/app/services/dashboard_access.py:263-266` (`check_visibility`): for
  visibility `none` the API raises `artifact.not_found` *before* it checks for
  a signed-in user, so an anonymous caller gets 404, never 401. This is
  deliberate (a private dashboard is hidden from anyone it does not let in)
  and is kept.
- `frontend/components/dashboard/SharedDashboardView.vue` (`loadReport`): a 404
  only tries the request-access page for a *signed-in* member; everyone else
  falls through to `navigateTo('/not_found')`. A shared editor who simply was
  not signed in in that browser had no sign-in prompt.

Ruled out with API probes (seeded editor, restricted dashboard):

| Visitor | `GET /api/d/{id}` | What the page showed |
|---|---|---|
| Signed-in workspace member with Can edit, published | 200 | the dashboard |
| Same, unpublished | 404 `dashboard.not_published` → draft 200 | the draft |
| Signed-in app user (no `access_workspace`), published | 404; `access_request` 200 | request-access page |
| Signed-in app user, unpublished | 404; `access_request` 404 | `/not_found` (by design: an app user cannot edit) |
| **Signed out**, published | **404**; `access_request` 401 | **`/not_found`**: the bug |

## Loop A: deterministic reproduction (no external services)

```bash
tools/agent/boot_stack.sh --dev
cd backend
uv run python ../tools/agent/seed_org.py \
    --invite editor@example.com --invite outsider@example.com > /tmp/seed.json
# dismiss onboarding for the seeded org (PUT /api/organization/onboarding {"dismissed": true})
DASH=$(uv run python ../tools/agent/seed_restricted_dashboard.py --seed /tmp/seed.json)
cd ../frontend
EDITOR_EMAIL=editor@example.com EDITOR_PASSWORD=... \
OUTSIDER_EMAIL=outsider@example.com OUTSIDER_PASSWORD=... \
CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium \
  node ../tools/agent/restricted_dashboard_signin.mjs "$DASH" /tmp/loop
```

The seeder publishes a dashboard owned by the admin, grants the first invited
member **Can edit** and leaves general access **Restricted**. The script opens
the link signed out, signs in as the editor from the page, and opens it as a
member with no grant. `CHROMIUM_EXECUTABLE` is needed only where the
frontend's pinned Playwright has no browser build of its own (cloud sandboxes).
In a DevBox, skip `boot_stack.sh` and `seed_org.py`'s admin/org steps (see
root `AGENTS.md`, "DevBox").

Observed on the base commit (fix reverted):

```
FAIL anon: url=/not_found gate=0
FAIL signin: no sign-in gate to click
PASS outsider: url=/d/<id> request_page=true
exit=1
```

## The fix

`SharedDashboardView.vue` `loadReport`: a 404 on `/d` or `/r` while the auth
status is `unauthenticated` (and not inside a host application) shows the
existing sign-in gate (`accessError = 'login'`) instead of `/not_found`. A
missing dashboard gets the same gate, so a signed-out visitor still learns
nothing about which dashboards exist. After sign-in the page loads again as
that person and the usual outcomes apply. The API is unchanged.

Same loop, fix applied:

```
PASS anon: url=/d/<id> gate=1
PASS signin: url=/d/<id> dashboard_rendered=true
PASS outsider: url=/d/<id> request_page=true
exit=0
```

Also checked by hand: a signed-in admin opening a non-existent `/d/<uuid>`
still lands on `/not_found` (no gate).

## What this proves / regression notes

- A shared editor who opens the link signed out reaches the dashboard after
  signing in, with no extra step from the owner.
- The signed-in paths (dashboard, request-access page, real 404) are unchanged.
- Not changed: an **app user** given Can edit on an **unpublished** dashboard
  still gets `/not_found`. Editing is workspace-only (`dashboard_access.py`,
  `can_edit`), so that case needs the person's role, not this page.

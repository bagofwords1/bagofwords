# Feedback Loop — "embed BOW artifacts in our ERP, signed in as the employee, with past conversations"

The claim: an admin registers the company's back office as a **host
application**; an analyst builds a dashboard or a chat-assistant app in BOW
chat and allows it in that application from the artifact's **Embed** tab; an
employee opens the back office and sees the artifact as themselves, with no
BOW sign-in (or a one-time BOW sign-in in SSO mode), holding their own
conversations with the organization's agents. Design: `docs/design/embedded-artifacts.md` (§12 is the as-built record).

## Root causes found and fixed along the way (validated)

| What a person saw | Cause | Fix |
| --- | --- | --- |
| Admin: host-app creation 500 on the dashboard test corpus | the corpus applies post-data-migration revisions unstamped; `hostapp01` was not idempotent | `alembic/versions/hostapp01_host_applications.py` checks for its tables; listed in `tests/fixtures/dashboard_corpus/alembic_at.py` |
| Embed refusal returned 500 instead of a typed 401 | the refusal path read the host row after `db.rollback()` expired it (`MissingGreenlet`) | `routes/embed.py::create_embed_session` reads ids before signing in |
| Employee: the app's **Send** button did nothing | the floating "Made with BOW" badge covered it inside the host | `SharedDashboardView.vue`: no badge (and no bubble lift) inside a host application |
| Employee: a second chat bubble over a chat app | the dashboard bubble rendered on an artifact that is itself a chat | bubble hidden when the artifact uses `bow.conversations` |
| Employee: page went blank after an answer | generated app rendered `tools` objects as React children; the SDK reference didn't say their shape | `artifact_sdk_reference.py` spells out `tools:[{name,title,status}]`; BOW fixed the app when told "the page goes blank" |
| Employee: answers showed raw Markdown tables | the sandbox has no Markdown renderer | `bow.markdown(text)` in `artifact-sdk.js` (React elements only, no HTML); BOW adopted it unprompted |
| Owner: "You can't use that agent here" with chat bubble set to *Dashboard data only* | conversations reused the bubble's scope (empty agent list) and required *Allow chat* | `services/artifact_conversations.py`: the agents the artifact was built with, independent of the bubble; editors can try the draft |
| Hebrew: Copy button over the first line of code | `end-2` resolves to the left in RTL while code is LTR | `DashboardEmbedPanel.vue`: the code block is `dir=ltr`, button `right-2` |

## Loop A — deterministic (no external services)

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest --db=sqlite -n 6 \
  tests/e2e/dashboards/test_embed_host_applications.py \
  tests/e2e/dashboards/test_embed_identity_sources.py \
  tests/e2e/dashboards/test_artifact_conversations.py \
  tests/e2e/test_embed_ldap.py \
  tests/e2e/dashboards/test_artifact_activity.py
```

Observed: 39 + 6 + 7 + 5 passed. The identity provider (discovery + keys), the
directory (`identity_by_guid`) and the agent loop are the only substitutions.
Mutation checks: removing the per-viewer filter on conversations, the
host-allows check in `core/embed_auth.py`, or the email guard in
`services/host_identity.py::_admit_oidc` each turns the matching test red.

## Loop B — live (real Claude Haiku 5.5, stand-in back office on its own origin)

Stack: `tools/agent/boot_stack.sh --dev` (set a stable `BOW_ENCRYPTION_KEY`;
a restart otherwise invalidates tokens and stored credentials), seed with
`tools/agent/seed_org.py --demo`, add the Anthropic provider with the key
from an env var. The back office: `tools/agent/embed/erp_host.py` on :4173
(signs assertions server-side with the host application's private key).

1. **Authoring, unbiased prompts** through `/api/reports/{id}/completions`:
   *"Our account managers need a dashboard they can check during customer
   calls: monthly revenue …"* and *"Our account managers want a sales
   assistant inside our back office … previous conversations listed on the
   side"*. Haiku chose `bow.conversations` for the second on its own.
2. `node tools/agent/embed/admin_setup.mjs <report> <out>` — Settings →
   Integrations → add host application (key shown once, redacted in the
   capture) → dashboard Settings → Embed → allow → snippet.
3. `node tools/agent/embed/erp_flow.mjs <out>` — 7/7: Alice and Bob signed in
   silently, Hebrew RTL, Carol (unmapped) refused with a readable message and
   no retry button, frame policy names only the back-office origin.
4. `node tools/agent/embed/assistant_flow.mjs <out>` — 5/5: two real questions
   answered from the Music Store data in two conversations, the first
   reopens with its history, Bob sees none of Alice's. Re-run with the
   bubble off and *data only*: still 5/5.
5. `node tools/agent/embed/sso_flow.mjs <out>` — 4/4: SSO-mode host shows
   *Sign in*, BOW sign-in in a popup on the BOW origin, popup closes, the
   dashboard appears as the person.

6. Backend examples (Settings → Integrations → *Backend example*, the
   Embed tab's *Server* view, and right after creating a host): the Python
   (PyJWT) and Node (jsonwebtoken) code, rendered for the real host and run
   unchanged against `POST /api/embed/{host}/session` — both return 200 for
   Alice. Screenshots `60-host-apps-python-{en,he}.png`,
   `61-embed-tab-python-{en,he}.png`.

7. **App activity log** (editor side and BOW's preview):
   `node tools/agent/embed/editor_activity_flow.mjs <report> <out> "<question>"`
   (`EXPECT=ok` for a working app) shows the batches the editor's page sends.
   `backend/ … uv run python ../tools/agent/embed/preview_sdk_check.py <report> <version> <user> "<button>"`
   drives BOW's own preview with no model and prints `sdk_calls` / `errors`.
   Live: a broken app (`create({agents:['Music Store']})`) → the editor's
   failure reaches the next turn and BOW fixes it unprompted; in a fresh
   conversation BOW's preview finds `agent_forbidden` before anyone uses the app.
   Sandbox note: the backend's Python Playwright wants browser build 1223;
   point `PLAYWRIGHT_BROWSERS_PATH` at a folder that links it to the installed one.

Evidence: `media/pr/embedded-artifacts/`.

## Regressions

- 112 passed: artifact chat, artifact resources, fork resources, LDAP (e2e +
  unit), Entra token exchange, security headers, SPA caching.
- `tests/e2e/dashboards` (≈600): the only failures are the golden-snapshot
  suites (`test_visibility_baseline`, `test_migration_before_after`), which
  fail identically on the commit before this work in this sandbox (artifact
  body hashes differ). One real regression found and fixed: the release-copy
  audit refuses route files with `include_in_schema=False` (`routes/embed.py`).
- `yarn build` passes. With `SERVE_FRONTEND=1`, `/d/{ref}?host=` sends the
  host's frame policy, an unknown host gets `'self'`, a plain `/d/` page keeps
  the site default.

## What this proves / notes

- The employee never sees a BOW login in assertion and IdP-token modes; the
  credential stays in memory; embed sessions open only the artifacts that
  allow their host and only as far as the person's own sharing allows.
- Not covered yet: SAML first-sign-in e2e, MCP tool approvals in
  conversations, re-attaching to a running answer.
- Environment notes: the Nuxt dev server was OOM-killed once while an
  8-worker test run shared the machine; uvicorn's dev reload waits for open
  streams and can hang after a code edit mid-stream (restart the backend).

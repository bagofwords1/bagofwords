# Feedback loop — one application for embedding and OAuth

An administrator should register an ERP or MCP client once, configure its agent
access, and enable embedding, OAuth, or both. Existing OAuth installations must
keep their credentials and grants. This loop verifies the shared model, the
edited unreleased migration, permission boundaries, and an actual embedded chat.

## Root cause and implementation

Previously OAuth registrations and hosted applications had separate names,
settings screens, lifecycles and identities. The artifact Embed modal could create
a host registration without exposing the OAuth/Entra setup beside it.

- `backend/app/models/connected_application.py:9` owns organization, name,
  enabled state, creator, timestamps and the agent ceiling. Each capability has
  an optional 1:1 row using the same application ID.
- `backend/alembic/versions/hostapp01_host_applications.py:62` replaces the
  unreleased host migration in place. It copies released OAuth rows, preserving
  IDs, secret hashes, redirects, scopes, trust, exchange settings and timestamps.
  Authorization codes and token rows retain their existing client identifiers.
- `backend/app/services/connected_application_service.py:67` reads the live
  application ceiling from the authenticated request origin. Agent discovery,
  direct reads, run selection, client construction, MCP schema tools and the
  connection-tool gateway apply it before admin/public bypasses. User and
  artifact permissions still apply. A restricted app token cannot change
  organization settings to widen its own ceiling.
- `backend/app/services/connected_application_service.py:47` revokes both
  capabilities' credentials on disable/delete. Issuance and lifecycle updates
  serialize on the parent application; ordinary OAuth and Entra acquire the
  user-session lock before the application lock.
- `frontend/components/embed/ConnectedApplicationsPanel.vue` is the common UI.
  The artifact Embed modal links to it, including the selected application.
  The existing capability endpoints and OAuth wire format remain available.

### Regression found during implementation

The first shared relationship used a left outer eager join. SQLite accepted it,
but PostgreSQL rejected the existing Entra `FOR UPDATE` query:

```text
ERROR: FOR UPDATE cannot be applied to the nullable side of an outer join
```

This caused **9 Entra failures** in the first PostgreSQL run (89 passed). A direct
SQL reproduction confirmed the cause. The capability's non-null parent FK allows
an inner join (`connected_application.py:48`); changing it fixed all 27 targeted
Entra/application tests. The final Entra issuance recheck also checks the shared
`enabled` flag (`entra_token_exchange.py:251`).

A new cross-organization test initially failed during seeding because signup was
closed, before exercising application code. It now creates a second organization
through the existing multi-organization fixture and checks list/update/delete and
both capability attachment endpoints against that actual tenant.

## Loop A — deterministic contracts

From a fresh checkout, use Python 3.13 and install the backend development
requirements (`uv sync --frozen --extra dev`). From `backend/`:

```bash
export BOW_DATABASE_URL=sqlite:///db/app.db
export TESTING=true
uv run pytest \
  tests/e2e/test_connected_applications.py \
  tests/e2e/test_oauth_app.py \
  tests/e2e/test_oauth_mcp.py \
  tests/e2e/test_entra_token_exchange.py \
  tests/e2e/dashboards/test_embed_host_applications.py \
  tests/e2e/dashboards/test_embed_identity_sources.py \
  tests/e2e/dashboards/test_artifact_conversations.py \
  --db=sqlite -q
```

Repeat with `--db=postgres`. Testcontainers starts PostgreSQL automatically;
`TEST_DATABASE_URL` can instead name a dedicated disposable PostgreSQL server.
Never point this test suite at a customer database: fixtures reset the schema.
The observed PostgreSQL run used PostgreSQL 16 in a temporary local container.
Docker's disk was full, so this container used a tmpfs database directory;
no existing containers or volumes were deleted.

The new endpoint initially returned 404 (two new tests failed before
implementation). The committed contracts now cover:

1. One parent for OAuth and embedding, shared rename, unchanged public IDs.
2. Default no-agent policy; legacy OAuth keeps an unrestricted application ceiling.
3. Disable/delete invalidates access, refresh and embed sessions. Re-enabling
   does not revive the old access token.
4. Members cannot register/manage applications. Administrators in another tenant
   cannot find, alter or attach capabilities to an application outside that tenant.
5. The application ceiling restricts administrators and members alike; selecting
   a private agent cannot grant a member access. Live edits affect existing tokens.
6. Real MCP `describe_tables` requests honor the same ceiling.
7. Embedded artifact policy and conversation creation intersect application,
   viewer and artifact selections; removing the last agent prevents chat.
8. Migration downgrade/upgrade against an API-created legacy OAuth registration
   preserves every stored client field. A pre-migration access token, refresh
   token and pending PKCE authorization code still work afterwards. The test
   also reruns the migration to check the corpus's unstamped/idempotent path.

Additional checks:

```bash
# backend/
uv run pytest tests/unit/test_skill_catalog_library.py tests/unit/test_agent_focus_scope.py -q
# frontend/
node --test tests/unit/embedSdk.mjs tests/unit/embedSession.mjs \
  tests/unit/embedViewerRefresh.mjs tests/unit/embeddedArtifactGate.mjs
```

Observed runs (2026-10-31):

| Run | Result |
|---|---|
| Full selection, PostgreSQL | 132 passed; before the last issuance-lock hardening and expanded lifecycle assertions |
| Final authentication/application subset, PostgreSQL | 53 passed |
| Full selection, SQLite | 131 passed, 1 skipped, 1 failed during cross-tenant test seeding |
| Final corrected authentication/application subset, SQLite | 52 passed, 1 skipped |
| Skill catalog + agent run scope | 33 passed |
| Frontend SDK/session/viewer/gate | 4 test files passed |
| Live UI-created OAuth integration | 4/4 checks passed |

The only SQLite full-run failure was the test fixture error described above;
the corrected test passed in both final subsets. The SQLite skip is the existing
concurrent first-exchange test, which runs on PostgreSQL. No targeted failures
remain. These are separate observed runs, not a claim that the whole repository
was tested or that the full selection was repeated after the last change.

The six changed Vue components/pages also passed SFC script/template compilation. Catalog key drift
was compared against the starting commit: missing/extra counts did not increase
in any of the ten catalogs. New copy is translated in English, Spanish and
Hebrew; the other catalogs currently use English fallback copy for these keys.

## Loop B — actual customer setup and live ERP

Use a fresh isolated database/configuration, `tools/agent/boot_stack.sh` and
`tools/agent/seed_org.py --demo`, with a working LLM configured through BOW.
The observed local run used backend `127.0.0.1:8101`, frontend
`127.0.0.1:3100`, and mock ERP `localhost:4174`. These are different sites.
Only seeded Chinook demo data was used.

1. Sign into BOW as the seeded administrator. In Settings → Channels → Connected
   applications, add **Chinook ERP**. In General & access, select **Music Store**
   and save. This was done through the real UI.
2. In Embedding, allow `http://localhost:4174` and choose BOW sign-in (SSO).
3. In API & MCP (OAuth), configure the same application with `app mcp` scopes and
   redirect `http://localhost:4174/callback`. Copy the one-time secret only into
   a private local file; never put it in the ERP HTML or screenshots.
4. Create/publish a chatbot artifact using the checked-in
   `tools/agent/embed/music-store-assistant.html`. This pass reused that previously
   BOW-generated source and saved/published it through the real artifact API;
   it did not ask a model to generate the artifact again.
5. Open the artifact's Embed tab, allow Chinook ERP, choose Floating, and copy its
   SDK snippet. Confirm that **Manage applications and authentication** points
   to this application's settings. Serve a mock ERP page on port 4174 containing
   that snippet. The SDK loads from BOW's `/api/embed/sdk/v1/bow-embed.js` endpoint.
6. Open the floating launcher, use Sign in, and ask:
   **How many tracks are in the Music Store?** The actual model streamed an
   answer with **3,503 tracks**, a structured result table and a CSV control.
   Close/reopen retained the completed conversation.
7. Remove Music Store in the BOW UI and save. Reloading the authenticated widget
   showed **Chat is not available here** with composer and Send disabled.
   Restore the selected agent afterwards. The unchecked state also survived a
   settings-page reload.
8. Switch the sandbox organization to Hebrew, inspect the common settings and
   restore English. Both directions rendered the new tabs, labels and controls.

The same UI-created OAuth client additionally completed real local API calls for
PKCE authorization/code exchange, selected-agent listing, MCP initialize, and
refresh rotation (old access token rejected, new access token accepted): **4/4**.
The live chat used a real model and local BOW popup sign-in; it did not exercise
an external IdP. The Entra tests in the deterministic suite substitute the
external identity-provider boundary.

A minimal SSO-mode ERP page can use the copied public values:

```html
<script src="http://127.0.0.1:3100/api/embed/sdk/v1/bow-embed.js"></script>
<script>
BOWEmbed.mount({
  baseUrl: 'http://127.0.0.1:3100',
  hostApp: 'COPY_PUBLIC_HOST_ID_FROM_SNIPPET',
  artifact: 'COPY_ARTIFACT_REFERENCE_FROM_SNIPPET',
  mode: 'floating',
  title: 'Music Store Assistant'
});
</script>
```

For the signed-assertion variant, the maintained UI setup helper is
`tools/agent/embed/admin_setup.mjs`. Supply `AGENT_NAME` in addition to its
existing environment variables; it now creates a Connected application, selects
that agent, then configures its Embedding capability. The private key stays on
the mock ERP backend (`erp_host.py`). The helper was syntax checked; this pass
performed the equivalent UI steps manually through the browser.

## Evidence and limits

Before/after, OAuth, Embed modal, Hebrew, actual ERP answer, and no-agent state:
`media/pr/connected-applications/`. `flow.gif` is an annotated-by-context sequence
of actual browser screenshots, not a continuous video or a timing measurement.

The live macOS sandbox ran in development mode without Linux Landlock; this is
not production confinement evidence. The final pass does not certify an actual
ServiceNow deployment, an air-gapped network, every browser, customer RLS rules,
or a new live Microsoft Entra/Power BI/Fabric OBO run. The earlier real two-user
Entra/Fabric evidence remains in `erp-embed-entra-customer.md`.

The migration revision is intentionally unchanged, as requested. Databases that
already applied the **old unreleased hostapp01** need a fresh disposable sandbox
or restoration of a pre-migration backup before upgrading with this branch. Do
not merely stamp the old tables as the new schema. Released OAuth databases
upgrade through the edited migration without credential rotation.

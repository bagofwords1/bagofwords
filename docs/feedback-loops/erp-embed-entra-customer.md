# ERP embedding with Entra — customer feedback loop

PR #1532. Starting revision: `4d767231a`. This loop uses a fresh local
database, a separate ERP origin, two real Entra demo identities, a Music Store
chatbot authored through BOW's UI, and delegated Power BI/Fabric connections.
Secrets must come from environment variables; do not commit tokens, browser
storage, credentials, or raw authentication traces.

## Integration contract

The ERP backend completes Entra authorization code + PKCE for its own client,
requesting BOW's `access_as_user` scope. It calls BOW's existing RFC 8693
exchange, which validates the external client and provisions delegated
connection credentials. It then serves a short-lived, single-use signed host
assertion to the embed SDK. The embed session is distinct from the server-held
`app` token. The host application uses `oidc:entra`, `uid_claim: oid`, and
`create_users: false`; both users are mapped beforehand through ordinary SSO.
No subject, email, role or token supplied by browser context grants access.

`tools/agent/embed/erp_entra.py` is the customer-style mock ERP. It extends
the existing authorization-code demo and mounts the SDK from BOW's own
endpoint in `floating` mode. It is a loopback development harness with
memory-only sessions, not a production ERP session implementation.

## Observed issues and fixes

1. **Wrong callback origin on non-default ports.** The frontend proxy's
   `configure` handler in `frontend/nuxt.config.ts` replaced the incoming host
   with `localhost:3000`. Real sign-in from port 3100 therefore sent the
   authorization code to another BOW instance. The handler now preserves the
   request Host when no upstream forwarded host is supplied.
   Before: `/api/auth/entra/authorize` returned redirect URI
   `http://localhost:3000/api/auth/entra/callback`.
   After: the same request returned
   `http://localhost:3100/api/auth/entra/callback`; both demo users completed
   real Microsoft login and reached the isolated BOW instance.
2. **Sandbox OpenAI credits exhausted (external setup).** The first real
   chatbot generation returned the provider's no-credits error. Switched to
   the existing Claude provider; creation then completed through
   `create_artifact`. This is not reported as a product fix.
3. **Nested launchers in generated chatbots.** The generated artifact owned
   a second floating launcher. Minimizing it left the host SDK's panel blank.
   Updated `backend/app/ai/skills/library/create-chatbot.md` to assign the
   floating shell to the host SDK; edited and published the actual artifact
   through BOW as v2. Before/after screenshots are in `media/pr/erp-embed-entra/`.
4. **History clicks dismissed the generated history overlay.** Its wrapper's
   click handler ran on bubbled `ChatThreads` clicks. Removed it through BOW's
   authoring flow (v3), verified opening and selecting a saved conversation,
   and added the rule to the chatbot skill. The saved, generated source is
   `tools/agent/embed/music-store-assistant.html` (requires artifact globals).
5. **Fabric extraction pools crossed credential boundaries.**
   `MsFabricClient.extraction_engine` keyed pools by `identity[:64]`. The two
   real delegated tokens differ but their first 64 characters are identical.
   Three isolated tests reproduced engine reuse across users, secret rotation
   and tenants. Hash the complete delegated token, or the complete service
   credential tuple, instead. The same credentials still reuse an engine.
   This affects pooled extraction, not the direct `execute_query` path; the
   live probes below do not claim that cross-user data was actually returned.
6. **macOS ODBC library loading (environment).** Fabric initially returned
   SQLSTATE `08001` with a connection-encryption error; TCP 1433 was reachable.
   Setting `DYLD_LIBRARY_PATH=/opt/homebrew/opt/openssl@3/lib` for this sandbox's
   Python process made both real-user queries pass. `Encrypt=yes` and
   `TrustServerCertificate=no` remained enabled. No system library links were
   changed. See Microsoft's [ODBC macOS known issues](https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/known-issues-in-this-version-of-the-driver).

### Reproduce the proxy and pool failures without credentials

```sh
git show 4d767231a:frontend/nuxt.config.ts > /tmp/bow-nuxt-before.ts
cd frontend
BOW_TEST_NUXT_CONFIG=/tmp/bow-nuxt-before.ts node --test tests/unit/devProxyOrigin.mjs
node --test tests/unit/devProxyOrigin.mjs
```

Observed before: expected `localhost:3100`, received `localhost:3000`.
Observed after: pass for two direct hosts/ports and an HTTPS ingress. The
harness uses the same `@refactorjs/http-proxy` implementation as Nuxt's proxy
module and a real local HTTP upstream, not a string check.

```sh
cd backend
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true python -m pytest \
  tests/unit/test_fabric_pool_identity.py -q --tb=short
```

Observed before the pool fix: **3 failed** (different credentials returned the
same SQLAlchemy engine). After: **3 passed**. No Fabric connection is opened
by this test. The wider pool lifecycle/identity and skill tests: **55 passed**.

## Deterministic authentication regression

From `backend/`, with Python 3.13 and development dependencies:

```sh
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true python -m pytest \
  tests/e2e/dashboards/test_embed_host_applications.py \
  tests/e2e/dashboards/test_embed_identity_sources.py \
  tests/e2e/test_entra_token_exchange.py \
  --db=sqlite -q --tb=short
```

These tests use signed mock IdP tokens with real BOW routes, admission,
membership checks and database operations. They do not prove real Entra
consent, browser framing, downstream permissions, or an LLM answer.

## Live setup

1. Boot BOW from this branch with a fresh database, stable encryption key,
   a working model provider, and an enabled tenant-specific Entra provider.
   Configure `uid_claim: oid` and BOW's delegated `access_as_user` scope.
2. Invite the two demo identities and complete normal BOW SSO once for each.
   Verify there are no saved connection credentials before the ERP test.
3. In BOW, ask it to create a Music Store chatbot using the built-in chatbot
   skill. Publish it, share it with the intended members, configure its model
   and agent policy, and allow the ERP host application.
4. Create the Power BI/Fabric connections with `user_required` and OAuth;
   use the BOW Entra application's credentials. Fabric SQL requires the
   Azure SQL delegated scope, not merely Fabric REST permissions.
   Refresh the user's accessible schema, then activate the intended tables
   in each agent. Discovery alone leaves newly discovered tables inactive.
   We activated one Power BI table and one Fabric table, and probed constants.
5. Create a confidential BOW OAuth client with Entra exchange enabled for
   the ERP client ID, and a host application in signed-assertion mode.
6. Launch the ERP with environment variables below. The callback must be
   registered on the ERP's Entra client. Keys and secrets stay server-side.

```sh
export BOW_URL=http://localhost:3100
export ENTRA_CALLBACK_URL=http://localhost:4173/entra/callback
export ERP_PORT=4173
export ENTRA_TENANT_ID=<tenant>
export EXTERNAL_ENTRA_CLIENT_ID=<erp-client>
export BOW_ENTRA_CLIENT_ID=<bow-client>
export EXTERNAL_ENTRA_SECRET=<secret>
export BOW_CLIENT_ID=<bow-oauth-client>
export BOW_CLIENT_SECRET=<secret>
export BOW_HOST_APP=<host-public-id>
export BOW_HOST_PRIVATE_KEY=<private-key>
export BOW_ASSISTANT=<published-artifact-id>
python -m tools.agent.embed.erp_entra
```

## Verification record

- Real BOW Entra login: demo1 and demo2 passed.
- Before ERP login: both identities existed; delegated credential count was zero.
- First ERP login: demo1 passed real external Entra → BOW exchange. A new,
  active Power BI credential was stored for demo1. Demo2 remained without one.
- Chatbot authoring: saved artifact through the real UI; its automatic preview
  check is simulated and is not counted as a live answer.
- Live Music Store: streamed answer and actual result table; top artist revenue
  was Iron Maiden 138.60, U2 105.93, Metallica 90.09 (seeded Chinook data).
- Closing/reopening the outer launcher preserved the running conversation.
- Selecting a customer updated the advisory host-context banner immediately.
- Live Power BI from the widget: both demo users returned `OBO_Probe = 1`.
  Demo1 also returned its own `USERPRINCIPALNAME()` in the widget.
- Live Fabric from the cross-site widget: demo1 returned `OBO_Probe = 1`
  after refreshing Fabric access and activating a table. Demo2's Fabric
  credential passed the direct client probe, but its failed earlier schema
  discovery still needed its own Refresh access; Fabric chat was not counted
  as passed for demo2. Schema recovery is per user, not shared by account.
- Downstream identity probes using each real user's OBO-provisioned credential
  and BOW's actual connection clients: **4/4 passed** — Power BI and Fabric
  each returned probe=1 and the correct demo1/demo2 account. No business rows
  were read. Fabric's query was `SELECT 1 AS OBO_Probe, SUSER_SNAME() AS Viewer`.
- ERP demo2 operated while the ordinary BOW tab was still signed in as demo1.
  Demo2's history showed its own conversation, not either demo1 conversation.
- Cross-site repeat: ERP `http://localhost:4173` embedded BOW
  `http://127.0.0.1:3100`; no BOW login was performed on 127.0.0.1. Real Entra
  ERP login, host-assertion authentication, Music Store query/table and
  close/reopen passed. This is a different-site test, not merely two ports.
  Chrome's third-party-cookie settings were not changed. Reproduce by binding
  Nuxt explicitly to `127.0.0.1`, setting the ERP's `BOW_URL` to that origin,
  and keeping the ERP callback on localhost. The GIF is a sequence of the
  actual open → closed → reopened browser states, not a streaming video.
- History selection restored saved messages after reload; New cleared the
  current view; Stop interrupted a real streamed answer and displayed stopped.
- Auth regressions: **72 passed, 1 skipped**. Five frontend test files covering
  SDK, expiry/renewal, identity refresh, gate races and chat components passed.
  The new proxy regression also passed (six frontend files total).

## Limits and deployment checks

- The live host is a mock ERP, not a ServiceNow instance. Run the same flow in
  the customer's actual host, including its CSP and iframe restrictions.
- Both users were existing BOW members with an Entra identity; automatic user
  provisioning and group sync were intentionally disabled.
- The real LLM sometimes refused a safe diagnostic or incorrectly claimed the
  DAX client was unavailable. A fresh constant-probe conversation succeeded.
  The direct client identity checks distinguish auth correctness from model
  variability. Do not represent model behavior as deterministic.
- Local Python ran in development subprocess mode because macOS lacks Linux
  Landlock. Production Linux confinement is not proven by this run.
- Real identity probes prove whose credentials executed; they do not prove a
  particular customer's RLS rules or all dataset permission combinations.
- This harness has in-memory, one-hour sessions and requires re-login on expiry.
  A production ERP must own session renewal/logout and keep all app secrets
  server-side. The embed SDK's renewal/race behavior has deterministic tests;
  a full one-hour real-Entra expiry soak remains a deployment check.
- Air-gapped deployment, Safari/Firefox, production HTTPS/reverse proxies,
  and full-repository/Postgres test suites were not exercised here.

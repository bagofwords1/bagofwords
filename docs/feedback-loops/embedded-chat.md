# Feedback Loop — "deploy BOW chat inside our ERP / ServiceNow with our users' own permissions"

**Status: IMPLEMENTED and VERIFIED (2026-10-28).** Loop A (deterministic, credential-free)
passes: 30 backend e2e tests + 15 unit tests on SQLite, and a 6-test Playwright journey that
drives a stand-in ERP on its own origin like an employee would. Evidence in
`media/pr/embedded-chat/`. Design: `docs/design/embedded-chat.md`.

The claim validated here: a customer can configure an **embed** in BOW, install a script that
this BOW instance serves, add one assertion endpoint to their backend, and their employees get a
compact branded chat inside the host application — signed in as their **existing BOW user**,
limited to the agents/tools the admin allowed **∩ their own access**, with the identity that
reaches MCP tools coming from BOW's member attributes and never from the browser.

## Baseline (before the change)

There was no embed contract at all: `GET /api/embed/<anything>/bootstrap` → 404 and `/embed/<id>`
served the SPA shell with the site-wide `frame-ancestors` (BOW origin + Office only), so no host
page could frame a chat and no host could sign an employee in. The artifact-page bubble
(`frontend/components/report/ArtifactChatBubble.vue`) was the only compact chat and was bound to
`/r/{id}` + an app session. This is a missing feature, not a defect in existing code.

## The change (what to read, in order)

| Piece | Where |
|---|---|
| Embed + replay tables, `reports.embed_id` (+ one-thread-per-user unique index), token `embed_id` | `backend/alembic/versions/embed01_embedded_chat.py`, `backend/app/models/embed.py` |
| Config, RS256 assertion contract, sessions, threads, roster ∩ access, forced-off tools | `backend/app/services/embed_service.py` |
| `/api/embed/*` (SDK, bootstrap, assertion → session, preview, chat, watch, cancel, approvals) | `backend/app/routes/embed.py` |
| `/api/embeds/*` admin (manage_settings; private key returned once) | `backend/app/routes/embed_admin.py` |
| Empty roster is never Auto for `embed_chat` (as for `artifact_chat`); `<embed_context>` in the planner prompt | `backend/app/ai/tools/implementations/agent_focus_common.py`, `backend/app/ai/agents/planner/prompt_builder.py` |
| The SDK (served by BOW) | `backend/app/static/embed/bow-embed.js` |
| Chat document, host handshake, in-memory session | `frontend/pages/embed/[id].vue`, `frontend/composables/useEmbedHost.ts`, `useEmbedSession.ts` |
| Shared compact chat (extracted from the artifact bubble, now used by both) | `frontend/components/chat/*`, `frontend/composables/useCompactChat.ts` |
| Per-embed `frame-ancestors` when Nuxt serves the SPA / when FastAPI serves it | `frontend/server/middleware/embed-frame.ts`, `routes/embed.py::frame_router` |
| Settings → Embedded chat | `frontend/pages/settings/embeds.vue`, `frontend/layouts/settings.vue`, `locales/*.json` (`embed.*`, `settings.embeds.*`, `errors.embed.*`) |

## Loop A — deterministic (no external services)

### A1. Backend contract (routes → services → DB, agent loop stubbed)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/unit/test_embed_service.py -q
uv run pytest tests/e2e/test_embedded_chat.py -m e2e --db=sqlite -q
```

Observed: `15 passed` and `30 passed in 42.31s`. Regression neighbours run alongside — artifact
chat and MCP dynamic params:

```bash
uv run pytest tests/e2e/test_artifact_chat_model.py tests/e2e/test_artifact_chat_viewer_data.py \
  tests/e2e/test_mcp_dynamic_params.py tests/e2e/test_embedded_chat.py -m e2e --db=sqlite -q
```

Observed: `63 passed in 76.64s`. Unit tests around the touched roster/prompt/settings code
(`-k "agent_focus or prompt_builder or artifact_chat or report_api_settings"`): `21 passed`.

What the e2e file pins down (each test named after the invariant):

- private key readable once (create / rotate), never from GET or the list; admin routes 403 for a member;
- revision-checked updates (409), validated origins/agents/model/tools/colors/attribute keys;
- bootstrap is public and appearance-only; the SDK is served as JavaScript;
- assertion matrix: wrong key, wrong issuer, wrong audience, expired, over-long, future `iat`,
  missing `jti`/`sub`, HS256-with-public-key confusion, `alg: none`, garbage → 401
  `embed.assertion_invalid`, no token; replay → `embed.assertion_replayed`;
- identity: unmapped → `embed.identity_unmapped`; the member's **email** as `sub` → unmapped; two
  members with the same value → `embed.identity_ambiguous` until one is cleared; same key+value in
  **another organization** → unmapped; disabled embed → 403 `embed.disabled`;
- an embed session is refused by `/api/reports`, `/api/users/whoami`, `/api/organizations`,
  `/api/embeds`, `/api/data_sources`, and by another embed's routes;
- logout, "sign everyone out", key rotation (old sessions AND old-key assertions die, new key
  works) and disable all end sessions; the admin preview session is the admin's own identity;
- chat runs on a hidden `embed_chat` thread bound to (embed, user), invisible in the member's
  report list, isolated per user; the thread carries the embed model, follow-ups, the forced-off
  tools plus admin switches, and roster = embed list ∩ the user's access (a private agent the
  member cannot reach stays off; making it public applies on the next turn without a new session);
- agent selection refused unless allowed and within the roster; oversized context/metadata → 400;
- the same idempotency key three times → one turn; watch/cancel 404 on another user's run;
- deleting the embed ends sessions and threads and leaves the shared agent intact.

Not run here: the PostgreSQL leg (`--db=postgres` needs Docker; `docker info` fails in this
sandbox). The migration uses batch mode and a partial unique index with both
`sqlite_where` and `postgresql_where`, the same pattern as `dashfc04`/`mainbuild01`.

### A2. Browser journey against a stand-in ERP on its own origin

Sandbox (all local; the only network the browser touches is `127.0.0.1:4173` and `localhost:3000`):

```bash
# BOW (backend :8000, frontend dev server :3000, devtools badge off for clean captures)
NUXT_DEVTOOLS=0 tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py            # admin@example.com / Password123!

# echo MCP server (what the agent's tool actually receives), scripted LLM, seed, ERP host
MOCK_MCP_CAPTURE_FILE=/tmp/bow-agent/mcp_capture.json uv run python tests/mocks/echo_mcp_http_server.py --port 3333 &
STUB_PORT=9099 uv run python ../tools/agent/embed/stub_llm.py &
uv run python ../tools/agent/embed/seed_embed.py                   # members, MCP agent, stub model, the embed, private key
EMBED=$(python3 -c "import json;print(json.load(open('/tmp/bow-agent/embed.json'))['embed']['public_id'])")
BOW_ORIGIN=http://localhost:3000 BOW_EMBED_ID=$EMBED BOW_EMBED_PRIVATE_KEY_FILE=/tmp/bow-agent/embed-private.pem \
  uv run python ../tools/agent/embed/erp_host.py --port 4173 &

cd ../frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npx playwright test --config=pw.embed.config.ts
```

Observed: `6 passed (56.3s)`.

| Test | What a person would see | Evidence |
|---|---|---|
| floating assistant: alice → context → reload → bob → unmapped carol | Launcher in the ERP corner; opens; Alice is signed in without typing anything; her question calls the ERP orders tool and the answer is scoped to **E-1001** with `workflow_id` from the host; clicking INC-1042 and asking about "this incident" puts the record in the answer; reload keeps the thread; switching to Bob ends Alice's session, Bob sees only his own (empty) thread and gets **E-1002**; Carol (ERP user, no BOW mapping) gets a clear "not mapped" refusal, no chat | `01`–`08`, `journey.gif`, `network-hosts.json` |
| inline, dark, Hebrew | Inline panel in the page; host theme `dark` honoured (embed theme `auto`); `?locale=he` → `dir=rtl`, Hebrew placeholder, bidi-correct bubbles | `09`, `10` |
| tampered assertion | A forged subject fails signature verification → "The sign-in from your application was not accepted" | `11` |
| unlisted origin | A page on `evil.example` frames `/embed/{id}`: document carries `frame-ancestors 'self' http://127.0.0.1:4173 http://localhost:4173`, the frame never runs (no bootstrap call, no root), ERP origin present, attacker absent | `12` |
| settings | Overview / Agents & model / Authentication (mapped members) / Appearance / Install (snippet + Python/Node examples); Preview renders the real chat signed in as the admin | `13`–`18` |
| artifact-page chat | `/r/{id}` bubble still opens, sends and streams on the shared components | `19` |

The MCP capture asserted on every answer: `custom_metadata = {employee_id, user_email,
workflow_id, erp_user}` and header `X-Employee-Id` equal the signed-in employee, never anything
the browser sent.

## Issues found by the loop and fixed before merge

- **Streamed markdown collided across messages.** `@nuxtjs/mdc`'s `<MDC>` keys its parse cache by
  a hash of the *initial* value; two streamed answers that open with the same chunk shared one
  entry and the second message rendered the first one's text. Fixed with a per-block `cache-key`
  in `CompactChatThread.vue` (this also hardens the artifact bubble, which had the latent bug).
- **History counted before it loaded** made the first version of the journey flaky; the panel now
  exposes `data-history-loaded` and the spec waits for it.
- Stub-only: the scripted LLM saw an earlier turn's tool observation in the prompt and skipped the
  tool; it now only looks after `<user_prompt>` (BOW's own duplicate-call guard had caught the
  repeat, which is itself a nice confirmation that the guard works through the embed).

## What this proves / what it does not

Proves: the contract above end to end on SQLite, with a real streamable-HTTP MCP server, real
routes/services/DB/SSE, a real browser across two origins with third-party cookies irrelevant
(bearer in iframe memory), and the egress of the embed surface limited to the BOW origin.

Also checked by hand: the FastAPI-served path the Docker image uses (`SERVE_FRONTEND=1`, a
scratch `FRONTEND_DIST_DIR`, uvicorn on :8001): `/embed/{id}` answered with
`frame-ancestors 'self' http://127.0.0.1:4173 http://localhost:4173`, an unknown id with
`frame-ancestors 'none'`, any other SPA page with the unchanged site-wide policy, and the SDK with
`application/javascript`. `yarn build` (client + server) completes.

Does not prove: PostgreSQL (no Docker here), a real LLM's behaviour (the planner is scripted), a
real ServiceNow/ERP extension surface, and SSO/Entra flows (deferred by design).

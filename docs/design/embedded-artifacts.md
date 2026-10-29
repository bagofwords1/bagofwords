# Embedding BOW artifacts in host applications — plan

Status: **phases 1–3 built, phase 4 in progress** on `ai/exciting-goodall-m7toth`
(revised 2026-10-29; §12 is the as-built record and what is left). Supersedes the
"embedded chat" prototype that lived on this branch (commit `544eb0c`, removed in
the next commit; see §11 for what was ported from it).

## 1. Decisions

| # | Decision |
| --- | --- |
| 1 | **The artifact is what gets embedded.** No separate "embedded chat" product. A chat assistant, a data app or a dashboard is an artifact; the customer frames it in their application (ERP, ServiceNow, intranet). |
| 2 | **Host applications are organization integrations.** A host application is one registered system ("Northwind ERP — production"): allowed origins + how it vouches for its users. It lives in Settings → Integrations next to OAuth apps. Admins create it there, or inline from an artifact's Embed section. One per system, never one per artifact. |
| 3 | **Embedding is opt-in on both sides.** The artifact lists the host applications it may appear in, and the host application must be enabled. Embedding never widens the audience: an embedded viewer must also pass the artifact's normal sharing. |
| 4 | **Every embedded viewer is a real BOW user.** No anonymous or "app owns data" mode. Users come from the organization's identity provider — created in advance (SCIM, LDAP sync, invitations) or just in time on first arrival, according to the policies BOW already has. |
| 5 | **Any SSO, not only Entra.** OIDC (Entra, Okta, Keycloak, Google, ADFS-OIDC), SAML (incl. ADFS, Ping), and on-prem Active Directory through LDAP. §4 describes how each one signs in silently. |
| 6 | **The SDK exposes reports and completions, not "agents".** A report can carry several agents, and users expect to come back to past conversations. Generated code calls `bow.reports.*` / `bow.completions.*` on the viewer's own conversations. The policy (which agents, which model, which tools) lives on the artifact and is enforced by the server, never by generated code. |
| 7 | **Snippets live on the artifact.** The artifact Settings window gets an **Embed** tab with the install snippets and backend examples. The organization settings page has no embedding page of its own. |

## 2. What exists today (verified on main `c0b0d92`)

**Reports API** (`backend/app/routes/report.py`)

- `POST /api/reports` — title, `data_sources` (any number of agents), `model_id`, `reasoning_effort`, `follow_ups`, `suggest_instructions`, `tools` (public switches, `app/schemas/report_api_settings.py`), `project_id`.
- `GET /api/reports` — page/limit, `filter=my|published`, search, status, mode, data source, project; regular reports only.
- `GET/PUT/DELETE /api/reports/{id}`; star, publish, fork, schedule, notes, summary.

**Completions API** (`backend/app/routes/completion.py`)

- `POST /api/reports/{id}/completions` — prompt (content, `model_id`, `reasoning_effort`, `metadata`, follow-up opt-outs); SSE when `stream: true`, JSON otherwise; queue mode.
- `GET /api/reports/{id}/completions` — v2 transcript with blocks, `limit` + `before` cursor.
- `GET /api/reports/{id}/completions/{cid}/stream` — re-attach to a running turn.
- `POST /api/completions/{cid}/sigkill` (stop), `.../mcp_tool_confirmations/{id}` (approve/deny), `.../clarify_response`, `.../steer`.
- Owner-only, `create_reports` / `view_reports`. Callers: app JWT, OAuth `app`-scope tokens, API keys.

**Dashboard viewer chat** (`backend/app/services/dashboard_chat.py`, `routes/dashboard.py` `/d/{ref}/chat*`)

- Already the policy model we need: `Artifact.chat_enabled`, `chat_data_source_ids` (agent allowlist, **multiple agents**), `chat_model_id`; roster = allowlist ∩ the viewer's own accessible agents, never Auto; threads are hidden `report_type='artifact_chat'` reports found by `reports.source_artifact_id`; tool ceiling `ARTIFACT_CHAT_TOOL_ALLOWLIST` (`app/ai/agent_v2.py`). Configured in the dashboard Settings window → Viewers.
- Limitation: one thread per viewer per dashboard.

**Artifact runtime** (`backend/app/routes/artifact_resources.py`, `frontend/composables/useArtifactRuntime.ts`, `frontend/public/libs/artifact-sdk.js`)

- Generated code holds **no credentials**: the SDK talks over a `MessagePort` to a broker in the BOW frame, which calls `/api/artifacts/{id}/runtime/*` with the viewer's session. The sandbox has `connect-src 'none'`.
- Existing SDK modules: `bow.data`, `bow.context`, `bow.records`, `bow.files`, `bow.ai.stream` (a single bounded model call — no agents, tools or persistence).
- Viewer resolution: `ArtifactResources.open` (members get resources; public viewers read-only); rate limiting via `admit`.

**Framing** (`backend/app/core/security_headers.py`): `/r/` and `/c/` frameable by anyone; everything else, `/d/` included, only by BOW and Office hosts.

**Identity stores and JIT** (what §4 maps a host's user onto)

| Source | Where the stable identity lives | Just-in-time creation today |
| --- | --- | --- |
| OIDC (any provider) | `oauth_accounts (oauth_name, account_id)`; `account_id` = provider's `uid_claim` (default `sub`) | Org invitations / `signup_policy.allowed_domains` (`auto_provision_user_for_org`, `core/auth.py`) |
| Entra (OIDC) | additionally `oauth_accounts.entra_identity` (tenant, object id, client) | Same, plus the RFC 8693 exchange admits via `entra_admission.resolve_or_admit` and provisions OBO credentials |
| SAML | `saml_identities (provider, issuer, subject, identity_hash)` | `SAMLProvider.auto_provision_users` or invitation (`saml_service.admit_user`) |
| Active Directory / LDAP | `users.ldap_subject`, `ldap_dn` | `LDAPConfig.auto_provision_users` + `admission_group_dn` |
| SCIM | `users.scim_external_id` | Created by the IdP ahead of time |
| Local | email + password | Invitations |

Existing rule kept everywhere: **never link an existing account by email.** SAML needs an explicit `account_links` entry; Entra admission refuses to claim old users.

## 3. Target architecture

```
Host page (customer origin)                       BOW
┌─────────────────────────────┐   script   ┌────────────────────────────────────────────┐
│ <script bow-embed.js>       │──────────▶ │ /api/embed/sdk/v1/bow-embed.js             │
│ BOWEmbed.mount({hostApp,    │   frame    │ /d/{ref}?host={hostApp}                    │
│   artifact, mode, identity})│◀─────────▶ │   CSP frame-ancestors = that host's origins│
│        │ postMessage(nonce) │            │   (only if the artifact allows that host)  │
│        ▼                    │            │                                            │
│ identity callback ──▶ host  │            │ POST /api/embed/{hostApp}/session          │
│   backend: assertion /      │──────────▶ │   assertion | IdP token | SSO handoff code │
│   IdP token / (none: popup) │            │   → user (existing or JIT) → embed session │
└─────────────────────────────┘            │ embed session accepted ONLY by:            │
                                           │   /api/d/{ref}/*  (artifacts allowing host)│
                                           │   /api/artifacts/{id}/runtime/*            │
                                           │     incl. reports/completions (§5)         │
                                           └────────────────────────────────────────────┘
```

Inside the frame nothing new is invented: the existing shared-dashboard page renders the artifact, and the existing runtime broker serves the SDK. The only change in the browser is **which credential** the page and broker use (an in-memory embed session instead of the app login).

## 4. Identity: one host application, three ways to sign in

A host application has exactly one **identity mode**. Every mode ends in the same place: a real BOW user who is an active member of the organization, then an embed session bound to `(host application, user)`.

### 4.1 Host assertion — works with any SSO (default)

The host's backend already knows who is signed in to the host (whatever SSO the host uses). It signs a short-lived RS256 JWT; BOW verifies it with the stored public key (BOW generates the pair and shows the private key once).

- Claims: `iss`, `aud` (= host application id), `sub`, `iat`, `exp` ≤ 2 minutes, unique `jti` (single use), optional `scp` (`view`, `converse`), optional `email`/`name` (used only for JIT, never for matching).
- **Identity source** — configured per host application, decides what `sub` means:

| Identity source | `sub` is | Resolved against |
| --- | --- | --- |
| `oidc:<provider>` | the IdP subject (`uid_claim`), e.g. Entra object id, Okta user id | `oauth_accounts (oauth_name, account_id)` |
| `saml:<provider>` | the SAML subject (NameID or configured attribute) | `saml_identities` (same `identity_hash` computation as login) |
| `ldap` | AD `objectGUID` (or the configured LDAP subject) | `users.ldap_subject` |
| `scim` | the IdP's SCIM `externalId` | `users.scim_external_id` |
| `member_attribute:<key>` | any value an admin set on the member | `memberships.custom_attributes[key]` (deployments without a directory) |

- **Unknown user**: refused by default (`identity_unmapped`). Optional per-host-application **"create users on first sign-in"**, delegated to *that source's* existing admission — OIDC → `auto_provision_user_for_org` (invitation/domain policy), SAML → `admit_user` honoring `auto_provision_users`, LDAP → BOW looks the subject up in AD with the service account and requires `admission_group_dn` membership before creating it (directory-verified, not host-trusted). New users get the organization's default role; data access comes from groups (SCIM/LDAP/OIDC group sync).
- Ambiguous matches, inactive users, non-members → refused; no email fallback.

### 4.2 IdP token exchange — OIDC identity providers, no BOW key

For hosts that already hold an OIDC token for the user (custom apps on Entra/Okta/Keycloak): the host backend calls BOW's RFC 8693 token endpoint with its OAuth client credentials and the user's IdP token (audience = BOW's client id at that IdP). BOW verifies it against the provider's issuer/JWKS — Microsoft or Okta vouches, not the host.

- Generalize `services/entra_token_exchange.py` from "tenant-specific Entra only" to any configured `OIDCProvider`, keeping its checks (issuer, audience, allowed client, signature from discovery JWKS, never token-controlled URLs).
- Resolution and JIT exactly as OIDC login (`oauth_accounts`, `auto_provision_user_for_org`); Entra keeps `resolve_or_admit` and OBO credential provisioning.
- Result is an embed session for the host application, not an `app`-scope token.

### 4.3 SSO popup — no host backend at all

For hosts that cannot run server code: the frame shows "Sign in", opens BOW's normal login in a **top-level popup** (OIDC, SAML, LDAP form — whatever the org uses), and the popup hands a single-use code back to the frame by `postMessage` (exact origin, nonce). Visible on first use and after expiry; JIT exactly as normal login. This is the only mode that works with third-party cookies blocked and no backend, which is why every BI vendor offers it as the fallback.

### 4.4 Sessions (all modes)

- Stored in `oauth_mcp_access_tokens` (prefix `bow_embed_`, scope `embed`, `host_application_id`), 1 hour, held in frame memory only, renewed silently through the same mode.
- Re-checked on every request: expiry, host application enabled + epoch, user active + `session_epoch`, organization membership, and that the artifact allows this host application.
- Revocation: logout, "sign everyone out", key rotation and disabling bump the host application's epoch.
- Accepted only by the routes in §3. Every other route keeps rejecting them.

## 5. Reports and completions in the artifact SDK

### 5.1 Policy: the artifact's existing viewer-chat settings, extended

Reuse `Artifact.chat_enabled / chat_data_source_ids / chat_model_id` (Settings → Viewers) as the **conversation policy** for both the stock dashboard chat bubble and SDK code. Extend it with:

- `chat_tools` — public tool switches (same vocabulary as `report_api_settings.TOOL_SWITCHES`), applied on top of the artifact-chat tool ceiling;
- `chat_max_conversations` per viewer (default e.g. 50; oldest archived);
- the **tool ceiling decision** (§10, item 2): today `ARTIFACT_CHAT_TOOL_ALLOWLIST` excludes MCP tools.

### 5.2 Conversations

- A conversation is a hidden `report_type='artifact_chat'` report with `source_artifact_id` = the artifact — the existing dashboard-chat thread, minus the one-per-viewer limit.
- Created with `data_sources` ⊆ policy allowlist ∩ the viewer's access (a viewer may pick a subset, so a report keeps its multi-agent shape); model, effort and tools from the policy, re-applied on every turn (owner changes apply immediately — existing behavior).
- Invisible in the main report list (already true for `artifact_chat`).

### 5.3 Runtime routes (server-side, under `/api/artifacts/{id}/runtime/conversations`)

As built (`backend/app/routes/artifact_conversations.py`, `services/artifact_conversations.py`):

| Route | Does |
| --- | --- |
| `GET  /policy` | available or why not (`auth_required`, `disabled`), scope, the agents this viewer may use |
| `GET  ?limit&before` | the viewer's conversations on this artifact, newest first |
| `POST` `{title?, agents?}` | create; `agents` must be ⊆ policy ∩ the viewer's access (`conversation.agent_forbidden`) |
| `GET/PATCH/DELETE /{cid}` | read, rename, archive — the viewer's own only (`conversation.not_found` otherwise) |
| `GET  /{cid}/messages?limit&before` | `get_completions_v2`, flattened to `{id, role, text, status, tools[]}` |
| `POST /{cid}/messages` `{text}` | `create_completion_stream`, re-emitted as NDJSON: `started`, `tool`, `text_delta`, `text`, then exactly one `completed` or `error` |
| `POST /{cid}/messages/{mid}/stop` | `update_completion_sigkill` |

Viewer = `viewer_optional` (app session or embed session); policy = `dashboard_chat.open_chat` (published, readable, member, chat on), re-applied on every message. Rate limits: 6 messages/min per viewer per artifact, 60/min per organization, 240 reads/min. Not built yet: re-attach to a running answer (`watch`), MCP approvals and clarify responses (§10.2).

**Why not let the broker call `/api/reports` directly:** generated code would get the viewer's whole API (every report, settings, sharing), policy would live in the browser, and embed sessions would have to be accepted by the main API. Server-side runtime routes keep all three closed.

### 5.4 SDK surface (`artifact-sdk.js`, broker in `useArtifactRuntime.ts`)

```js
await bow.conversations.policy()                          // {available, scope, agents}
const c = await bow.conversations.create({ title, agents })
for await (const ev of bow.conversations.send(c.id, text, { signal })) { /* started|tool|text_delta|text|completed */ }
await bow.conversations.list({ limit: 20 })               // past conversations
await bow.conversations.messages(c.id, { before })        // transcript
await bow.conversations.rename(c.id, title); await bow.conversations.archive(c.id)
await bow.conversations.stop(c.id, messageId)
bow.markdown(text)                                        // agent answers → safe React elements (no HTML)
```

Fixture mode keeps conversations in the frame and answers with a fixed text, like `bow.ai`. The page's own chat bubble hides when the artifact uses `bow.conversations` (it is the chat).

### 5.5 Authoring

- Document the module in the artifact authoring context and `create_artifact`/`edit_artifact` tool guidance; the agent sets the policy through the existing dashboard settings path, never in code.
- A "Chat assistant" starter (the stock experience: thread list, transcript, composer, approvals) that users can ask the agent to restyle.
- Keep `bow.ai.stream` for single-shot generation (summaries, rewrites); point chat-like requests at `bow.conversations`.

## 6. Embedding an artifact

### 6.1 Data model

- `host_applications`: organization, public id, name, enabled, allowed origins, identity mode, mode config (issuer, public key + fingerprint, identity source, create-users flag, linked OAuth client for exchange), epoch, created by. Secrets never stored (private key shown once; exchange uses the existing OAuth client secret hash).
- `artifact_host_applications (artifact_id, host_application_id)` — the artifact side of the opt-in (queryable both ways, survives versions).
- `oauth_mcp_access_tokens.host_application_id` (nullable FK) for embed sessions.
- `embed_assertion_replays (host_application_id, jti, expires_at)`.

### 6.2 Serving the artifact

- `/d/{ref}?host={hostAppId}` returns the SPA with `frame-ancestors 'self' <that host's origins>` **only if** the artifact allows that enabled host; otherwise the normal site policy. Implemented twice, as the prototype proved necessary: FastAPI route when `SERVE_FRONTEND=1`, Nitro middleware for the Nuxt server.
- `SharedDashboardView` + `useArtifactRuntime` gain an "embedded" credential: when `host` is present they request a session through the SDK handshake and send `Authorization: Bearer bow_embed_…` instead of the app token. Everything else (rendering, data, runtime broker) is unchanged.
- `/api/d/{ref}/*` read routes and `/api/artifacts/{id}/runtime/*` accept an embed session for artifacts that allow its host application; viewer = the session's user, subject to the artifact's sharing.

### 6.3 SDK (`/api/embed/sdk/v1/bow-embed.js`)

```js
BOWEmbed.mount({
  baseUrl: 'https://bow.internal.example',
  hostApp: 'host_…',
  artifact: 'b51c4d89-…',             // dashboard / data app / chat assistant
  mode: 'floating' | 'inline' | 'page',
  container: '#assistant',            // inline
  identity: { getAssertion }          // §4.1 — or { exchangeToken } §4.2, or { sso: 'popup' } §4.3
  context: { recordId: 'INC-1042' },  // → bow.context.host inside the artifact
  theme, locale,
})  // .open() .close() .updateContext() .logout() .destroy(); events: ready, authenticated, authRequired, error
```

Host context reaches artifact code through `bow.context`; the artifact decides whether to pass it into a conversation turn.

## 7. UI

- **Settings → Integrations → Host applications** (next to OAuth apps): list, create, identity mode and its fields, key generation/rotation (private key shown once), allowed origins, enable/disable, sign everyone out, active sessions, the artifacts that allow it, audit link.
- **Artifact Settings window → Embed tab** (`components/dashboard/DashboardSettingsModal.vue`): "Embeddable in" picker (admins can create a host application inline), snippets for floating / inline / full page, backend examples per identity mode (Python, Node, Java, C#; the exchange call for 4.2; nothing for 4.3), a preview signed in as yourself, and the frame URL for platforms that only accept an iframe URL.
- **Viewers tab**: the conversation policy (§5.1) — already there for dashboard chat, extended with tool switches.
- Removed: the "Embedded chat" settings tab and page (already gone from this branch).

## 8. Security requirements

- Assertions: RS256 only, `exp − iat` ≤ 2 minutes, ≤ 60 s skew, `jti` single use, `aud` = host application; typed failures that never echo the token.
- Rate limits on session creation (`admit`, per host application and per source IP) and on conversation turns (§5.3).
- No token in URLs or storage; postMessage with exact origin + nonce both ways; the only `*` message is the credential-free hello.
- Frame policy per host application; unknown or disallowed → `frame-ancestors 'none'`/site default.
- JIT only through the identity source's existing admission rules; never email linking; LDAP JIT verifies the subject in AD.
- Customer obligations, stated in the Embed tab and docs: the assertion endpoint uses the host's own session (never a request parameter), POST + same-site/CSRF protection, key in a secret store.
- Run the repo's `security-review` pass on the handshake, framing, session acceptance and runtime routes before release.

## 9. Delivery phases

| Phase | Scope | Exit gate |
| --- | --- | --- |
| 1. Host applications + host assertion | Model, migration, Integrations UI, assertion → session for all identity sources (no JIT yet), rate limits, audit | e2e: matrix of bad assertions; each identity source resolves the right user; sessions refused by the main API; revocation paths |
| 2. Embeddable artifacts | Artifact opt-in, `/d/{ref}?host=` framing (FastAPI + Nitro), embedded credential in `SharedDashboardView`/runtime broker, SDK `mount`, Embed tab with snippets | Playwright: separate-origin host page frames a dashboard and a records app as two different users; unlisted origin cannot frame; artifact not allowing the host → refused |
| 3. Conversations in the SDK | Multi-conversation `artifact_chat`, runtime routes, SDK module + broker, policy extension, authoring guidance, "Chat assistant" starter | e2e: roster ⊆ policy ∩ access with multi-agent reports; cross-viewer/cross-artifact isolation; stop/approve/watch; Playwright: ERP host → chat assistant artifact → MCP call receives the viewer's identity |
| 4. JIT + token exchange + SSO popup | Create-on-first-sign-in per source (OIDC/SAML/LDAP), generalized OIDC token exchange, popup handoff | e2e per source with a mock IdP (`tests/mocks/mock_oauth_provider.py`, `saml_idp.py`, LDAP fixture); duplicate concurrent first sign-ins create one user |
| 5. Hardening + docs | Security review, install docs per identity mode, release notes | Review findings resolved; docs reproduce an install from zero |

Every phase runs on SQLite and PostgreSQL, keeps the existing `/d/` sharing and dashboard-chat suites green, and captures UI evidence (en + he).

## 10. Open decisions

1. **Conversation scope for "past reports".** Recommended: the viewer's conversations created through *this artifact* only. Alternative: also list the viewer's regular BOW reports read-only — more useful, but generated code then sees every conversation the user ever had.
2. **MCP tools in artifact conversations.** `ARTIFACT_CHAT_TOOL_ALLOWLIST` is read/query only and excludes MCP. ERP assistants need MCP; recommended: allow MCP tools for conversations whose policy names MCP agents, with existing per-tool `ask` approvals surfaced through `bow.conversations.respond`.
3. **Public artifacts.** Recommended: embeddable for viewing only; conversations always require a signed-in member.
4. **Default role for JIT users** and whether a host application can narrow it further.
5. **Per-viewer limits and cost**: conversation turns per hour per viewer and per organization; budget alerts before opening embedding to every employee.

## 11. Prototype history (what was ported, what not)

The removed prototype (`git show 544eb0c`) is the reference for:

- `backend/app/services/embed_service.py` — RS256 decoding and claim checks, `jti` replay table, session issue/resolve with epoch checks, keypair generation (port into host applications; drop runtime/branding/thread code).
- `backend/app/routes/embed.py` — SDK serving with ETag, frame-policy route for `SERVE_FRONTEND=1`.
- `frontend/server/middleware/embed-frame.ts` — Nitro frame policy.
- `frontend/composables/useEmbedHost.ts`, `useEmbedSession.ts` — handshake and in-memory session (fold into the shared-dashboard view).
- `backend/app/static/embed/bow-embed.js` — SDK lifecycle and sizing.
- `backend/tests/e2e/test_embedded_chat.py`, `tests/unit/test_embed_service.py` — the assertion matrix and session tests.
- `tools/agent/embed/erp_host.py`, `stub_llm.py` — the separate-origin ERP host and scripted LLM for the browser loop.

Not ported: the `embeds` table and its runtime/branding, the `embed_chat` report type, the dedicated chat document and panel, the Settings → Embedded chat page.

One fix from the prototype stays on this branch: per-block `cache-key` on `<MDC>` in `ArtifactChatBubble.vue` (two streamed answers starting with the same chunk rendered the same text).

## 12. As built (revision 2026-10-29) and what is left

### Decisions taken on §10
1. Past conversations are scoped to **this artifact** (the viewer's own).
2. MCP tools: **not yet** — conversations keep the artifact-chat tool ceiling (read/query). Approvals through the SDK are the next step (left below).
3. Public artifacts: embeddable for viewing; conversations need a signed-in member.
4. JIT users get the organization's **default member role**.
5. Rate limits per viewer and per organization (§5.3); session creation 30/min per host + address, 600/min per host.

### Deviations from the plan
- **Admin capability for the Embed tab** follows the dashboard's own Settings gate (`dashboard_for(..., CAP_SETTINGS)`): an organization admin who does not edit a dashboard cannot embed it (same as the rest of its Settings window).
- **SSO mode** is handled inside the frame: the user clicks *Sign in* in the embedded page, BOW's own sign-in runs in a popup on the BOW origin and posts a one-time PKCE-bound code back to the frame (`pages/embed-signin.vue`). The host page configures nothing.
- **Host context** reaches artifact code as `ARTIFACT_DATA.host_context` (updated on `embed.updateContext`), not as `bow.context.host`.
- **"Made with BOW" badge** is not shown inside a host application (it floated over the customer's controls — found in the live loop).
- **`bow.markdown`** added: agent answers are Markdown and the sandbox has no renderer; the live loop showed raw tables.
- Embed sessions also return the viewer payload (`viewer_context`) so the artifact's `current_user` is the host's user.

### Where it lives
- Backend: `models/host_application.py`, migration `hostapp01`, `services/host_application_service.py` (keys, assertions, IdP tokens, SSO codes, sessions), `services/host_identity.py` (identity sources + JIT), `core/embed_auth.py` (`viewer_optional`), `routes/host_applications.py`, `routes/embed.py` (SDK, bootstrap, session, frame policy), `routes/artifact_conversations.py`, `static/embed/bow-embed.js`.
- Frontend: `components/embed/HostApplication{Form,sPanel}.vue`, `components/dashboard/{DashboardEmbedPanel,EmbeddedArtifactGate}.vue`, `composables/{useEmbedCredential,useEmbedHost}.ts`, `pages/embed-signin.vue`, `server/middleware/embed-frame.ts`, `public/libs/artifact-sdk.js`.
- Tests: `tests/e2e/dashboards/test_embed_host_applications.py` (39), `test_artifact_conversations.py` (6).
- Live loop: `tools/agent/embed/` (stand-in back office on its own origin, admin and employee Playwright flows).

### Verified live (real Claude Haiku 5.5, unbiased prompts)
- An analyst asked BOW chat for an account-manager dashboard; published; embedded in the back office; Alice and Bob saw it signed in silently, Hebrew rendered RTL, an unmapped contractor got a clear refusal, the frame policy named only the back-office origin.
- An analyst asked for "a sales assistant … with previous conversations on the side"; BOW chose `bow.conversations` unprompted. Two bugs surfaced in the generated app (a tool object rendered as a React child; a new conversation's messages overwritten by a reload) and were fixed by reporting them to BOW in plain words; the SDK reference was made explicit about the `tools` shape as a result.

### Left
- Phase 4: e2e for IdP-token mode (mocked provider keys) and JIT per source (OIDC, SAML, LDAP), concurrent first sign-in; SSO popup browser loop.
- MCP tools and approvals in conversations (§10.2); `watch` re-attach.
- Regression suites (dashboard chat, LDAP, artifact chat), security review, user docs, release notes.

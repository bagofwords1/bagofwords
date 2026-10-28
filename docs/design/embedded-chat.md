# Embedded chat (V1): BOW chat inside a customer application

Status: implemented on `ai/exciting-goodall-m7toth`; verified in the sandbox
loop described in `docs/feedback-loops/embedded-chat.md`. Updated 2026-10-28.

Embedded chat delivers the BOW assistant inside an application the customer
already runs (an ERP, ServiceNow, an internal portal) through a script that
this BOW instance serves. Employees use a compact chat with their **existing
BOW identity and permissions**; nothing about the chat is a generated artifact
and nothing depends on a CDN or on BOW cloud.

## 1. What ships in V1

| Area | V1 |
| --- | --- |
| Configuration | Settings → Embedded chat: one page per embed, direct save with optimistic concurrency, enable/disable |
| Runtime | Allowed agents (explicit roster), default model, reasoning effort, public tool switches, follow-ups, optional agent picker |
| Authentication | Signed host assertion (RS256, BOW-generated keypair) mapped to an existing member through an admin-set member attribute |
| Attributes | Host context (LLM-visible, bounded), request metadata (MCP dynamic params), identity forwarding through the existing MCP mappings |
| UI | Shared compact chat (also used by the artifact-page bubble); floating launcher and inline container; approvals for `ask`-policy tools; reconnect after reload |
| Branding | Name, accent color, light/dark/auto theme, greeting, suggested prompts, launcher label, locale, local logo |
| SDK | `/api/embed/sdk/v1/bow-embed.js`: `mount`, `open`, `close`, `updateContext`, `updateMetadata`, `setLocale`, `logout`, `destroy`, events |
| History | One private thread per (embed, user), persisted, restored on reload |
| Deployment | Every asset served by BOW; no runtime network dependency beyond the BOW origin |
| Operations | Counters (mapped members, live sessions, conversations), key rotation, sign-everyone-out, delete; audit events for every change and sign-in |

Deferred (unchanged from the plan): embed SSO / Entra exchange, new OBO
provisioning, JIT or consumer identities, customer-supplied public keys or
JWKS, overlapping keys, draft/publish/rollback, per-MCP-tool policies, a
mapping builder, runtime request overrides, model selection by the employee,
multiple conversations, custom CSS, rich artifact output, scheduling, hard
budget reservations, historical SDK versions.

## 2. Pieces and where they live

```
backend/app/models/embed.py                 Embed, EmbedAssertionReplay
backend/alembic/versions/embed01_*.py       tables + reports.embed_id + oauth token embed_id
backend/app/schemas/embed_schema.py         admin/public/session/chat payloads + validation
backend/app/services/embed_service.py       config, assertions, sessions, threads, enforcement glue
backend/app/routes/embed_admin.py           /api/embeds/*           (manage_settings)
backend/app/routes/embed.py                 /api/embed/*            (public, assertion, embed session)
backend/app/static/embed/bow-embed.js       the SDK (served by routes/embed.py)
frontend/pages/embed/[id].vue               the chat document the SDK frames
frontend/components/embed/EmbedChat.vue     launcher / panel / auth states / composer
frontend/components/chat/*                  CompactChatThread, CompactChatComposer (shared with ArtifactChatBubble)
frontend/composables/useCompactChat.ts      SSE parsing + event folding + tool labels (shared)
frontend/composables/useEmbedSession.ts     bootstrap, token in memory, renewal, authed fetch
frontend/composables/useEmbedHost.ts        postMessage handshake (iframe side)
frontend/server/middleware/embed-frame.ts   per-embed frame-ancestors when Nuxt serves the SPA
frontend/pages/settings/embeds.vue          Settings → Embedded chat
```

## 3. Trust model

* **The embed row is the only authority.** `allowed_origins` decides who may
  frame the chat (CSP `frame-ancestors` on the document, origin + source +
  nonce checks on every postMessage). `issuer` + `public_key` decide who may
  sign employees in. `subject_attribute` decides which member an asserted
  employee is. Nothing the browser or the host backend sends can widen any of
  that.
* **Assertions are RS256 only.** BOW generates the keypair, shows the private
  key once (create / rotate) and stores only the public key: a database leak
  yields nothing that can sign anyone in. Required claims: `iss`, `aud` (=
  embed id), `sub`, `iat`, `exp` (≤ 10 min after `iat`), `jti` (single use,
  tracked in `embed_assertion_replays`). Clock skew ≤ 60 s. Algorithm
  confusion and `alg: none` are refused by the fixed allowlist.
* **Identity = admin-set member attribute.** `sub` must equal exactly one
  member's `Membership.custom_attributes[subject_attribute]` in the embed's
  organization. Zero matches → `embed.identity_unmapped`; two → 
  `embed.identity_ambiguous`. Email, name and IdP-synced attributes are never
  consulted; nothing is provisioned; claims never grant roles.
* **Sessions are their own credential class.** `bow_embed_` tokens live in
  `oauth_mcp_access_tokens` (scope `embed`, `embed_id` set). They are accepted
  only by `current_embed_session` on `/api/embed/{id}/…` and must match the
  embed in the path. Every request re-checks expiry, embed enabled, the
  embed epoch, the user's `session_epoch`, the user being active and the
  membership. Logout revokes server-side. Disable, key rotation and
  "sign everyone out" bump the epoch and end every session at once. The
  token is held in iframe memory only (never URL, never storage).
* **The thread carries the policy.** Each (embed, user) has one hidden report
  (`report_type='embed_chat'`, `embed_id`), unique by database index. Before
  every turn the embed's runtime is written onto it: roster = admin list ∩
  the employee's own accessible live agents (an empty admin list stays
  empty — `NON_AUTO_REPORT_TYPES`), model, effort, forced-off tools +
  admin switches, follow-ups. The existing agent, tool and credential
  enforcement then applies unchanged; a settings change needs no host
  redeploy.
* **Host context is a hint, not authority.** `context` is rendered into the
  prompt as `<embed_context>` (bounded to 4 KB) and explicitly framed as
  task data; a record id grants nothing. `metadata` is stored with the turn
  and reaches MCP connections only through their dynamic-params allowlist;
  locked identity fields always win.
* **Compact-chat-only tools.** Tools whose interaction the compact surface
  cannot complete (`clarify`, artifacts, docs, scheduling, wait, lists,
  image generation, delegate, notify, send_email) are always off inside an
  embed; the admin can only switch more off.

## 4. The browser boundary

```
Host page (customer origin)           BOW origin
┌──────────────────────────┐          ┌──────────────────────────────────┐
│ <script bow-embed.js>    │  load    │ GET /api/embed/sdk/v1/bow-embed.js│
│ BOWEmbed.mount({...})    │─────────▶│                                  │
│  └ <iframe src=/embed/id>│  frame   │ GET /embed/{id}  (CSP: frame-    │
│     ▲ postMessage        │◀────────▶│   ancestors 'self' + origins)    │
│     │ init(nonce,ctx)    │          │  └ bootstrap → appearance only   │
│     │ auth-request       │          │  └ session: assertion → token    │
│ getAssertion() ─▶ host   │          │  └ chat: send / watch / cancel / │
│   backend signs RS256    │          │     approvals, all bound to the  │
│     │ assertion          │          │     caller's own thread          │
└──────────────────────────┘          └──────────────────────────────────┘
```

The SDK is a UI shim: it creates and sizes the iframe, relays context,
metadata, locale and theme, asks the host for an assertion when the frame
needs one, and surfaces lifecycle events (`ready`, `open`, `close`,
`authenticated`, `authRequired`, `error`, `loggedOut`) without transcript
text. Both sides accept messages only from the window they created or were
created by, from the expected origin, carrying the nonce the SDK generated.

## 5. Air-gap notes

The SDK, the chat document, styles, icons, fonts, markdown rendering and
every lazy chunk come from the BOW origin; the embed makes no other network
requests (the loop records every host the browser contacted). Assertions are
verified with the stored public key, so no IdP round trip is needed at sign-in.
Models and MCP/data endpoints are whatever the organization configured; an
isolated deployment uses internal ones. This covers the embedded-chat surface,
not a claim about every BOW feature.

## 6. Operating it

1. Settings → Embedded chat → New embed: name, allowed origins, issuer, member
   attribute key. Copy or download the private key (shown once).
2. Set the attribute on each employee (Access → member → Attributes, or the
   CSV import with an `attr.<key>` column). The Authentication tab lists who
   is mapped.
3. Agents & model: pick the agents the embed may use and the model; switch off
   tools you do not want. Appearance: name, color, theme, greeting, prompts,
   logo, language.
4. Install: add the page snippet and the backend assertion endpoint (Python and
   Node examples are generated with the embed's own values).
5. Later: Preview (as yourself), rotate the key, sign everyone out, disable,
   delete. Everything is audited (`embed.*` actions).

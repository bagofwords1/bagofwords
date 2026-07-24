# Google Chat Integration — Investigation

Status: investigation only, no implementation.

Goal: let users talk to the analyst from Google Chat (DMs and space
@mentions), the same way they already can from Slack and Microsoft Teams.

This doc maps the existing chat-platform architecture, explains how the
Google Chat app model fits it, and lists every touchpoint a future
implementation needs to change. The short version: **Google Chat slots into
the existing `PlatformAdapter` pattern cleanly — closest in shape to a
Slack/Teams hybrid — and needs no schema migration.**

---

## 1. How the existing chat integrations work

One org-scoped row per platform in `external_platforms`
(`app/models/external_platform.py`): `platform_type` (free string),
`platform_config` (JSON), `credentials` (Fernet-encrypted JSON). No enum at
the DB layer, so a new platform type is not a migration.

The message lifecycle, shared by Slack / Teams / WhatsApp / email:

1. **Webhook route** (`app/routes/{slack,teams,whatsapp}_webhook.py`,
   registered in `backend/main.py`, allow-listed as an API path in
   `app/core/spa.py`): parses the event, resolves the `ExternalPlatform`
   row (by team_id / tenant_id / phone id), verifies the request signature
   via the adapter, dedupes by event id (in-memory set), filters bot echoes
   and non-message events, then calls
   `ExternalPlatformManager.handle_incoming_message(db, platform_type, org_id, event)`.
2. **Adapter** (`app/services/platform_adapters/*_adapter.py`, registered in
   `adapter_factory.py`): implements the `PlatformAdapter` ABC
   (`process_incoming_message`, `send_response`, `get_user_info`,
   `verify_webhook_signature`, `send_verification_message`) plus the
   de-facto extended interface every adapter also implements:
   `add_reaction` / `remove_reaction`, `send_dm`, `send_dm_in_thread`,
   `send_file_in_dm` / `send_file_in_thread`, `send_image_in_dm`.
   `process_incoming_message` normalizes the platform event into a dict:
   `external_user_id`, `channel_id`, `channel_type` (`im`/`personal` vs
   `channel`), `message_text`, `thread_ts`, `message_ts`,
   `is_thread_reply`, plus platform extras (Teams keeps `service_url`,
   `bot_id`).
3. **Manager** (`app/services/external_platform_manager.py`):
   - **Identity**: looks up `ExternalUserMapping`; if missing, either
     auto-links by workspace email (`auto_link_by_email` config +
     `adapter.get_user_info()` + optional auto-provisioning — allowed for
     `("slack", "teams")` because the workspace IdP vouches for the email)
     or creates an unverified mapping and DMs a verification link
     (`send_verification_message` → `/settings/integrations/verify/<token>`
     page, which is already platform-generic).
   - **Report/session routing**: Slack maps a thread to a report via
     `Completion.external_thread_ts`; Teams 1:1 and WhatsApp (no usable
     threading) reuse the user's most recent report within an
     org-configurable window (`{platform}_session_max_age_hours` in
     `organization_settings_schema.py`; Teams 120h, WhatsApp 24h) and honor
     a lone `"new"` command (`NEW_REPORT_COMMAND_PLATFORMS`).
   - **Data-source scoping**: channel mentions get only public data
     sources; DMs get the user's accessible set. Reused reports get their
     sources re-synced per message. Per-agent gating comes from
     `DataSource.channel_availability` (free-form `{channel: bool}` JSON —
     no backend enum to extend).
   - Creates a background `Completion` carrying
     `external_platform/user_id/thread_ts/message_ts/channel_id/channel_type`.
4. **Outbound** (event-listener driven, not request/response):
   `app/models/completion_block.py` listens for terminal completion blocks
   and sends decision/final text via `adapter.send_dm_in_thread(...)`;
   `app/services/slack_notification_service.py` sends step results (chart →
   matplotlib PNG upload, table → CSV upload, count → text) with per-platform
   branches. Reactions (👀 while processing, ✅ when done) are swapped via
   `add_reaction`/`remove_reaction` — Teams stubs these as no-ops.
   Both files gate on hardcoded platform allowlists:
   `external_platform in ('slack', 'teams', 'whatsapp')`.
5. **Prompting**: `app/ai/agents/planner/prompt_builder_v3.py`
   `_platform_system_directives()` injects per-platform formatting rules
   (Slack mrkdwn, Teams markdown-no-inline-charts, WhatsApp plain text).
6. **Admin UI**: `frontend/pages/settings/integrations/index.vue` renders a
   card per platform with a `<X>IntegrationModal.vue` (setup instructions +
   credential form → `POST /api/settings/integrations/<platform>`), backed by
   `SlackConfig`/`TeamsConfig`/... Pydantic schemas in
   `external_platform_schema.py` and `create_<platform>_platform` +
   `_test_<platform>_connection` in `external_platform_service.py`.

## 2. How Google Chat apps work (and how they map)

Google Chat "apps" are the bot equivalent. Setup is done in a Google Cloud
project: enable the **Google Chat API**, configure the app on its
"Configuration" page (name, avatar, "receive 1:1 messages", "join spaces"),
choose **HTTP endpoint** as the connection type, and set visibility to the
Workspace domain or specific users. Internal apps don't need Marketplace
publishing. A **service account** in the same project provides outbound
credentials (JSON key).

Mapping to our lifecycle:

| Concern | Google Chat mechanism | Closest existing precedent |
|---|---|---|
| Inbound transport | Google POSTs JSON events (`MESSAGE`, `ADDED_TO_SPACE`, …) to our HTTPS endpoint; sync response optional, empty response OK (we reply async) | Teams (Bot Framework POSTs activities) |
| Inbound auth | `Authorization: Bearer <JWT>` signed by `chat@system.gserviceaccount.com`; verify against Google's public certs, `aud` = the app's **Cloud project number** | Teams JWT verification (JWKS fetch + issuer/audience checks) — near copy-paste |
| When events fire | DMs: every message. Spaces: only when the app is @mentioned | Slack `message` (im) vs `app_mention` split |
| Mention stripping | Event provides `message.argumentText` with the leading @mention already stripped | Teams strips `<at>` tags with a regex — not needed here |
| Outbound | REST `POST https://chat.googleapis.com/v1/spaces/{space}/messages` with an OAuth token minted from the service-account JSON, scope `https://www.googleapis.com/auth/chat.bot` (no user impersonation) | Teams Bot Connector POST with client-credentials token. Token minting from SA JSON already exists in `app/services/email/oauth.py` (Google DWD path — same library, drop the `subject`) |
| Threading | Every message carries `message.thread.name`; reply by setting `thread.name` + `messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` | Slack `thread_ts` |
| DM vs channel | `message.space.spaceType`: `DIRECT_MESSAGE` vs `SPACE`; replies always target the space the message arrived from (`space.name`, e.g. `spaces/AAAA…`) | Teams (always reply by conversation id) |
| User identity | `message.sender` carries `name` (`users/<id>`), `displayName`, and — for Workspace users in-domain — `email` directly in the event | Better than Slack/Teams (no extra API call for auto-link) |
| Text formatting | Slack-like markup: `*bold*`, `_italic_`, `~strike~`, backticks, `<url|text>` | Slack mrkdwn directives nearly reusable |
| Reactions | Creating reactions requires **user** auth; app auth can't do 👀/✅ | Teams (no-op `add_reaction`/`remove_reaction`) |
| File/chart upload | `POST https://chat.googleapis.com/upload/v1/{space}/attachments:upload` then attach to a message — documented for app auth (**verify during implementation**); fallback: Teams-style "chart available in the web report" link | Slack file upload / Teams fallback |

### Routing the webhook to the right org

Slack routes by `team_id`, Teams by `tenant_id`. The Google Chat event
doesn't carry a Workspace/customer id at the top level in all cases, but
verified JWT `aud` = project number, and each org's integration has its own
Cloud project. So: **route by the `aud` claim (project number) matched
against `platform_config.project_number`**, mirroring
`find_platform_by_tenant_id`. (Space/user `name`s are also stable ids if a
secondary key is ever needed.)

### Session model decision

- **Spaces**: thread-per-report, exactly like Slack —
  `thread_ts = message.thread.name`, look up reports via
  `Completion.external_thread_ts`.
- **DMs**: Google Chat DM threads exist (every message belongs to a thread),
  and replies land in-thread, so the Slack model *can* work in DMs too.
  Recommended: start with the Slack model for both (no session window, no
  `"new"` command, no new org setting). If DM threading proves awkward in
  practice (flat-rendered DMs), fall back to the Teams-personal pattern:
  `google_chat_session_max_age_hours` + add `"google_chat"` to
  `NEW_REPORT_COMMAND_PLATFORMS`.

### Connectivity: does Google need to reach our URL?

Only in HTTP-endpoint mode — and Google Chat is the only chat platform we
could support **without any inbound connectivity at all**, which matters for
enterprise/self-hosted deployments behind a firewall.

Google Chat apps support two connection modes, chosen on the app's
configuration page:

1. **HTTP endpoint** (assumed above): Google POSTs events to our public
   HTTPS URL. Same exposure as today's Slack/Teams/WhatsApp webhooks — a
   publicly reachable endpoint, authenticated by verifying the
   Google-signed JWT. Note that Google publishes no narrow source-IP range
   for these calls, so IP allowlisting is impractical; the JWT check is the
   real gate (as with Teams).
2. **Cloud Pub/Sub**: Google publishes the same events to a Pub/Sub topic
   in the customer's own GCP project, and our backend **pulls** them over
   an outbound connection to `pubsub.googleapis.com:443` using the same
   service account (granted `roles/pubsub.subscriber` on the
   subscription). No inbound port, no public URL, no webhook route.
   Replies still go out through the Chat REST API as normal. The only
   thing lost is the ability to respond synchronously in the HTTP
   response — which we never use anyway (all replies are async via the
   completion-block listeners).

The repo already has the structural precedent for mode 2: the email
channel's `EmailPoller` (`app/services/email_poller_service.py`) is a
background task started from `main.py` under the scheduler leader that
discovers active platforms and feeds inbound messages into the same
`ExternalPlatformManager.handle_incoming_message()` pipeline. A
`GoogleChatPubSubListener` would be a near-clone: iterate active
`google_chat` platforms configured with `connection_mode="pubsub"`, pull
messages (streaming pull or periodic `pull` calls), ack after handoff, and
dedupe by `message.name` (Pub/Sub is at-least-once delivery, so the dedupe
we already planned is mandatory here, not just nice-to-have).

Pub/Sub setup cost on the customer side: create a topic, grant
`chat-api-push@system.gserviceaccount.com` publish rights on it, create a
subscription for the app's service account, and select the topic in the
Chat app config. More GCP clicks than pasting a URL, but it's the standard
Google-documented path and it keeps the deployment fully egress-only.

Recommendation: implement the adapter/manager pipeline transport-agnostic
(both modes produce the identical event JSON) and let `connection_mode` in
the platform config pick HTTP webhook vs Pub/Sub listener. If enterprise
deployments are the driving use case, Pub/Sub-first is a defensible scope
cut — it also skips the JWT-verification code path entirely, since pulled
messages don't carry the bearer token and don't need it (the subscription
itself is the trust boundary).

### Config & credentials

```python
class GoogleChatConfig(BaseModel):
    project_number: str            # JWT audience check + org routing
    service_account_json: Any      # JSON key (str or dict) — encrypted at rest
    auto_link_by_email: bool = True
    connection_mode: str = "http"  # "http" | "pubsub"
    pubsub_subscription: Optional[str] = None  # projects/x/subscriptions/y
```

`platform_config` (plaintext JSON): `project_number`, `auto_link_by_email`,
maybe the SA client_email for display. `credentials` (encrypted): the full
service-account JSON — same treatment as the email integration's
`google_service_account_info`.

Connection test (`_test_google_chat_connection`): mint a `chat.bot`-scoped
token from the SA JSON (proves the key is valid and the API is enabled).

### Auto-link by email

`sender.email` arrives in the event itself, so the adapter should stash the
sender's email/display-name from the last processed event (or the manager
can read them from `processed_data`) and serve them from `get_user_info()`.
Add `"google_chat"` to the manager's `allow_auto_provision` tuple —
Workspace vouches for the email exactly like Slack/Teams IdPs. The
verify-link fallback works unchanged (the verify page defaults its label
per `platform_type`; add a label/icon).

## 3. Implementation checklist

Backend — new files:
- `app/services/platform_adapters/google_chat_adapter.py` — the adapter.
  Teams adapter is the best template (token cache, JWT verify with cert
  cache, REST send); Slack's `process_incoming_message` shape for threading.
- `app/routes/google_chat_webhook.py` — `POST
  /api/settings/integrations/google_chat/webhook`: parse event, handle only
  `MESSAGE` (return `{}` for `ADDED_TO_SPACE` etc.), verify JWT, route by
  `aud`/project number, dedupe by `message.name`, delegate to the manager.
  (HTTP mode only — skippable if Pub/Sub-first is chosen.)
- `app/services/google_chat_listener_service.py` — Pub/Sub pull listener
  modeled on `email_poller_service.py`, started from `main.py` under the
  scheduler leader (Pub/Sub mode only).
- Tests mirroring `tests/unit/test_whatsapp_adapter.py` /
  `test_whatsapp_webhook_route.py` (event normalization, JWT verify, org
  routing, thread mapping), plus `test_channel_availability.py` additions.

Backend — single-line/small edits (the platform-enumeration points):
- `adapter_factory.py`: register `"google_chat"`.
- `backend/main.py`: import + `include_router`.
- `app/core/spa.py`: add `"google_chat_webhook"`-equivalent path prefix to
  the API allow-list (whatever the route module/prefix ends up being).
- `external_platform_schema.py`: `PlatformType.GOOGLE_CHAT`,
  `GoogleChatConfig`.
- `app/routes/external_platform.py`: `POST /settings/integrations/google_chat`
  (+ audit log entry).
- `external_platform_service.py`: `create_google_chat_platform`,
  `_test_google_chat_connection` (+ dispatch in `test_platform_connection`).
- `external_platform_manager.py`: add `"google_chat"` to
  `allow_auto_provision` and to the auto-link `get_user_info` branch; report
  link formatting (Google Chat uses `<url|text>` like Slack, so the existing
  Slack else-branch already fits).
- `app/models/completion_block.py`: add `'google_chat'` to both
  `external_platform in ('slack', 'teams', 'whatsapp')` allowlists;
  response-channel rule = Teams-style (always reply to `space.name`).
- `slack_notification_service.py`: add to the platform allowlist; decide
  chart path (attachment upload if app-auth upload verified, else Teams-style
  text fallback) and table path (CSV upload or skip like Teams).
- `prompt_builder_v3.py`: `google_chat` platform directives (clone the Slack
  block: brief, Slack-like markup, `<url|label>` links, prefer charts only
  if upload is supported — otherwise clone the Teams "describe in text"
  rules).
- `organization_settings_schema.py`: only if the Teams-style DM session
  window is chosen (`google_chat_session_max_age_hours`).
- No Alembic migration needed: `platform_type` is a free string,
  `Completion.external_platform` is a free string,
  `channel_availability` is a free JSON map.

Frontend:
- `components/GoogleChatIntegrationModal.vue` — setup walkthrough (create
  GCP project → enable Chat API → service account key → app configuration
  page with our webhook URL → visibility) + form (project number, SA JSON
  paste, auto-link toggle). `TeamsIntegrationModal.vue` is the template.
- `pages/settings/integrations/index.vue` — card entry + state refs.
- `public/icons/google_chat.png` (referenced by the card and the verify
  page; note `frontend/public/icons/` vs wherever slack.png/teams.png
  actually live — the verify page uses `/icons/teams.png`).
- `pages/settings/integrations/verify/[verify_code].vue` — label + icon for
  `google_chat`.
- Channel-availability / instruction-channel pickers
  (`AgentSettingsPanel.vue`, instruction `applicable_channels`
  UI) — add the `google_chat` option.
- `locales/*.json` — strings.

Docs: user-facing setup guide alongside the existing integration docs.

## 4. Risks / things to verify during implementation

1. **Attachment upload under app auth** (`attachments:upload` with
   `chat.bot` scope) — determines whether charts/CSVs go inline (Slack
   parity) or via web-report links (Teams parity). Design the notification
   branch so either works.
2. **`sender.email` availability** — populated for in-domain Workspace
   users, but can be absent for external users or under some admin
   settings. The existing "couldn't read your workspace email → ask your
   admin" block message path already covers the miss case.
3. **DM threading UX** — verify that thread-scoped replies read naturally
   in flat DM spaces before committing to the Slack session model for DMs
   (fallback documented above).
4. **Event dedupe/retries** — Google retries on non-2xx/slow responses;
   the in-memory `processed_events` set pattern (keyed on `message.name`)
   matches what Slack/Teams routes already do. Same known limitation:
   per-process memory, resets on restart.
5. **Rate limits** — Chat API write limits are per-space per-minute
   (~60/min); the per-block send pattern can emit several messages per
   completion. Same class of risk as Slack/Teams today; no new mitigation
   required initially.

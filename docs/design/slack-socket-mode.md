# Slack Socket Mode — Design

Status: **implemented** (2026-07) — Socket Mode is the default
``connection_mode`` for new Slack setups; Events API remains for existing
installs. The implementation also enables Slack's **Agent experience**
(Agents & AI Apps): ``assistant_thread_started`` pushes suggested prompts
sourced from the org's agents' ``conversation_starters``, and messages in
assistant threads show the native ``assistant.threads.setStatus``
"is thinking…" line (auto-cleared when the reply posts). Requires the app's
*Agent experience* toggle, the ``assistant:write`` scope, and the two
assistant event subscriptions. Shared inbound logic lives in
``app/services/slack_event_service.py`` (used by both transports); the
socket client/discovery loop in ``app/services/slack_socket_service.py``;
webhook-mode request-signature verification is now enforced when a signing
secret is configured. The
per-platform outbound-only picture: Google Chat has Pub/Sub, Teams and
WhatsApp have nothing, and Slack has **Socket Mode** — an official,
bot-initiated WebSocket transport that removes the need for any public
inbound URL. This doc scopes what adopting it would take.

## Why it's cheap here

Slack designed Socket Mode as a drop-in transport swap: the client opens a
WebSocket (`apps.connections.open` → ephemeral `wss://` URL), and Slack
pushes **envelopes whose `payload` is byte-for-byte the same
`event_callback` JSON the Events API webhook receives**. Everything after
parsing — bot filtering, dedupe, DM/mention gating, platform lookup,
`ExternalPlatformManager.handle_incoming_message` — is transport-agnostic
already. Outbound is untouched: `SlackAdapter` posts via the Web API over
outbound HTTPS in both modes.

Protocol obligations for the client:
- Authenticate `apps.connections.open` with an **app-level token**
  (`xapp-…`, scope `connections:write`) — a new credential alongside the
  existing bot token. The signing secret becomes unused in this mode
  (there is no inbound request to verify; the token-authenticated WSS
  connection is the trust boundary).
- **Ack every envelope** (`{"envelope_id": …}`) within ~3s — ack first,
  process after (we already hand off to a background completion, so this
  is natural).
- Handle `hello`, `disconnect` (Slack refreshes connections every few
  hours — reconnect with backoff to a fresh URL), and ping/pong.
- Slack permits up to 10 concurrent connections per app and delivers each
  event to one of them; we need exactly one, owned by the scheduler
  leader (same single-consumer rule as the email poller).

No new dependency: `uvicorn[standard]` already pulls in the `websockets`
package, which works as a client. (Alternative: adopt `slack_sdk`'s async
`SocketModeClient`; not worth a new dependency for a simple protocol.)

## Change list

Backend:
1. **Refactor** `app/routes/slack_webhook.py`: extract the post-parse body
   (bot filter, event gating, dedupe, team_id→platform lookup, manager
   handoff) into a shared `handle_slack_event(db, event_data)`; the route
   keeps only URL-verification challenge + signature concerns.
2. **New** `app/services/slack_socket_mode_service.py` — modeled on
   `email_poller_service.py`, started from `main.py` under the scheduler
   leader: discover active Slack platforms with `connection_mode ==
   "socket_mode"`, maintain one WebSocket each
   (`apps.connections.open` → connect → ack + feed envelopes'
   `payload` into `handle_slack_event`), auto-reconnect on `disconnect`
   / errors with backoff. Existing in-memory `event_id` dedupe carries
   over (Slack redelivers unacked envelopes, so dedupe matters more
   here).
3. **Schema/config**: `SlackConfig` gains
   `connection_mode: "events_api" | "socket_mode"` (default
   `events_api`) and optional `app_token` (encrypted with the other
   credentials; required iff socket mode). Connection test for socket
   mode: call `apps.connections.open` and verify a WSS URL comes back.
4. No DB migration (config/credentials are JSON), no adapter changes, no
   manager changes.

Frontend:
- `SlackIntegrationModal.vue`: mode selector. Events API path unchanged;
  Socket Mode path swaps "paste Request URL + signing secret" for
  "enable Socket Mode in the Slack app settings, generate an app-level
  token with `connections:write`, paste it here". Event subscriptions
  list is identical in both modes; Slack just stops requiring a Request
  URL.

Customer-side delta (socket mode): two clicks in their existing Slack app
(Settings → Socket Mode → enable; Basic Information → generate app-level
token). Everything else in the current setup guide stays.

## Caveats

- **Marketplace restriction**: Slack disallows Socket Mode for
  Marketplace-distributed apps. Irrelevant here — every org creates its
  own app — but worth a line in the setup modal in case that ever
  changes.
- **Single consumer**: the listener must run once across workers
  (scheduler leader), or two connections would each receive a share of
  events and split the dedupe set.
- **Liveness**: a dead WebSocket is silent (unlike a webhook, where Slack
  retries and surfaces failures in the app dashboard). The listener
  should log reconnect cycles and ideally surface last-connected state on
  the integration card, like the email channel's health display.

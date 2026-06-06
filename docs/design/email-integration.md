# Email Integration Design

Lets users **email the AI analyst and get answers back**, and lets the org use
the same mailbox as its outbound SMTP transport. Email is modeled as one more
platform behind the existing `PlatformAdapter` abstraction (alongside Slack /
Teams / WhatsApp), so the agent/completion pipeline is untouched.

## Mental model: one integration, two capability tiers

There is a single **Email** integration in `/settings/integrations` (a card like
Slack/Teams). The capability is *derived from which fields are filled in*:

```
Email integration  (one ExternalPlatform row, platform_type="email")
├── SMTP only            -> capability: SEND
│      • becomes the org's outbound mail transport (overrides global SMTP)
│      • NOT a conversational channel; the poller does not run
└── SMTP + IMAP          -> capability: SEND + RECEIVE
       • everything above, PLUS
       • the analyst becomes an email contact (inbound -> agent -> reply)
```

SMTP is the foundation (a channel can't reply without it); IMAP is the optional
upgrade that turns the integration into a channel. They are **not** separate
integrations — entering IMAP details flips `inbound_enabled` on the same row.

## Why IMAP/SMTP (and not Graph/Gmail API)

The deployment target is self-hosted and provider-agnostic. IMAP + SMTP are the
universal protocols every mail system speaks, so one adapter works against
Gmail/Workspace, Microsoft 365, and self-hosted servers alike. The transport is
kept separate from **auth** so the OAuth story slots in later without a rewrite:

```
EmailAdapter — IMAP read + SMTP send  (one codebase, all providers)
  auth_type:
    "password"   -> self-hosted / app-password mailboxes        (v1, shipped)
    "xoauth2"    -> Microsoft 365 / Google via OAuth token       (v2, roadmap)
```

`auth_type` is stored on the integration; v1 ships `password`. The `xoauth2`
paths are stubbed and clearly marked in `sender.py` / `mailbox_reader.py`.

## Inbound transport: polling (IMAP)

Email has no native webhook, so the leader worker polls each org's analyst
mailbox for unread mail and feeds it into the same
`ExternalPlatformManager.handle_incoming_message` path Slack/Teams use. IMAP
IDLE (near-real-time push) is a future enhancement; v1 polls on an interval.

## Security model

The email channel is a data-exfiltration surface, so the `From` header is never
trusted on its own. Controls, in order (see `app/services/email/security.py`):

1. **Internal-only mailbox** (admin config, documented) — biggest lever.
2. **Provider auth verdict** — parse the `Authentication-Results` header the
   receiving provider stamped and require `dmarc=pass` (or aligned
   `dkim=pass` + non-failing SPF). We read the verdict; we don't re-implement
   the crypto. Configurable via `require_auth_pass`.
3. **Domain allowlist** — `allowed_domains` restricts who may talk to the
   analyst. Empty list = rely on the internal-only mailbox + auth checks.
4. **Existing-member-only, no auto-provision** — unlike Slack/Teams (whose
   identity is IdP-vouched), an inbound email auto-links to an **existing** org
   member but never provisions a new account. Enforced in the manager's email
   branch (`allow_auto_provision = platform_type in ("slack","teams")`).
5. **First-contact verification** — the existing `is_verified` / token flow
   sends a verification link the user must click while signed in to BOW.
6. **Loop / auto-reply suppression** — `Auto-Submitted`, `Precedence: bulk`,
   list headers, and our own `From` are dropped so we never ping-pong.
7. **Least privilege + metadata** — DMs use the user's accessible data sources
   only; the security verdict (`dmarc/dkim/spf`, from-address, reason) travels
   with the message as audit metadata.

## Threading & report re-attachment

Threading uses RFC headers (`Message-ID` / `In-Reply-To` / `References`), not
the subject. The **conversation root** is the `Message-ID` of the *first*
message in the thread — and it's stamped onto `Completion.external_thread_ts`.

- **User emails first**: their `Message-ID` is the root; a new report is created
  with that root.
- **Agent emails first** (`EmailAdapter.send_new_message` returns the
  `Message-ID`): that id is the root and is stamped on the report's completion.
- **Either way**, when the user replies, their `References`/`In-Reply-To` carry
  the root, so `ExternalPlatformManager._find_report_by_thread_ts(thread_ts)`
  lands on the **same report**. This symmetry is what makes agent-initiated
  conversations re-attach correctly.

Every outbound reply sets `In-Reply-To` + `References` to the root so mail
clients chain the visible thread.

## Outbound consolidation (integration overrides global SMTP)

When an org configures the Email integration with SMTP, that mailbox becomes the
**authoritative** outbound transport for *all* org mail — share notifications,
scheduled-report results, verification, and the analyst's replies — overriding
the global `settings.email_client`. If no integration is configured, the global
client is used (unchanged behavior).

- Decision logic: `app/services/email_client_resolver.choose_outbound` (pure,
  unit-tested) → `resolve_outbound(db, org_id)` (DB-backed).
- `NotificationService._resolved_send` uses it; `send_custom_email` accepts
  optional `db` + `organization_id`.
- Caveats: setup-time verification mail uses the global fallback
  (chicken-and-egg); validate-on-save since the mailbox becomes the sole path;
  OAuth unification rides on the same `auth_type` abstraction in v2.

## File map

| Concern | File |
|---|---|
| Inbound security gate | `app/services/email/security.py` |
| Message construction (threading) | `app/services/email/message_builder.py` |
| SMTP send | `app/services/email/sender.py` |
| IMAP read + fake reader | `app/services/email/mailbox_reader.py` |
| Conversational adapter | `app/services/platform_adapters/email_adapter.py` |
| Adapter registration | `app/services/platform_adapters/adapter_factory.py` |
| Inbound poller | `app/services/email_poller_service.py` |
| Outbound resolver | `app/services/email_client_resolver.py` |
| Manager (auto-link, routing) | `app/services/external_platform_manager.py` |
| Create/test integration | `app/services/external_platform_service.py` |
| Config schema | `app/schemas/external_platform_schema.py` |
| API route | `app/routes/external_platform.py` (`POST /settings/integrations/email`) |
| Notification override | `app/services/notification_service.py` |
| Poller startup | `main.py` (leader-gated) |
| Integrations UI card | `frontend/components/EmailIntegrationModal.vue` + `frontend/pages/settings/integrations/index.vue` |
| Sandbox feedback loop | `backend/email_sandbox/` (30 tests, no DB) |
| DB-backed e2e | `backend/tests/e2e/test_email_integration.py` (real app + manager) |

## v1 / v2 sequencing

- **v1 (this change):** IMAP/SMTP with `password` auth; one Email card with
  progressive SMTP→channel capability; per-org outbound override; full security
  + threading + reattachment; sandbox feedback loop.
- **v2 (roadmap):** `xoauth2` auth strategy → Microsoft 365 (Entra app
  registration) + Google (OAuth client) connect flows reused by both analyst
  replies and notifications; IMAP IDLE for near-real-time inbound. The connect
  UI becomes "Sign in with Microsoft/Google"; the transport stays IMAP/SMTP.

## Frontend

The integrations page has an **Email** card (mirroring Slack/Teams) with a
two-section progressive form in `EmailIntegrationModal.vue`: **Outbound (SMTP,
required)** and a **"Receive email as a channel" (IMAP, optional)** toggle that
reveals IMAP fields, an allowed-domains field, and the auto-link / require-auth
switches. The connected view shows capabilities (send vs send+receive) and a
**Test connection** button (`POST /settings/integrations/{id}/test`).

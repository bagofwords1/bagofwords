# Email channel — outbound transport & inbound identity (spec)

Status: **agreed design, not yet implemented.** This is the reference for the
next implementation pass. It supersedes the "integration mailbox is the org's
outbound transport" behavior currently in `email_client_resolver` /
`notification_service`, and the "auto-link on by default" inbound behavior in
`external_platform_manager`.

---

## 1. Outbound transport — strict separation

There are two kinds of outbound mail, and they must use different transports.

| Mail | Transport |
|---|---|
| **Analyst mail** — the agent `send_email` tool + channel replies (analyst answering an inbound email) | **Analyst inbox** (the Email integration mailbox), always |
| **System mail** — share notifications, scheduled-report/prompt results, verification/registration links, any org notification | **Org SMTP** (if configured) → **Global SMTP** (`bow-config`). **Never** the analyst inbox |

Changes from today:
- **Remove** the behavior where an Email integration becomes the org's transport
  for *all* mail. The analyst inbox is used **only** for analyst mail.
- **Drop** the proposed "use AI email as SMTP" checkbox — the two are kept
  strictly separate.
- **Keep** the org-SMTP override (org SMTP overrides global). Good as‑is.

### Org SMTP storage
Store in `OrganizationSettings.config.smtp` (a setting, not an `ExternalPlatform`):

```json
"smtp": {
  "enabled": true,
  "host": "...", "port": 587, "security": "starttls",
  "username": "...", "password_enc": "<fernet>",
  "from_address": "noreply@acme.com", "from_name": "Acme"
}
```

- The **password is Fernet‑encrypted** (`password_enc`) using
  `settings.bow_config.encryption_key` — never stored in plaintext in the JSON.
  Small encrypt/decrypt helper; resolver decrypts at send time.
- Read/update via the org-settings API; UI section with a **Test connection**
  button (reuse the `…/email/test`-style probe).

### Resolver
`resolve_outbound(db, org_id, purpose)`:
- `purpose="analyst"` → the analyst inbox (Email integration). Used by
  `send_email` and channel replies.
- `purpose="system"` → **org SMTP (decrypt) if `enabled`, else global**. Used by
  `dispatch`/`_send_email`, scheduled prompt/report results, verification.

`send_custom_email` / `_resolved_send` take `purpose` and pass it through. The
`send_email` tool path is `analyst`; everything else is `system`.

---

## 2. Inbound identity & anti‑spoofing — verify‑first

### Threat model (why From alone is not enough)
- The `From` header is forgeable.
- **DMARC/DKIM validates the *domain*, not the *mailbox*** (DKIM is a domain key;
  SPF ignores the local part). Managed tenants usually enforce per‑user From, but
  the residual risk is an insider with SendAs or a misconfigured mail system.
- **DMARC isn't reliably available on every transport** — it's trustworthy only
  when we read from a provider mailbox that stamped `Authentication-Results`
  (IMAP from M365/Google). A self‑hosted SMTP listener has no trusted verdict.

**Conclusion:** DMARC is a useful *pre‑filter*, not identity validation. We must
never grant data access on a spoofable `From` alone.

### The three layers
1. **Pre‑filter (transport):** require DMARC/DKIM pass where available + domain
   allowlist + loop/auto‑reply/mailing‑list suppression. (Already built in
   `security.evaluate_inbound`.) Drops external spoofers and noise before we
   ever respond.
2. **Validation (identity) — the real gate:** **first‑contact verification.**
   The first time an address writes in, we send a **verification link** to that
   address; the recipient clicks it **while signed into BOW**, which proves they
   control *both* the mailbox *and* a BOW account. This creates a **verified
   `email → BOW user` binding**, independent of `From` trust and transport.
3. **Trust thereafter:** once the mapping is verified, subsequent emails from
   that address are trusted (no link each time) — same as Slack/Teams
   `is_verified`.

### Default vs opt‑in
- **Default = verify‑first** (NOT auto‑link). Flip the current default.
- **Auto‑link = explicit opt‑in**, relabeled to make the trade‑off clear:
  *"Auto‑link members without verification (less secure)."* When on, a
  DMARC‑passing, allowlisted member is linked + verified with no click — the
  admin accepts the residual insider/misconfig risk.

### Identity ladder (after the pre‑filter passes)

| Sender state | Action |
|---|---|
| **Member**, verify‑first (default) | First contact → **verification link** → on click, verified → proceed; later mail trusted |
| **Member**, auto‑link opt‑in | Linked + verified immediately (DMARC‑gated), no click |
| **Registered user (any org) + open, non‑expired invite** to this org | **Accept‑invite / verify link** → joins org + verified |
| **Not registered + open, non‑expired invite** | **Registration link** (existing invite‑token signup) → sign up → auto‑join → usable |
| **Not registered + no invite, signup policy admits the domain** | **Registration / sign‑up link** (org opted the domain in; still require the click — From is spoofable) |
| **None of the above** | **Ignore + audit** (`email.ignored_non_member`) — no unsolicited "register for BOW" mail |

Principle running through the ladder: where Slack/Teams *silently auto‑provision*,
email instead **sends a link** — because clicking a link delivered to the real
mailbox is what validates a spoofable `From`.

---

## 3. UI changes
- **Email integration modal:** relabel the toggle to *"Auto‑link members without
  verification (less secure)"*, **default OFF** (verify‑first). Helper text
  explaining the trade‑off.
- **Org SMTP:** a settings section (separate from the Email integration) for the
  `OrganizationSettings.config.smtp` block — host/port/user/password/security/
  from + **Test connection**.
- **Remove** the "use AI email as SMTP" idea entirely.

---

## 4. Implementation surface
- `email_client_resolver`: add `purpose`; `system` → org‑SMTP(decrypt)/global;
  `analyst` → analyst inbox. Remove "integration is org transport".
- `notification_service`: `_resolved_send`/`send_custom_email`/`dispatch`/
  scheduled paths pass `purpose="system"`; `EmailSendService` (send_email tool)
  uses `purpose="analyst"`.
- Org settings: `smtp` block + Fernet helper + API + UI + Test.
- `external_platform_manager`: rework the email identity branch into the ladder;
  default verify‑first; keep ignore+audit as the floor.
- `email_adapter`: send the right link type (verification vs registration/accept‑
  invite) per rung; reuse existing token URLs.
- Invites/signup: reuse the existing invite‑token registration URL and
  `signup_policy` / `auto_provision_user_for_org` domain logic; add an
  open‑invite lookup by email.
- Audit events: `email.ignored_non_member` (exists), plus
  `email.verification_sent`, `email.registration_invited`, `email.auto_linked`.

## 5. Notes
- DMARC reliability is transport‑dependent; treat it as best‑effort pre‑filter,
  never as the sole grant.
- Verification/registration links go only to addresses that cleared the
  pre‑filter (DMARC + allowlist), so we don't emit links to arbitrary spoofers.

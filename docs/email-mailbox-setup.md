# Connecting the AI Analyst Mailbox (Microsoft 365 & Google Workspace)

This guide walks an **administrator** through giving the Bag of words (BOW) AI
analyst its own mailbox (e.g. `analyst@acme.com`) so it can **send** answers and
notifications and (optionally) **receive** questions by email.

## How it works (read this first)

The analyst is like a teammate with an inbox. **BOW connects *outbound* to that
one mailbox** — it sends via SMTP and (optionally) reads via IMAP. It never
exposes an inbound port and never logs in as your end users.

Because Basic Auth (passwords / app passwords) is being removed by Microsoft and
Google, BOW authenticates to cloud mailboxes with **OAuth tokens over IMAP/SMTP
(XOAUTH2)**. You pick the method that matches your environment:

| Your environment | Auth method to choose in BOW |
|---|---|
| On‑prem Exchange, or any mailbox that still allows IMAP/SMTP passwords | **Username & password** |
| Microsoft 365 / Exchange Online | **Microsoft 365 (OAuth app‑only)** |
| Google Workspace | **Google Workspace (service account)** |

The mailbox credential is **separate from your SSO sign‑in config** — same
tenant/identity provider, but a distinct, least‑privilege credential. You do not
reuse the Entra/Google login app you configured for user sign‑in.

You configure everything at **Settings → Integrations → Email** in BOW.

---

# Option A — Microsoft 365

**Time:** ~10 minutes. **You need:** Global/Application admin in Entra and
Exchange admin (for the mailbox grant).

### Step 1 — Create the mailbox
In the Microsoft 365 admin center, create the address — a **shared mailbox**
(`analyst@acme.com`) is ideal: it's free (no license), has no interactive
sign‑in, and app‑only access works on it.

### Step 2 — Register an Entra application
1. Go to **Microsoft Entra admin center → Identity → Applications → App
   registrations → New registration**.
2. Name it (e.g. "BOW Analyst Mailbox"), choose **Accounts in this
   organizational directory only** (single tenant), and **Register**.
3. On the app's **Overview**, copy the **Directory (tenant) ID** and the
   **Application (client) ID** — you'll paste these into BOW.

### Step 3 — Add the mail permissions (Exchange Online, *not* Graph)
1. In the app → **API permissions → Add a permission → APIs my organization
   uses** → search **Office 365 Exchange Online**.
2. Choose **Application permissions** and add:
   - `IMAP.AccessAsApp` — to read mail (only needed if you'll receive)
   - `SMTP.SendAsApp` — to send mail
   - *(optional)* `POP.AccessAsApp`
3. Click **Grant admin consent for &lt;tenant&gt;** (required — there's no user to
   consent for app‑only).

### Step 4 — Create a client secret
**Certificates & secrets → Client secrets → New client secret** → copy the
**Value** immediately (you can't see it again). This is the **client secret**
for BOW.

### Step 5 — Authorize the app for *this one mailbox* (scopes it down)
By default the app could access any mailbox — this step restricts it to just
`analyst@acme.com`. In **Exchange Online PowerShell**
(`Connect-ExchangeOnline` from the `ExchangeOnlineManagement` module):

```powershell
# Register the app's service principal in Exchange.
# IMPORTANT: ObjectId is the *Enterprise application* object id
# (Entra → Enterprise applications → your app → Object ID),
# NOT the App registration object id.
New-ServicePrincipal -AppId <APPLICATION_CLIENT_ID> -ObjectId <ENTERPRISE_APP_OBJECT_ID> -DisplayName "BOW Analyst Mailbox"

# Grant the app access to only this mailbox (read).
Add-MailboxPermission -Identity analyst@acme.com -User <SERVICE_PRINCIPAL_ID> -AccessRights FullAccess

# Allow the app to send as this mailbox (SMTP).
Add-RecipientPermission -Identity analyst@acme.com -Trustee <SERVICE_PRINCIPAL_ID> -AccessRights SendAs
```

### Step 6 — Make sure SMTP AUTH is enabled on the mailbox
Even with OAuth, the SMTP submission protocol must be enabled for the mailbox:

```powershell
Set-CASMailbox -Identity analyst@acme.com -SmtpClientAuthenticationDisabled $false
```
(If it's disabled at the tenant level you'll also need
`Set-TransportConfig -SmtpClientAuthenticationDisabled $false`, or an
authentication policy that permits it.)

### Step 7 — Enter it in BOW
**Settings → Integrations → Email → Connect**, choose **Microsoft 365**, and fill in:

| BOW field | Value |
|---|---|
| Mailbox address | `analyst@acme.com` |
| Directory (tenant) ID | from Step 2 |
| Application (client) ID | from Step 2 |
| Client secret | from Step 4 |
| Receive email as a channel | toggle on if you want inbound |
| Allowed sender domains | e.g. `acme.com` (recommended) |

BOW fills in the Office 365 hosts automatically (`smtp.office365.com:587`,
`outlook.office365.com:993`). Click **Connect** — BOW mints a token and verifies
it can authenticate before saving.

### Microsoft troubleshooting
| Symptom | Fix |
|---|---|
| Connect fails with `oauth token failed` | tenant/client/secret wrong, or admin consent not granted (Step 3) |
| `xoauth2 rejected` on SMTP | `Add-RecipientPermission … SendAs` missing, or SMTP AUTH disabled (Step 6) |
| `xoauth2 rejected` on IMAP | `Add-MailboxPermission … FullAccess` missing, or `IMAP.AccessAsApp` not consented |
| Worked, then stopped | client secret expired — create a new one and update BOW |
| `New-ServicePrincipal` "not found" | use the **Enterprise application** object id, not the app registration's |

---

# Option B — Google Workspace

**Time:** ~10 minutes. **You need:** a Google Cloud project and a Workspace
**super admin** (for domain‑wide delegation).

### Step 1 — Create the mailbox
In the Google **Admin console → Directory → Users**, create
`analyst@acme.com`. This must be a **licensed user mailbox** (a Google Group /
collaborative inbox will not work with the Gmail API/IMAP).

### Step 2 — Create a service account + key
1. In the **Google Cloud console**, create (or pick) a project — ideally a
   dedicated one for this.
2. **APIs & Services → Library →** enable the **Gmail API**.
3. **APIs & Services → Credentials → Create credentials → Service account.**
   Name it (e.g. "bow-analyst-mailbox") and create it.
4. Open the service account → **Keys → Add key → Create new key → JSON** →
   download the file. This JSON is what you paste into BOW.
5. On the service account's details, copy its **Unique ID** (a long number) —
   this is the **OAuth Client ID** you authorize in the next step.

### Step 3 — Authorize domain‑wide delegation
1. **Admin console → Security → Access and data control → API controls →
   Domain‑wide delegation → Manage Domain Wide Delegation → Add new.**
2. **Client ID:** the service account's Unique ID from Step 2.
3. **OAuth scopes:** `https://mail.google.com/`
   (this single scope covers IMAP + SMTP send/receive).
4. **Authorize.**

> Note: domain‑wide delegation lets the service account act for users in your
> domain on that scope. BOW only ever impersonates the one configured mailbox.
> Use a dedicated service account and this single scope to keep it least‑
> privilege. If your org enabled **multi‑party approval** for DWD, a second
> super admin must approve this authorization.

### Step 4 — Enter it in BOW
**Settings → Integrations → Email → Connect**, choose **Google Workspace**, and fill in:

| BOW field | Value |
|---|---|
| Mailbox address | `analyst@acme.com` (the impersonated mailbox) |
| Service‑account JSON | paste the entire JSON key file from Step 2 |
| Receive email as a channel | toggle on if you want inbound |
| Allowed sender domains | e.g. `acme.com` (recommended) |

BOW fills in the Gmail hosts automatically (`smtp.gmail.com:587`,
`imap.gmail.com:993`). Click **Connect** to verify and save.

### Google troubleshooting
| Symptom | Fix |
|---|---|
| `oauth token failed` / `unauthorized_client` | DWD not authorized for this Client ID + scope, or wrong Unique ID (Step 3) |
| `invalid_grant` | the mailbox address (impersonated subject) doesn't exist or isn't a licensed user |
| Gmail API errors | the Gmail API isn't enabled on the project (Step 2.2) |
| Worked, then stopped | service‑account key was rotated/deleted — create a new key and update BOW |

---

# Option C — Username & password (on‑prem / app password)

For on‑prem Exchange or any server that still allows IMAP/SMTP password auth
(or a mailbox with an app password):

Choose **Username & password** in BOW and provide **SMTP host/port/username/
password** (and, to receive, **IMAP host/port/username/password**). No console
setup is needed. Note this won't work against Microsoft 365 or Gmail, where
Basic Auth has been removed.

---

# Receiving email (optional) — security

When you toggle **Receive email as a channel**, anyone who emails the analyst
could ask it to run queries, so BOW enforces several controls. Configure these
in the same modal:

- **Allowed sender domains** — only senders on these domains are accepted.
  Leave blank only if the mailbox is internal‑only.
- **Require DMARC/DKIM pass** (recommended, on by default) — BOW drops messages
  that fail the provider's anti‑spoof verdict.
- **Auto‑link to existing members** — a sender is linked to an existing BOW user
  with the same email; BOW never creates a new account from an inbound email.
- Spoofed, off‑allowlist, auto‑reply, and mailing‑list messages are rejected
  before they ever reach the agent.

Replies are threaded (the analyst's answer chains to the user's email), and
follow‑up replies re‑attach to the same BOW report.

---

# Verifying

After connecting, open the Email integration and click **Test connection**. BOW
mints a fresh token and authenticates to SMTP (and IMAP if receiving) via
XOAUTH2 — you'll see `SMTP ok` / `IMAP ok` on success, or the specific failure
to fix.

# Quick reference — what to paste into BOW

| Method | Secrets you provide | Hosts |
|---|---|---|
| Microsoft 365 | tenant ID, client ID, client secret, mailbox address | auto (Office 365) |
| Google Workspace | service‑account JSON, mailbox address | auto (Gmail) |
| Username & password | SMTP/IMAP host, port, username, password | you enter them |

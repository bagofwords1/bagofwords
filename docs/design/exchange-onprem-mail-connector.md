# Exchange Server (on-prem) mail connector — design

A read-only mail connector for **on-premises Microsoft Exchange Server**, so the
agent can list, search and read mail in mailboxes that never reach Microsoft 365.
It reuses the existing mail agent tools (`list_emails`, `read_email`,
`search_email`) and the Windows authentication already proven by the
`sharepoint_onprem` connector, so it ships **no new agent tool surface and no
new authentication code path**.

Status: **phase 1 implemented, in beta.** Verified with automated tests and a
local protocol-accurate server; not yet verified against a real Exchange Server.
See [the feedback loop](../feedback-loops/exchange-onprem-connector.md) for what
was run and what remains, and [the admin guide](../exchange-server.md).

## Why a new connector

| Existing piece | Why it does not cover on-prem Exchange |
|---|---|
| `outlook_mail` (`graph_mail_client.py`) | Talks to Microsoft Graph with Entra credentials. Graph does not serve mailboxes hosted on an on-prem Exchange server. |
| Email integration (`app/services/email/`) | IMAP + SMTP for **one** analyst mailbox used as a conversation channel. It is not a knowledge source and does not read users' mail. |
| `sharepoint_onprem` | Right authentication and deployment model (NTLM / Kerberos, internal CA, no cloud dependency), but for document libraries. |

On-prem Exchange exposes mailboxes through **Exchange Web Services (EWS)**, a
SOAP API at `https://<host>/EWS/Exchange.asmx`. EWS remains supported on
Exchange Server; the announced EWS retirement applies to Exchange Online only.

## Principle

**A mailbox is a mailbox to the agent, wherever it is hosted.** The connector
conforms to the mail contract the tool layer already drives (capabilities
`LIST_EMAILS` / `READ_EMAIL` / `SEARCH_EMAILS` in `clients/base.py`), so the
planner sees the same three tools it sees for `outlook_mail` and `gmail_mail`.
Everything new is confined to one client and its registry entry.

## Scope

**In (v1):**
- The signed-in identity's own mailbox: folder tree, listing, keyword search,
  reading a message as plain text.
- **Named shared mailboxes** — extra mailbox addresses listed in the connection
  config. Exchange decides, per request, whether the identity may open them.
- Folder / unread / received-window filters and cursor paging, with the same
  semantics and limits as the other mail connectors (`mail_common.py`).
- NTLM as a service account or per user; Kerberos as a service account.

**Later (separate decisions, see Phasing):**
- Public folders, online archive mailboxes, attachments.
- Service-account impersonation across many mailboxes.
- OAuth sign-in for servers configured for it.
- Calendar, contacts, and any write operation (send, move, flag, delete).

**Out:** the analyst-mailbox channel. Moving that from IMAP/SMTP to EWS is a
different feature with a different security model.

## What "team folders" means here

EWS runs every request as the authenticated identity and Exchange enforces the
mailbox and folder permissions. The connector never widens access; it only
decides which mailboxes it will *ask* for.

| Team setup in Exchange | Supported | How |
|---|---|---|
| Personal mailbox | v1 | Default for the authenticated identity. |
| Shared mailbox (e.g. a support inbox) | v1 | Address listed in `shared_mailboxes`; the identity needs Full Access. |
| A colleague's folder shared with the user | v1, limited | Address listed in `shared_mailboxes`. Only folders the identity can see are returned; a shared subfolder under a hidden parent cannot be reached by walking. |
| Public folders | Phase 2 | Separate root. The server allows one-level traversal only, so the tree is fetched level by level and search runs per folder. |
| Online archive mailbox | Phase 2 | Separate root in the same mailbox. |

Two consequences shape the design:

1. **There is no discovery.** EWS has no "mailboxes I can open" call, so shared
   mailboxes must be named in the connection config by an admin.
2. **Auth scope decides visibility.** With per-user sign-in, each person sees
   their own mailbox plus the configured shared mailboxes they hold rights to.
   With a service account, everyone on the connection sees what that account can
   open — right for a team inbox, wrong for personal mail. The form copy must
   say this plainly.

## Architecture

```
agent tools (unchanged)          list_emails / search_email / read_email
        │  capability gate: LIST_EMAILS / SEARCH_EMAILS / READ_EMAIL
        ▼
ExchangeOnpremMailClient         mail contract: list_mail_folders,
  (new, clients/)                resolve_mail_folder, list_messages,
        │                        search_messages, read_file, test_connection
        ▼
_exchange_ews.py (new)           builds SOAP envelopes, parses responses,
        │                        maps faults to actionable errors
        ▼
Windows-auth HTTP session        pinned egress, NTLM or Kerberos, deployment
  (shared with sharepoint_onprem) CA bundle, no ambient proxy / netrc
        ▼
https://<host>/EWS/Exchange.asmx
```

### Session reuse

`SharepointOnpremClient._session()` already builds exactly the session EWS
needs: a pinned session, per-thread, NTLM with username/password or Kerberos
from a mounted keytab with mutual authentication and expiry-driven renewal,
`REQUESTS_CA_BUNDLE` for internal CAs, and ambient proxy settings disabled.

**Change:** lift that method into a small shared helper (for example
`clients/_windows_auth_session.py`) that both connectors call. The SharePoint
connector's behavior must not change; its existing unit tests are the guard.

### EWS operations used

Four read operations, all `POST` to the single endpoint:

| Operation | Used for |
|---|---|
| `FindFolder` | Folder tree under the message root of each mailbox (deep traversal), with unread and total counts. |
| `FindItem` | Listing (sorted by received time, newest first, with unread / received restrictions) and keyword search (query string), paged by offset. |
| `GetItem` | One message: subject, sender, recipients, received time, body requested as **plain text**, and the web-client link. |
| `GetFolder` | The well-known folders of each mailbox (Inbox, Sent, Drafts, Deleted, Junk) by role, so they are found whatever the server language. Doubles as the connection test. |

Requests carry a fixed schema version suitable for Exchange 2013 and later
(2016, 2019 and Subscription Edition are the targets). No version selector in
v1; add one only if a real deployment needs an older schema.

Responses are parsed with entity expansion disabled and a response-size ceiling,
the same posture as the SharePoint SOAP adapter.

## Client contract

The client mirrors `GraphMailClient` so `email_tools.py` needs no change.

| Method | Behavior |
|---|---|
| `list_mail_folders()` | Rows of `{id, path, unread, total}` for the primary mailbox and each configured shared mailbox. Bounded (same 500-folder ceiling as the other mail clients). Cached per client instance. |
| `resolve_mail_folder(folder)` | Accepts a folder id, a path, or a unique display name, case-insensitive. Unknown or ambiguous names raise with the paths that do exist. |
| `list_messages(folder, unread_only, received_after, received_before, max_results, cursor)` | Returns `{items, next_cursor, folder, note}`, newest first, filtered server-side. |
| `search_messages(query, …same filters…)` | Keyword search, same return shape. |
| `read_file(message_id)` | `Subject / From / To / Date / Link` header block plus the plain-text body — the same rendering as the other mail clients. |
| `get_schemas()` | Returns `[]`. Mail is always read live; nothing is indexed or copied. |
| `test_connection()` | Authenticates, opens the Inbox of each mailbox on the connection, and reports the message count of each and which mailboxes were not reachable. Fails only when no mailbox can be opened. |

**Folder addressing across mailboxes.** Paths in the primary mailbox are
unprefixed (`Inbox/Projects`). Paths in a shared mailbox are prefixed with its
address (`support@acme.example/Inbox`). The prefix is what `list_mail_folders`
returns, so the agent copies it back rather than constructing it.

**Default listing scope.** With no `folder`, a listing covers the whole default
mailbox: every folder mail arrives in, newest first, leaving out Sent, Drafts,
Deleted and Junk. A shared mailbox address as the folder covers that mailbox the
same way. The first implementation listed the Inbox only; a live agent run then
missed an unread escalation that an inbox rule had filed into a subfolder, which
is the common case on Exchange, so the default was widened.

EWS has no "whole mailbox" item listing, so the client asks each incoming folder
for its newest messages (several folders per request) and merges them. Folders
cannot be paged together by offset; the page boundary is therefore a point in
time. The cursor is the received time of the last message returned, plus the
ids already returned at that exact second, and the next call asks every folder
for what is at or before it. A folder that had more to give than it returned
only vouches for messages down to its oldest returned one, so nothing older
than that is emitted until the next call. This stays correct when a server caps
its pages or reads one folder per request.

**Search scope.** With no `folder`, search covers every mailbox on the
connection (excluding Deleted and Junk), skipping empty folders, several folders
per request. A shared mailbox address searches that mailbox. Results gathered in
one call are returned newest first; the cursor is the folder and offset reached.
One call is bounded in requests and returns a cursor when it stops early.

**Saying what was covered.** Listings and searches return a note the tool adds
to its observation: what a whole-mailbox listing covered, which other mailboxes
exist, and which configured mailboxes this identity could not open. Without it
the agent reported an unreachable shared mailbox as visible.

**Filters with search.** A keyword query and a structured restriction cannot be
sent in the same `FindItem`. Unread and received bounds are therefore folded
into the query string where the server's query syntax supports them, and
otherwise applied to the returned rows — the approach `outlook_mail` already
takes for the same reason.

**Message ids** are the opaque EWS item ids, passed through unchanged. They are
long; the tools already treat ids as opaque strings.

## Configuration and credentials

Registry entry `exchange_onprem` in `data_source_registry.py`: category
`services`, `requires_license="enterprise"`, `version="beta"`, mail nouns
(`message` / `messages`), no catalog, schema-generated form.

**Config (`ExchangeOnpremConfig`)**

| Field | Notes |
|---|---|
| `ews_url` | Required. Server URL; the standard service path is added when omitted. HTTPS only, no credentials, query or fragment in the URL. No autodiscovery in v1 — it adds DNS and redirect surface for little gain when an admin is filling in the form. |
| `shared_mailboxes` | Optional comma-separated mailbox addresses. Validated as addresses; at most 20. |
| `include_own_mailbox` | On by default. Off for a service account whose own mailbox is irrelevant: the first shared mailbox becomes the default. |
| `allow_http` | Lab-only escape hatch, default off, same wording as the SharePoint connector. |

**Credentials** — the same two variants as `sharepoint_onprem`:

| Variant | Scopes | Notes |
|---|---|---|
| Windows credentials (NTLM) | `system`, `user` | `DOMAIN\user` or `user@domain` plus password. |
| Kerberos service account | `system` | Mounted `krb5.conf` and client keytab; optional principal. Requires the `kerberos` extra. |

The credential schemas mirror the SharePoint Server ones, with mail-specific
field descriptions.

## Security

- **Read-only by construction.** The client contains no code path for sending,
  moving, flagging or deleting. Only the four operations above are built.
- **No privilege widening.** Every request runs as the configured identity;
  shared mailboxes are attempted, never assumed. An access-denied fault on a
  shared mailbox is reported, not retried under another identity.
- **Stored domain passwords.** Per-user NTLM stores each user's Windows password
  encrypted, as the SharePoint connector does today. Two rules follow:
  - Stop after the first authentication failure for a credential and surface
    "sign in again". Repeated attempts with a stale password can lock the
    account in the directory. Implemented as a process-wide memory of rejected
    username/password pairs (five minutes): a client is built per tool call, so
    a per-client flag would not stop an agent that calls three tools in a row.
    Changed credentials are a different pair and are tried at once.
  - Never log request bodies or headers.
- **Egress.** The endpoint host is fixed at connection time and pinned; redirects
  to another host are refused.
- **Transport.** HTTPS with certificate verification on; internal CAs come from
  the deployment's CA bundle, never from a path typed into the form.
- **Prompt-injection surface.** Mail bodies are untrusted text reaching the
  model, exactly as with the existing mail connectors. No new mitigation is
  introduced here, and none is removed.

## Error handling

Map EWS faults to messages a user or admin can act on, in the style of
`friendly_tool_error`:

| Fault | Message direction |
|---|---|
| HTTP 401 | Credentials rejected — sign in again. No retry. |
| Access denied on a mailbox or folder | Names the mailbox; says the identity lacks permission. |
| Mailbox not found / not an Exchange mailbox | Names the address; points at the config. |
| Server busy with a back-off hint | Wait for the hinted interval once, then fail with "server is throttling". |
| Item or folder not found | "No longer exists" — the message was moved or deleted since listing. |
| HTTP 404 / non-SOAP response | "This URL is not an Exchange Web Services endpoint." |

## Testing

- **Unit** (`tests/unit/test_exchange_onprem_client.py`): canned SOAP responses
  for each operation and each fault; folder resolution; cursor round-trips;
  filter folding; multi-mailbox prefixes; the stop-after-one-auth-failure rule.
- **Shared session:** the existing `test_sharepoint_onprem_client.py` must pass
  unchanged after the session helper is lifted out.
- **Registry and generic e2e:** the checks listed in the `add-connection-type`
  skill.
- **Live verification is a hard prerequisite for leaving beta.** There is no
  Exchange lab today, and the existing directory lab cannot host one. Either a
  Windows Server lab is built, or verification runs against a customer test
  mailbox. Record the loop under `docs/feedback-loops/`.

## Phasing

| Phase | Content |
|---|---|
| 1 — **done** | Shared session helper; client with own mailbox + named shared mailboxes; registry entry, schemas, locale strings in every catalog; unit, e2e and egress tests; sandbox verification with a real LLM; admin guide. Verification on a real Exchange server is outstanding. |
| 2 | Public folders (level-by-level tree, per-folder search) and online archive, each behind a config switch. Attachments, reusing the document extraction the file connectors use. |
| 3 | Service-account impersonation with an explicit allowlist of mailboxes; OAuth sign-in for servers that offer it. Each needs its own security review. |

## Planner context

The tool surface is identical for a personal mailbox read with each user's own
sign-in and a team mailbox read through one service account. The schema context
therefore states which it is and names the mailboxes; otherwise the model calls
a shared support mailbox "your Inbox".

## Open questions

Status after Phase 1. Items marked *client-side settled* are handled in code
and tested against the local server; all still need confirming on a real one.

1. **Channel binding** — *client-side settled.* Recent Exchange builds enforce
   Extended Protection by default, which requires the NTLM client to bind to the
   TLS channel and breaks behind TLS-terminating load balancers. A local server
   that refuses unbound sign-ins accepts the connector's, and refuses the same
   sign-in with the binding switched off. The load-balancer requirement is in the
   admin guide. Still to confirm on an Exchange server with the setting on.
2. **Search fan-out** — *client-side settled.* The client sends several folders
   per request and falls back to one per request when a server refuses, whether
   the refusal comes for the whole request or per folder. Which path a real
   server takes decides only the number of round-trips.
3. **Filter folding** — *avoided.* Unread and received bounds on a keyword
   search are applied to the returned rows, so nothing depends on the server's
   query syntax for dates or read state.
4. **Public-folder keyword search.** Whether content search works there at all
   on-prem, or only subject/sender matching.
5. **Schema version floor.** Whether one fixed schema version serves 2013
   through Subscription Edition without a selector.

## Decisions needed from the product owner

- **First scope:** personal + named shared mailboxes (this document's v1), or
  public folders first.
- **Default credential scope:** per-user sign-in, service account, or both
  offered with per-user as the default.
- **Verification path:** build a lab, or verify against a customer mailbox.

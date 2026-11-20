# Exchange Server (on-premises) mail

`exchange_onprem` is a read-only mail connector for Exchange Server hosted in
your own environment. The agent can list, search and read mail through Exchange
Web Services (EWS) with Windows authentication. No cloud tenant, app
registration or internet access to Microsoft is involved. An enterprise license
is required. The connector is in beta.

It is separate from **Outlook Mail**, which reads Microsoft 365 mailboxes.

## What it does

Create a connection → test → attach it to an agent → ask questions about mail.
The agent gets three tools: `list_emails`, `search_email` and `read_email`.

- **Read-only.** The connector cannot send, move, flag or delete anything. It
  only issues folder and message read requests.
- **Live.** Nothing is indexed or copied into BOW. Every list, search and read
  goes to Exchange at that moment, so results are always current and there is
  no schedule to configure.
- **Permission-true.** Every request runs as the configured Windows account and
  Exchange enforces that account's rights. The connector never widens access.

## Which mailboxes a connection covers

| Mailbox | Covered when |
|---|---|
| The signed-in account's own mailbox | **Include the account's own mailbox** is on (default) |
| Shared or team mailboxes | Their addresses are listed in **Shared mailboxes** and the account may open them |
| A colleague's shared folder | The colleague's address is listed in **Shared mailboxes**; only the folders the account can see are returned |

Exchange has no way to ask "which shared mailboxes can I open?", so shared
mailboxes must be named on the connection. A listed mailbox that an account
cannot open is skipped and reported — in the connection test and to the agent —
never read under another identity.

Not covered yet: public folders, online archive mailboxes, attachment contents
(attachment names are shown), calendars and contacts.

## Choosing who signs in

This choice decides whose mail people see. Pick it deliberately.

| Mode | Exchange sees | Everyone on the connection reads | Use it for |
|---|---|---|---|
| Shared service account (NTLM) | The configured account | Whatever that account can open | A team inbox such as support@ |
| Each user signs in (NTLM, user-required) | Each member's own account | Their own mailbox, plus the listed shared mailboxes they hold rights on | Personal mail |
| Kerberos service account | The mounted keytab / ticket-cache principal | Whatever that principal can open | A team inbox without a stored password |

The same choice decides where the connection shows up for people:

- **Each user signs in → a connector.** It appears under **Connectors** in the
  prompt box. A person uses it once they have signed in, it always runs as that
  person, and Auto never picks it on its own. When you turn on **Require user
  authentication** in the connection form, the form offers **Make available to
  everyone in your org**, which adds the connector for all members; each of
  them then signs in from the connectors list.
- **Service account → an agent.** It appears in the agent picker, Auto can
  choose it, and instructions apply, like any other source the organization
  curates.

An admin can switch a connector to an agent from the connectors list.

For a team inbox on a service account, turn **Include the account's own
mailbox** off: the first shared mailbox becomes the default and the service
account's own (usually empty) mailbox stays out of the way.

With **each user signs in**, members click **Sign in** on the connector, enter
their domain username and password, optionally test, and save. Until they do,
the connector is not available to them; it does not fall back to the service
account. **Sign out** removes their stored credentials. This is
credential-based Windows authentication, not automatic single sign-on.

Passwords are stored encrypted. If a stored password stops working, the
connector makes **one** sign-in attempt, reports that the credentials need
updating, and sends nothing more for that username and password for five
minutes. This prevents repeated agent calls from locking the account in the
directory. Updated credentials are tried immediately.

## Connection settings

| Field | Notes |
|---|---|
| Exchange server URL | For example `https://mail.example.com`. The service path `/EWS/Exchange.asmx` is added when omitted. HTTPS only. |
| Shared mailboxes | Optional, comma-separated addresses, up to 20. |
| Include the account's own mailbox | On by default. |
| Allow HTTP | For an isolated test lab only. |

There is no autodiscovery: enter the server URL.

## How listing and search behave

- **Listing with no folder** covers the whole default mailbox, newest first —
  every folder mail arrives in, including subfolders that inbox rules file into.
  Sent, draft, deleted and junk mail is left out unless that folder is named.
- **Listing a shared mailbox** by its address covers that whole mailbox the
  same way. Naming a folder (`Inbox/Projects`, or
  `support@example.com/Inbox/Escalations`) lists just that folder.
- **Search with no folder** covers every mailbox on the connection. Deleted and
  junk mail is left out unless named.
- Search takes keywords and the usual `from:` / `subject:` terms, `OR`, and
  quoted phrases. Unread and date filters can be combined with it.
- Folder names follow the mailbox's own language. `inbox`, `sent`, `drafts`,
  `deleted` and `junk` are always understood, whatever the server language.
- Results are paged. The agent is told when a result is incomplete and how to
  continue, and what a listing did and did not cover.

Limits: one connection reads up to 500 mail folders across its mailboxes, in the
order the mailboxes are listed. Past that, folders are not read: the connection
test says so, and the agent is told that its answer is not complete and which
mailboxes were only partly read or not read at all. Put very large mailboxes on
their own connection. On a server that can only read one folder per request, a
mailbox with more than 30 non-empty incoming folders is listed Inbox-only when
no folder is named; the agent is told so and can name other folders.

## Server requirements

1. Exchange Server 2013 or later with Exchange Web Services enabled and
   reachable from the **backend**.
2. HTTPS with a certificate the backend trusts. For a private certificate
   authority, mount a CA bundle and set `REQUESTS_CA_BUNDLE` in the backend
   environment. Certificate validation stays on.
3. Windows authentication (NTLM or Negotiate) enabled on the EWS virtual
   directory.
4. For a team inbox: a least-privilege service account with Full Access on the
   shared mailboxes it should read, and nothing else.
5. For Kerberos: the same deployment setup as SharePoint Server (on-prem) — the
   backend `kerberos` extra, a mounted `krb5.conf` and client keytab
   (`KRB5_CONFIG`, `KRB5_CLIENT_KTNAME`) or a managed ticket cache, and a correct
   `HTTP/<mail-host>` service principal name. See
   [SharePoint Server (on-premises)](sharepoint-server.md#keytab-and-deployment).

### Extended Protection and load balancers

Recent Exchange builds enable Extended Protection for Authentication by
default. The connector binds its NTLM sign-in to the TLS connection, which is
what Extended Protection requires. Two deployment conditions follow:

- TLS must not be terminated in front of Exchange (no SSL offloading), or a
  re-encrypting load balancer must present the **same certificate** as the
  Exchange servers. Otherwise the binding does not match and sign-in is refused.
- The backend must reach Exchange without a TLS-inspecting proxy in between.

A refused sign-in for this reason looks the same as a wrong password. If the
credentials are known to be right, check these two conditions first.

## Verification status

Verified against automated tests and a local protocol-accurate server with a
real Windows sign-in, including enforced channel binding. **Not yet verified
against a real Exchange Server.** Before relying on it in production, run the
connection test and the checks in
[the feedback loop](feedback-loops/exchange-onprem-connector.md#what-still-needs-a-real-exchange-server)
against your server.

## Troubleshooting

| Message | Meaning |
|---|---|
| Exchange rejected the username or password | Wrong credentials, or the Extended Protection conditions above are not met. Further attempts are held for five minutes. |
| This URL is not an Exchange Web Services endpoint | The URL answers, but not with EWS. Check the host and path. |
| Exchange redirected the request | Enter the server's own EWS URL, not one that redirects. |
| TLS verification failed | The backend does not trust the server certificate. Install the issuing CA. |
| Not reachable with this account: `<address>` | The account has no permission on that mailbox, or the address is wrong. |
| The Exchange server is throttling requests | Exchange asked the connector to back off. Try again shortly. |

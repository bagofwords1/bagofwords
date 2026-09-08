# OpenText Documentum

`documentum` is a read-only document connector over **Documentum REST Services**
(`https://host/dctm-rest`). It is separate from the SharePoint and file-share
connectors. An enterprise license is required.

## Supported workflow

Create a connection → choose a repository and root folder → test → attach it to
an agent → browse, search, read documents, and analyze CSV/Excel files in
reports. The connector never writes to Documentum. Report attachments and saved
queries are stored in BOW, not in the repository.

- One connection reads one repository (docbase). The **Root Folder** bounds the
  connection (`/Finance/Reports`, or `/` for every cabinet the identity can see);
  include globs and object types narrow it further. Reads enforce the scope even
  when given a raw `r_object_id`.
- Documents are addressed by their stable `r_object_id`; paths shown to the agent
  are relative to the root folder. Only CURRENT versions are listed. A document
  filed into several folders appears once per connection.
- CSV/TSV and XLS/XLSX as DataFrames; PDF, DOCX, PPTX and text extraction; JSON;
  original binary downloads and the BOW document-preview pipeline. Documentum
  format names (`msw12`, `excel12book`, `crtext`, …) are mapped to MIME types via
  the repository's `/formats` resource.
- Search combines the repository's full-text index (xPlore where installed;
  database search otherwise) with live name/title matching, so a document added
  minutes ago is found even before the index catches up.
- Default bounds: 5,000 catalog documents and 50 MB per file; maximum
  configurable bounds: 50,000 documents and 250 MB. Narrow the scope for larger
  repositories.

Not part of this connector: writes, check-in/out, workflows, virtual-document
assembly, D2-specific configuration semantics, and CMIS.

## Authentication choices

Documentum ACLs are evaluated for every call, because every call runs as the
authenticated identity. See `docs/documentum-auth.md` for the full design.

| Mode | Documentum sees | Credential location |
|---|---|---|
| Username / Password (system) | The configured repository user | Encrypted BOW connection credentials |
| Username / Password (user-required) | Each member's own repository login | Encrypted per-user connection credentials |
| OTDS OAuth client (system) | The OAuth client's service user | Encrypted BOW connection credentials |
| **OTDS impersonation (user-required)** | **Each member**, via an RFC 8693 token exchange minted by the connection's OTDS OAuth client | Members store only their Documentum login; the client secret stays on the connection |
| Sign in with OTDS (user-required) | Each member, via their own OTDS OAuth token | Per-user OAuth tokens managed by BOW |

For user-required connections members click **Connect**, pick a method, test and
save. Missing credentials block member access; they do not fall back to the
service account. Since Documentum 23.4 every non-inline login is brokered by
OTDS; members provisioned only from Entra ID (SCIM/JIT) have no password OTDS can
validate, so use impersonation or sign-in for them.

## Customer requirements

1. REST Services reachable from the **backend** over HTTPS with a trusted
   certificate (`Allow HTTP` is an isolated-lab exception). For a private CA,
   mount a CA bundle and set `REQUESTS_CA_BUNDLE`.
2. A least-privilege repository user (or OTDS OAuth client) with Browse/Read on
   the root folder. No superuser is required.
3. For impersonation: a confidential OTDS OAuth client with **Allow
   impersonation**, scoped to the users' partition, and impersonation enabled on
   the repository resource; REST running an OTDS token auth mode
   (`otds_token`, `oauth2` or `ct-otds_ticket-otds_token`).
4. For sign-in: the same OTDS client with BOW's callback registered as a
   redirect URI.
5. Content links must resolve to the REST host (the connector requests
   `media-url-policy=local`); ACS/BOCS-only deployments need REST-served content.

## Verification boundary

The September 2026 loop proves the whole flow against a **simulated** Documentum
REST + OTDS server (`tools/documentum/mock_documentum_server.py`): schema-generated
connect form, test connection, discovery, per-user impersonation with ACL
trimming, and agent reads of CSV/DOCX/PDF through the real tool pipeline. No
OpenText entitlement was available, so a real 23.4+/OTDS repository has **not**
been exercised; the items to confirm there are listed in
`docs/documentum-auth.md` §7. See the
[feedback loop](feedback-loops/documentum-connector.md).

References: `docs/documentum-connector-analysis.md`, `docs/documentum-auth.md`,
`docs/documentum-lab-access.md`.

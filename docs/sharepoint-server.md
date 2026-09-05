# SharePoint Server (on-premises)

`sharepoint_onprem` is a read-only document-library connector using SharePoint's
REST API. It is separate from the existing Microsoft Graph SharePoint and
SharePoint Lists connectors. An enterprise license is required.

## Supported workflow

Create a connection → choose a site and library/folder scope → test → attach it
to an agent → browse, search, read documents, and analyze CSV/Excel files in
reports. The connector never writes to SharePoint. Report attachments and saved
queries are stored in BOW, not in the source library.

- All visible document libraries (`*`), one named library, or the default library
  (blank). With all libraries, returned paths include the library name.
- Folder scope, recursive enumeration, include globs, metadata indexing or live
  listing. Reads enforce scope even when given an absolute server-relative ID.
- CSV/TSV and XLS/XLSX as DataFrames; PDF, DOCX, PPTX and text extraction; JSON;
  original binary downloads and the existing BOW document-preview pipeline.
- SharePoint indexed content search plus immediate live filename matching.
  New document **contents require a SharePoint search crawl**. BOW metadata
  indexing does not run that crawl. Search results outside the configured URL
  origin/site/library/folder/globs are rejected, and hits are reauthorized live.
- Default bounds: 5,000 catalog files and 50 MB per file; maximum configurable
  bounds: 50,000 files and 250 MB. Narrow the scope for larger libraries.

Document-library files are supported; arbitrary SharePoint lists, pages,
attachments to list items, writes, and Microsoft Graph OAuth are not part of
this connector. Older servers without the ResourcePath REST API are not
verified. The live lab uses SharePoint Server Subscription Edition.

## Authentication choices

| Mode | SharePoint sees | Credential location |
|---|---|---|
| Shared NTLM | The configured Windows account | Encrypted BOW connection credentials |
| User-required NTLM | Each member's supplied Windows account | Encrypted per-user connection credentials |
| Kerberos service account | The mounted keytab/ticket-cache principal | Deployment-managed keytab or ticket cache; optional principal in the form |

For user-required NTLM, members click **Connect**, test their domain username and
password, and save. Missing credentials block member access; they do not fall
back to the service account. Existing BOW administrator/owner fallback behavior
still applies to non-OAuth user-required connections. This is credential-based
Windows authentication, **not automatic SSO**.

Kerberos service mode does not impersonate the logged-in BOW user. End-user SSO
would additionally require identity mapping and a constrained-delegation flow
validated against the customer's AD and SharePoint SPNs. It is not implemented
by this connector. Do not select service mode expecting user-level delegation.

## Customer requirements

1. A site URL reachable from the **backend**, including the site path—not Central
   Administration. Configure SharePoint alternate access mappings for that URL.
2. HTTPS and a trusted server certificate. For a private CA, mount a CA bundle
   and set `REQUESTS_CA_BUNDLE` in the backend environment. Certificate validation
   remains enabled. `Allow HTTP` is an explicit isolated-lab exception.
3. A least-privilege account with read access to the selected site and libraries,
   or individual accounts if using user-required NTLM. No Domain Admin or SQL
   access is required by the connector.
4. For indexed content search: Search Service Application, a working content
   source/crawl, and search URLs matching the configured access URL. Verify
   security trimming with allowed and denied test users.
5. For Kerberos: Windows Integrated Authentication/Negotiate on the web
   application, correct unique `HTTP/<site-FQDN>` SPNs for the IIS service
   identity, working AD DNS/realm configuration, synchronized clocks, and
   private backend access to the KDC. Keep AD/KDC/RDP/SQL ports off the public
   Internet. Kerberos uses the **HTTP** SPN, not `MSSQLSvc`.

## Keytab and deployment

Install the backend `kerberos` extra. The root Dockerfile already includes the
MIT Kerberos runtime and installs this extra; the added `requests-gssapi`
dependency is picked up on the next image build. No new Docker OS packages are
required for this connector.

The connection form selects Kerberos and optionally a principal such as
`svc_bow@EXAMPLE.COM`. **Do not upload a keytab into the form or bake it into the
image.** The deployment owner mounts it read-only and sets:

```text
KRB5_CONFIG=/etc/bow-kerberos-config/krb5.conf
KRB5_CLIENT_KTNAME=/etc/bow-kerberos-secret/client.keytab
```

Alternatively, supply a managed ticket cache via `KRB5CCNAME`; its owner must
acquire/renew tickets. The connector creates thread-local authentication
contexts, requests Kerberos explicitly, requires mutual authentication, and
does not silently negotiate NTLM or change process-global Kerberos variables.
With a client keytab, near-expiry credentials are reacquired when next used.

The existing Helm chart supports the required environment and volume hooks, so
no chart-template change is necessary. Example **values**, referencing secrets
and config maps created by the deployment team:

```yaml
extraEnv:
  - name: KRB5_CONFIG
    value: /etc/bow-kerberos-config/krb5.conf
  - name: KRB5_CLIENT_KTNAME
    value: /etc/bow-kerberos-secret/client.keytab
extraVolumes:
  - name: sharepoint-krb5
    configMap:
      name: sharepoint-krb5
  - name: sharepoint-keytab
    secret:
      secretName: sharepoint-client-keytab
      defaultMode: 0440
extraVolumeMounts:
  - name: sharepoint-krb5
    mountPath: /etc/bow-kerberos-config
    readOnly: true
  - name: sharepoint-keytab
    mountPath: /etc/bow-kerberos-secret
    readOnly: true
```

Ensure the runtime UID/group can read the secret (set the pod security context
as appropriate for the platform). Mount it in every backend/worker that executes
connector calls. Restrict secret RBAC; rotate the account key and keytab
together, and roll the backend after rotation. OpenShift is not required;
Docker bind mounts, Kubernetes Secrets, or another deployment secret mechanism
can provide the same files.

## Verification boundary

The September 2026 lab proves real NTLM file access and localhost report flows.
Kerberos configuration and fail-closed behavior have deterministic tests, but
**live Kerberos is not verified**: the provided site advertises NTLM only and
no private KDC/keytab was supplied. Acceptance requires a real Kerberos-only
session plus server-side authentication evidence, ticket expiry/rotation tests,
and denied-user tests. See the [feedback loop](feedback-loops/sharepoint-onprem-connector.md).

References: [Microsoft SharePoint REST files](https://learn.microsoft.com/en-us/sharepoint/dev/sp-add-ins/working-with-folders-and-files-with-rest),
[ResourcePath names](https://learn.microsoft.com/en-us/sharepoint/dev/solution-guidance/supporting-and-in-file-and-folder-with-the-resourcepath-api),
[Kerberos planning](https://learn.microsoft.com/en-us/sharepoint/security-for-sharepoint-server/kerberos-authentication-planning).

# SharePoint Server (on-premises)

`sharepoint_onprem` is a read-only document-library connector using SharePoint's
modern REST API or SharePoint 2010 SOAP services. It is separate from the existing Microsoft Graph SharePoint and
SharePoint Lists connectors. An enterprise license is required.

## Supported workflow

Create a connection → choose a site and library/folder scope → test → attach it
to an agent → browse, search, read documents, and analyze CSV/Excel files in
reports. The connector never writes to SharePoint. Report attachments and saved
queries are stored in BOW, not in the source library.

- All visible document libraries (`*`), one named library, or the default library
  (blank, Modern REST only). SharePoint 2010 requires a name or `*`. With all
  libraries, returned paths include the library name.
- Folder scope, recursive enumeration, include globs, metadata indexing or live
  listing. Reads enforce scope even when given an absolute server-relative ID.
- CSV/TSV and XLS/XLSX as DataFrames; PDF, DOCX, PPTX and text extraction; JSON;
  original binary downloads and the existing BOW document-preview pipeline.
- Modern REST: SharePoint indexed content search plus immediate live filename matching.
  New document **contents require a SharePoint search crawl**. BOW metadata
  indexing does not run that crawl. Search results outside the configured URL
  origin/site/library/folder/globs are rejected, and hits are reauthorized live.
- SharePoint 2010: filename/path search within the enumeration cap, without
  indexed content search. Existing document extraction and CSV/Excel analysis
  run after a file is read.
- Default bounds: 5,000 catalog files and 50 MB per file; maximum configurable
  bounds: 50,000 files and 250 MB. Narrow the scope for larger libraries.

Document-library files are supported; arbitrary SharePoint lists, pages,
attachments to list items, writes, and Microsoft Graph OAuth are not part of
this connector. Modern REST requires the ResourcePath endpoints used by this
connector; the existing live lab uses SharePoint Server Subscription Edition.
SharePoint 2010 mode has automated SOAP-contract and application coverage, but
has not yet been verified on a real 2010 farm.

## SharePoint 2010 setup

Choose **SharePoint API → SharePoint 2010** on the existing **SharePoint Server
(on-prem)** connector. Keep the same Windows authentication choice and account.
Saved connections without this setting retain **Modern REST**. There is no
automatic fallback after a 404: a wrong URL or missing library must not silently
switch protocols.

The site URL must address the SharePoint web containing the library. Put the
library's display name (or GUID) in **Document Library**, and a path relative to
that library in **Folder Path**. Use `*` for all visible document libraries.
A site URL containing the library/folder or a view page such as `AllItems.aspx`
will not work. Enable **Include Subfolders** when needed.

The backend must be able to call these read operations with the configured
identity:

| Endpoint beneath the site URL | Operations | Purpose |
|---|---|---|
| `/_vti_bin/Lists.asmx` | `GetListCollection`, `GetList`, `GetListItems` | Libraries, scope, paged file metadata |
| `/_vti_bin/Copy.asmx` | `GetItem` | File bytes, returned as base64 |

These are SOAP POSTs, but they do not write or create SharePoint content. No
Graph OAuth, SharePlum installation, new credentials, or database migration is
required. Large libraries remain subject to the farm's list-view thresholds;
page size is not a bypass. Narrow **Folder Path** for large libraries. Copy
responses have base64 overhead and are bounded before parsing and after decoding.

### Read-only on-prem smoke test

Run from the deployed backend environment, with its Python dependencies, network
access, CA trust, and existing Windows credentials. Set `SP_SITE_URL`,
`SP_LIBRARY`, `SP_USERNAME`, and `SP_PASSWORD` securely in the environment;
never put the password in a command or report. Compute a known file's SHA-256
independently from its original bytes, then run from the repository root:

```bash
python tools/sharepoint_onprem/verify_2010.py \
  --file 'Reports/known.csv' --sha256 '<64-character-original-file-sha256>' \
  --min-files 3 --require-paging
```

The default diagnostic page size is two. Choose a scope with more than two items
in one folder to exercise paging. `--folder` narrows scope; `--file` then becomes
relative to that folder. Use `--auth kerberos` with the existing keytab/cache
setup (`SP_PRINCIPAL` is optional) for Kerberos. `--allow-http` is only for an
isolated HTTP lab, consistent with the connector's existing setting.

Success requires connection, listing, filename search, and exact downloaded-byte
hash verification. `--require-paging` also requires a successful continuation
request. The output contains counts and booleans, not credentials or file data.
Run again for a Unicode filename, a nested file, and an empty file as applicable.
A restricted account reading a known denied file must fail. No fixture files are
created on SharePoint by this script.

Remaining deployment checks: actual NTLM/Kerberos negotiation, endpoint exposure,
public URL/alternate-access mappings, permissions, and farm query limits. See the
[2010 feedback loop and source comparison](feedback-loops/sharepoint-2010.md).

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
4. For Modern REST indexed content search: Search Service Application, a working content
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

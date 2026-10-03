# SAML 2.0 single sign-on

BOW can authenticate users through a SAML 2.0 identity provider (IdP), including
Microsoft Entra ID or a provider configured with its own metadata and claims.
Configuration lives in `bow-config.yaml`; no Entra-specific protocol code is used.
OIDC, Google, and LDAP remain available alongside SAML in `hybrid` auth mode.

## Supported interoperability profile

BOW is the service provider (SP). It initiates login using an HTTP-Redirect
AuthnRequest and accepts the IdP's HTTP-POST response. Responses signed at the
assertion level, response level, or both are supported. Optional signed
AuthnRequests, encrypted assertions, multiple trusted signing certificates, and
custom identity attribute names are supported. SHA-1 signatures are rejected.

“Provider-neutral” does not mean every SAML binding or optional extension:
unsolicited/IdP-initiated responses, Artifact binding, POST AuthnRequests, and
SAML Single Logout are not implemented. Start sign-in from BOW (an IdP portal
can link to BOW's sign-in page). BOW logout ends/revokes the BOW session; the IdP
session remains. Group/role provisioning is separate: use existing SCIM or
LDAP administration, or assign BOW roles. SAML claims do not grant admin roles.
A SAML assertion is not an OAuth access token for delegated data connections.

Verified integrations: live Entra demo sign-ins for two assigned users, and an
independent local signed-response IdP with different claim names and issuer.
The local regression suite also covers signed requests and encrypted assertions.
Other IdP products must be configured to this profile and tested before rollout;
this is not a claim that every vendor/version has been individually certified.

## Configure an organization

First create the BOW organization through normal instance setup. Obtain its ID
from the authenticated `/api/users/whoami` or `/api/organizations` response.
Set `BOW_CONFIG_PATH` to an absolute YAML path to select a separate configuration.
Secrets should be supplied through environment variables or mounted key files,
never checked into the repository.

```yaml
base_url: https://bow.example.com
auth:
  mode: hybrid
saml_providers:
  - name: company-sso
    enabled: true
    label: Company SSO
    organization_id: ${BOW_SAML_ORGANIZATION_ID}
    idp:
      metadata_url: https://idp.example.com/app/metadata
      # Optional: pin/select an entity when metadata contains multiple IdPs.
      entity_id: https://idp.example.com/issuer
    attributes:
      subject: name_id
      email: mail
      name: displayName
    auto_provision_users: false
```

Use the exact attribute **Name** from an assertion, including a full URI if the
IdP uses one. FriendlyName/display labels are not attribute keys. Email must be
one nonempty valid email address. `name` is optional; an absent display name
falls back to the email local part. `subject: name_id` uses NameID and its
qualifiers; alternatively specify a stable, immutable identity attribute name.
Transient NameID is not accepted as the account identifier.

Each provider name must be a unique lowercase slug across SAML/OIDC/Google.
Every provider belongs to exactly one organization. A valid assertion never
implicitly admits a user to another organization. Restart after YAML changes.
Enabled SAML requires an HTTPS `base_url` origin, without a path/query/fragment.
Use `hybrid` while testing; `sso_only` hides password login except the existing
admin recovery flow. `local_only` does not mount SSO endpoints.

### Exchange metadata with the IdP

For `company-sso`, give the IdP these values:

| Setting | Value |
| --- | --- |
| SP metadata | `https://bow.example.com/api/auth/saml/company-sso/metadata` |
| Entity ID / audience | `https://bow.example.com/saml/company-sso` |
| ACS / Reply URL | `https://bow.example.com/api/auth/saml/company-sso/acs` |
| Sign-on URL | `https://bow.example.com/users/sign-in` |

An explicit `sp.entity_id` overrides the generated Entity ID; the ACS remains
derived from `base_url` and the provider slug. Match scheme, hostname, port, and
path exactly. Configure a stable NameID and assign permitted users to the IdP
application. Leave RelayState and SAML Logout URL unset.

The metadata URL is an operator-controlled HTTPS URL, fetched without redirects,
with a ten-second timeout, two-MiB size limit, and five-minute cache. Metadata
expiry is honored. Downloaded metadata can instead be mounted locally:

```yaml
idp:
  metadata_file: /etc/bow/company-idp.xml
  entity_id: https://idp.example.com/issuer
```

For IdPs without metadata, use explicit trust material:

```yaml
idp:
  entity_id: https://idp.example.com/issuer
  sso_url: https://idp.example.com/sso
  certificates:
    - ${BOW_SAML_IDP_CERTIFICATE}
    - ${BOW_SAML_IDP_NEXT_CERTIFICATE}
```

Certificates are public X.509 signing certificates, not fingerprints. During
rotation trust the old and new certificate together, then remove the old one.
For a mounted metadata file, update the file to publish the new certificate.
BOW never trusts a certificate just because an incoming assertion contains it.

### Admission and existing accounts

With `auto_provision_users: false`, a new identity needs a live, unexpired invite
for its asserted email **in that provider's organization**. A new user takes the
role explicitly assigned by the invitation. With `true`, assigned IdP users can
join that organization as ordinary members; seat limits still apply. Invitations
already reserve a seat and do not require an additional one at acceptance.

Matching an existing BOW email does **not** automatically link accounts. An
operator may explicitly approve a link to an existing organization member:

```yaml
account_links:
  "immutable-idp-subject": existing-member@example.com
```

For `subject: name_id`, the key is the NameID value. Otherwise it is the mapped
subject attribute. The assertion must also have that email, and the BOW user
must already be a member of the configured organization. Inactive accounts,
service accounts, superusers, and LDAP-bound users cannot be linked this way.
Use an ordinary member account for testing. Subsequent logins use the stored
provider/issuer/organization/subject identity, not email as an identity key.
Removing membership does not allow JIT to recreate access on the next login.
Disabling the provider, deactivating the user, or removing membership prevents
use of the associated SAML session.

### Optional signing and encryption

```yaml
sp:
  entity_id: https://bow.example.com/saml/company-sso
  certificate_file: /run/secrets/saml-sp.crt
  private_key_file: /run/secrets/saml-sp.key
  sign_requests: true
  want_assertions_encrypted: true
  name_id_format: urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified
```

Import BOW's SP metadata/certificate into the IdP. Protect the private key with
filesystem access controls. Neither key material nor IdP metadata appears in
public `/api/settings`. Encryption does not replace signature verification.
The default NameID format is Unspecified; change it to match the IdP if needed.

## Run the localhost sandbox

The checked-in example is `configs/bow-config.saml-demo.yaml`. It includes the
public metadata URL for the supplied BOW-SAML Entra test application; it contains
no passwords. The helper creates a new SQLite database, organization, temporary
signing keys, local test IdP, and runtime configuration in the system temporary directory.

Prerequisites: Python 3.12, installed backend/frontend dependencies, Node, and
`mkcert` with its local CA trusted. Stop existing servers on 3000, 8010 and 9443.
From the repository root:

```bash
# One-time local development certificate trust setup:
mkcert -install

# Install dependencies using the repository lockfiles:
(cd backend && uv sync --frozen --extra dev)
(cd frontend && yarn install --frozen-lockfile)

# Self-contained local IdP; no cloud account needed:
backend/.venv/bin/python tools/agent/saml_sandbox.py

# Alternatively include the supplied Entra demo application:
backend/.venv/bin/python tools/agent/saml_sandbox.py --entra
```

Open `https://localhost:3000/users/sign-in`. “Sign in with Local SAML” goes to the
local test IdP; its Continue button sends a real signed assertion to BOW. This
IdP has no authentication and exists only for isolated local testing. Never
expose it outside localhost or deploy it. Ctrl-C stops the sandbox services.

For Entra configure these exact values in Enterprise application → Single
sign-on → SAML:

| Entra field | Local value |
| --- | --- |
| Identifier | `https://localhost:3000/saml/entra-saml` |
| Reply URL | `https://localhost:3000/api/auth/saml/entra-saml/acs` |
| Sign on URL | `https://localhost:3000/users/sign-in` |

Assign test users. The supplied demo users have no populated `mail` attribute,
so this demo maps email to the full `name` claim URI containing their email-shaped
UPN. This is an explicit demo mapping, not an automatic fallback in the SAML
engine. For another tenant, map its populated email attribute instead. Use a
stable subject, such as NameID sourced from `user.objectid` with Unspecified
format. The browser must run on the same machine as the localhost sandbox.

### Playwright verification

With the sandbox running, from the repository root:

```bash
export NODE_EXTRA_CA_CERTS="$(mkcert -CAROOT)/rootCA.pem"
node frontend/tests/saml/local-sso.mjs
```

For live Entra, use the `--entra` sandbox and set `BOW_SAML_DEMO_USERS` to a
comma-separated list of assigned test users and `BOW_SAML_DEMO_PASSWORD` through
your local secret environment. Do not commit or echo the password. Then run:

```bash
node frontend/tests/saml/entra-sso.mjs
```

The live script records only post-login screenshots; it does not retain traces,
HARs, login videos or browser session state. MFA/account remediation may require
a human. The local script records synthetic-user UI evidence under
`media/pr/saml-sso/`.

## Troubleshooting

- **Sign-in button absent:** verify `auth.mode`, provider `enabled`, and that the
  running process loaded the expected `BOW_CONFIG_PATH`.
- **Configuration unavailable:** check the IdP metadata URL/file, signing
  certificates, supported Redirect endpoint, expiry, and exact entity selection.
- **Generic sign-in failure:** start a fresh attempt from BOW. Requests expire
  after five minutes; both RelayState and a browser cookie must match. Check
  safe diagnostic reasons in the backend log; assertions and tokens are not logged.
- **Missing identity attribute:** check full attribute Names and whether the
  actual assigned user's email field is populated.
- **Browser correlation failure:** use trusted HTTPS. The cross-site SAML POST
  requires a Secure, HttpOnly, SameSite=None correlation cookie. Do not substitute
  OIDC's SameSite=Lax cookie policy or disable correlation.
- **Existing email rejected:** approve an explicit subject link for an existing
  organization member, or test with a new account. Do not enable email auto-linking.
- **Entra authenticates but BOW rejects:** verify exact audience/ACS values,
  assertion issuer, claim mapping, organization admission, and stable subject.
- **xmlsec import/library mismatch:** use the locked compatible wheels. Platforms
  building xmlsec from source need libxml2/libxmlsec development libraries and
  matching lxml/xmlsec libxml2 versions.

The executable reproduce→verify record is `docs/feedback-loops/saml-sso.md`.

## Install with Kubernetes
---
You can install Bag of Words on a Kubernetes cluster. The Helm chart can deploy the app with a bundled PostgreSQL instance **or** connect to an external managed database such as AWS Aurora with IAM authentication.

### 1. Add the Helm Repository

```bash
helm repo add bow https://helm.bagofwords.com
helm repo update
```

### 2. Install or Upgrade the Chart

Here are a few examples of how to install or upgrade the Bag of Words Helm chart:

### Deploy with a bundled PostgreSQL instance
```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB>
```

### Deploy without TLS and with a custom hostname
```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
  --set host=<HOST> \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB> \
 --set ingress.tls=false
```

### Deploy with TLS, cert-manager, and Google OAuth
```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 --set host=<HOST> \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB> \
 --set config.googleOauthEnabled=true \
 --set config.googleClientId=<CLIENT_ID> \
 --set config.googleClientSecret=<CLIENT_SECRET>
```

### Deploy with AWS Aurora and IAM Authentication

When using a managed database like AWS Aurora PostgreSQL, the chart skips the bundled PostgreSQL subchart and connects directly to your Aurora cluster. Passwords are generated at runtime using IAM — no static credentials are stored.

**Prerequisites:**
- An Aurora PostgreSQL cluster with IAM database authentication enabled
- A database user with `GRANT rds_iam TO <username>`
- An IAM role with `rds-db:connect` permission
- In EKS: an IRSA (IAM Roles for Service Accounts) annotation on the pod's service account

```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 --set host=<HOST> \
 --set database.auth.provider=aws_iam \
 --set database.auth.region=us-east-1 \
 --set database.auth.sslMode=require \
 --set database.host=<AURORA-CLUSTER-ENDPOINT> \
 --set database.port=5432 \
 --set database.username=<DB-USER> \
 --set database.name=<DB-NAME> \
 --set serviceAccount.annotations.'eks\.amazonaws\.com/role-arn'=arn:aws:iam::<ACCOUNT>:role/<ROLE-NAME>
```

For example, with a real Aurora cluster:
```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 --set host=bow.example.com \
 --set database.auth.provider=aws_iam \
 --set database.auth.region=us-east-1 \
 --set database.auth.sslMode=require \
 --set database.host=bow-pg1-instance-1.cry2og862pqb.us-east-1.rds.amazonaws.com \
 --set database.port=5432 \
 --set database.username=bow_user \
 --set database.name=postgres \
 --set serviceAccount.annotations.'eks\.amazonaws\.com/role-arn'=arn:aws:iam::123456789012:role/bow-rds-role
```

**SSL certificate verification:** The image ships with the public AWS RDS CA bundle. By default, `sslMode=require` encrypts the connection without certificate verification. To enable full certificate verification, set `sslMode=verify-full` — the built-in CA bundle will be used automatically.

To use a **custom CA certificate** (e.g. a private or corporate CA), create a K8s Secret containing the cert and reference it:
```bash
--set database.auth.sslMode=verify-full \
--set database.auth.sslRootCert.secretName=my-rds-ca-cert \
--set database.auth.sslRootCert.key=ca-bundle.pem
```
This mounts the Secret into the pod, overriding the built-in bundle.

### Deploy with Aurora using values.yaml

For Aurora deployments, you can also set all values in a file:

```yaml
# aurora-values.yaml
host: bow.example.com

database:
  auth:
    provider: aws_iam
    region: us-east-1
    sslMode: require
  host: bow-pg1-instance-1.cry2og862pqb.us-east-1.rds.amazonaws.com
  port: 5432
  username: bow_user
  name: postgres

serviceAccount:
  name: bowapp
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/bow-rds-role

config:
  encryptionKey: "<your-encryption-key>"
  baseUrl: "https://bow.example.com"
```

```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 -f aurora-values.yaml
```

### Secrets handling

Sensitive values must be provided via a Kubernetes `Secret`, not via the
ConfigMap that the chart renders by default. The recommended pattern is:

1. Put the placeholder `${ENV_VAR}` in `values.yaml` (or `--set ...`).
2. Create a `Secret` whose keys match those env-var names.
3. Reference it via `--set config.secretRef=<secret-name>`.

Sensitive keys consumed by the app:

| Key                                | Source field in `values.yaml`                | Notes |
| ---                                | ---                                          | --- |
| `BOW_ENCRYPTION_KEY`               | `config.encryptionKey`                       | Required for stable installs — see warning below. |
| `BOW_DATABASE_URL`                 | bundled-Postgres path only                   | Embeds the DB password; prefer a Secret. |
| `BOW_GOOGLE_CLIENT_SECRET`         | `config.googleClientSecret`                  | Plaintext in ConfigMap if set inline. |
| `BOW_GOOGLE_CLIENT_ID`             | `config.googleClientId`                      | Public-ish; treat like a secret if your IdP requires. |
| `BOW_OIDC_<NAME>_CLIENT_SECRET`    | `config.oidcProviders[].clientSecret`        | Use a placeholder per provider (uppercase, alnum + `_`). |
| `BOW_LDAP_BIND_PASSWORD`           | `config.ldap` (never in values)              | Always Secret-only. |
| `BOW_SMTP_PASSWORD`                | `config.smtp.password`                       | Always Secret-only. |
| `BOW_LICENSE_KEY`                  | `config.licenseKey`                          | Always Secret-only. |

> **Encryption key is required.** If neither `config.encryptionKey` nor a
> `BOW_ENCRYPTION_KEY` in your Secret is set, the backend generates a new
> Fernet key on every pod start, which makes previously-encrypted data
> (LLM credentials, OAuth tokens, etc.) unreadable after a restart. Generate
> one once and persist it: `openssl rand -base64 32`.

#### Step-by-step

1. Create the namespace if it doesn't exist:
```bash
kubectl create namespace <namespace>
```

2. Create the `Secret` with the sensitive keys you want to inject:
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: bowapp-secrets
  namespace: <namespace>
stringData:
  # App-wide
  BOW_ENCRYPTION_KEY: "<fernet-key>"
  BOW_DATABASE_URL: "postgresql://<user>:<pass>@<host>:5432/<db>"

  # Google OAuth
  BOW_GOOGLE_CLIENT_SECRET: "<google-client-secret>"

  # OIDC (one per provider name; uppercase + underscores)
  BOW_OIDC_OKTA_CLIENT_SECRET: "<okta-client-secret>"
  BOW_OIDC_ENTRA_CLIENT_SECRET: "<entra-client-secret>"

  # LDAP / Active Directory
  BOW_LDAP_BIND_PASSWORD: "<service-account-password>"

  # SMTP
  BOW_SMTP_PASSWORD: "<smtp-password>"

  # License
  BOW_LICENSE_KEY: "<license-key>"

  # Bundled-Postgres (subchart)
  postgres-password: "<postgres-password>"
```

3. Reference the Secret in your install:
```bash
helm upgrade -i \
  bowapp bow/bagofwords \
 -n <namespace> \
 --set postgresql.auth.existingSecret=bowapp-secrets \
 --set config.secretRef=bowapp-secrets
```

The `Secret` is loaded after the ConfigMap via `envFrom`, so its values
override any plaintext defaults from the ConfigMap. You only need to include
the keys you want to set/override.

### Microsoft Entra (Azure AD) with group sync

```yaml
# values.yaml
config:
  secretRef: bowapp-secrets       # contains BOW_OIDC_ENTRA_CLIENT_SECRET
  authMode: hybrid                # or sso_only

  oidcProviders:
    - name: entra
      enabled: true
      label: "Sign in with Microsoft"
      icon: microsoft
      issuer: https://login.microsoftonline.com/<tenant-id>/v2.0
      clientId: "<entra-client-id>"
      clientSecret: "${BOW_OIDC_ENTRA_CLIENT_SECRET}"
      scopes: ["openid", "profile", "email"]
      pkce: true
      clientAuthMethod: post
      discovery: true
      uidClaim: sub
      # Sync the `groups` claim from the id_token into BOW Groups on login.
      syncGroups: true
      groupClaim: groups
      # Entra returns group object IDs (UUIDs); resolve display names via Graph.
      resolveGroupNames: true
```

### LDAP / Active Directory

```yaml
# values.yaml
config:
  secretRef: bowapp-secrets       # contains BOW_LDAP_BIND_PASSWORD

  ldap:
    enabled: true
    url: ldaps://ad.corp.com:636
    bindDn: "cn=service-account,ou=Services,dc=corp,dc=com"
    useSsl: true
    startTls: false
    baseDn: "dc=corp,dc=com"
    userSearchBase: "ou=Users,dc=corp,dc=com"
    userSearchFilter: "(objectClass=person)"
    userEmailAttribute: mail
    userNameAttribute: displayName
    groupSearchBase: "ou=Groups,dc=corp,dc=com"
    groupSearchFilter: "(objectClass=group)"
    groupNameAttribute: cn
    groupMemberAttribute: member        # "member" (AD/DN) or "memberUid" (OpenLDAP)
    groupMemberFormat: dn               # "dn" or "uid"
    syncIntervalMinutes: 60
    autoProvisionUsers: false
```

The bind password is never accepted via `values.yaml` — it must be set as
`BOW_LDAP_BIND_PASSWORD` in the Secret referenced by `config.secretRef`.


### Service Account annotations
For adding a SA annotation pass the following flag during `helm install` command
`--set serviceAccount.annotations.foo=bar`
Otherwise, set annotations directly in values.yaml file by updating
```yaml
serviceAccount:
  ...
  annotations:
    foo: bar
```

For IRSA (EKS IAM Roles for Service Accounts), annotate with the IAM role ARN:
```bash
--set serviceAccount.annotations.'eks\.amazonaws\.com/role-arn'=arn:aws:iam::<ACCOUNT>:role/<ROLE-NAME>
```

### Configure node selector
For adding a node selector to both the BowApp and the PostgreSQL instance set the following flag during `helm install`
command ` --set postgresql.primary.nodeSelector.'kubernetes\.io/hostname'=kind-control-plane`
Otherwise, set node selector directly in values.yaml
```yaml
postgresql:
  ...
  primary:
    ...
    nodeSelector:
      kubernetes.io/hostname: kind-control-plane
```

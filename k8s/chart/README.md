# bagofwords

![Version: 3.0.0](https://img.shields.io/badge/Version-3.0.0-informational?style=flat-square) ![Type: application](https://img.shields.io/badge/Type-application-informational?style=flat-square) ![AppVersion: 1.0.1](https://img.shields.io/badge/AppVersion-1.0.1-informational?style=flat-square)

Bag of words - a new ai data tool

## Values

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| affinity | object | `{}` | Affinity rules for the app pod. |
| autoscaling.enabled | bool | `false` |  |
| autoscaling.maxReplicas | int | `5` |  |
| autoscaling.minReplicas | int | `1` |  |
| autoscaling.targetCPUUtilizationPercentage | int | `80` |  |
| config | object | `{"allowMultipleOrganizations":false,"allowUninvitedSignups":false,"authMode":"hybrid","baseUrl":"","database":{"idleSessionTimeoutMs":"","maxOverflow":"","poolSize":""},"encryptionKey":"","forwardedAllowIps":"","googleClientId":"","googleClientSecret":"","googleOauthEnabled":false,"intercomEnabled":false,"ldap":{"admissionGroupDn":"","allowLocalSuperuserLogin":false,"autoProvisionUsers":false,"baseDn":"","bindDn":"","caCertsFile":"","connectionTimeout":10,"enabled":false,"groupMemberAttribute":"member","groupMemberFormat":"dn","groupNameAttribute":"cn","groupSearchBase":"","groupSearchFilter":"(objectClass=group)","kerberosRealm":"","organizationId":"","pageSize":500,"startTls":false,"syncIntervalMinutes":60,"url":"","useSsl":true,"userEmailAttribute":"mail","userNameAttribute":"displayName","userSearchBase":"","userSearchFilter":"(objectClass=person)"},"licenseKey":"","oidcProviders":[],"otel":{"enabled":false,"headers":"","protocol":"grpc","serviceName":"bagofwords-backend","tracesEndpoint":"http://localhost:4317"},"secretRef":"","smtp":{"enabled":false,"from_email":"","from_name":"","host":"","password":"","port":587,"use_credentials":true,"use_ssl":false,"use_tls":true,"username":"","validate_certs":true},"telemetryEnabled":true,"uvicornWorkers":"","verifyEmails":false}` | Application configuration. Sensitive fields (`encryptionKey`, `googleClientSecret`, SMTP `password`, OIDC `clientSecret`, LDAP `bindPassword`, `licenseKey`) should be provided via a Kubernetes Secret referenced by `config.secretRef`. Use the `${VAR}` placeholder pattern in these fields; put the real value in the Secret with the matching key. See README.md for details. |
| config.allowMultipleOrganizations | bool | `false` | Allow multiple organizations (multi-tenant mode). |
| config.allowUninvitedSignups | bool | `false` | Allow users to sign up without an invitation. |
| config.authMode | string | `"hybrid"` | Authentication mode. `hybrid` allows both local and SSO login. `local_only` disables SSO. `sso_only` disables local login. |
| config.baseUrl | string | `""` | Public base URL of the application (e.g. `https://app.example.com`). |
| config.database | object | `{"idleSessionTimeoutMs":"","maxOverflow":"","poolSize":""}` | App-side database connection pool, per Uvicorn worker. The ceiling against the server is `(poolSize + maxOverflow) * uvicornWorkers * replicaCount`. Keep that under the server's `max_connections` minus `superuser_reserved_connections` (stock PostgreSQL: 100 - 3 = 97), or connects are refused outright with "remaining connection slots are reserved for roles with the SUPERUSER attribute" — which surfaces to users as a failed agent run, not as a pool timeout. The bundled PostgreSQL (`postgresql.primary.extendedConfiguration`) is sized to match. |
| config.database.idleSessionTimeoutMs | string | `""` | Let the server close pooled connections left idle this long (ms), so a quiet worker's backends drain instead of holding slots for the life of the process. A connection pool never shrinks on its own. `0` disables. REQUIRES PostgreSQL 14+: on older servers the parameter is rejected at connect time and *every* connection fails. Left empty here, which applies `600000` (10 min) only when this chart can be certain the app is talking to the bundled PostgreSQL 17 — that is, it generates the database URL itself and nothing can override it. It stays unset when the URL is delegated elsewhere and the version is therefore unknown: an external `database.host`, an IAM-authenticated `database.auth.provider`, `postgresql.enabled: false`, or a Secret supplying `BOW_DATABASE_URL` via `postgresql.auth.existingSecret` / `config.secretRef`. Set it explicitly on any server you know is 14 or newer to get the same draining behaviour — an explicit value always wins. |
| config.database.maxOverflow | string | `""` | Extra connections a worker may open under load. Empty uses the app default (20). |
| config.database.poolSize | string | `""` | Pooled connections held per worker. Empty uses the app default (20). |
| config.encryptionKey | string | `""` | AES encryption key for sensitive data at rest. Prefer `config.secretRef` key `BOW_ENCRYPTION_KEY`. |
| config.forwardedAllowIps | string | `""` | Proxies trusted to set X-Forwarded-For (IPs/CIDRs, comma-separated), e.g. your ingress controller's pod CIDR. Unset trusts only loopback, so behind an ingress every anonymous visitor shares one rate-limit identity. Use `*` only if the Service is reachable solely through an ingress that overwrites client-sent X-Forwarded-For. |
| config.googleClientId | string | `""` | Google OAuth client ID. |
| config.googleClientSecret | string | `""` | Google OAuth client secret. Prefer `config.secretRef` key `BOW_GOOGLE_CLIENT_SECRET`. |
| config.googleOauthEnabled | bool | `false` | Enable Google OAuth login. |
| config.intercomEnabled | bool | `false` | Enable Intercom in-app chat widget. |
| config.ldap | object | `{"admissionGroupDn":"","allowLocalSuperuserLogin":false,"autoProvisionUsers":false,"baseDn":"","bindDn":"","caCertsFile":"","connectionTimeout":10,"enabled":false,"groupMemberAttribute":"member","groupMemberFormat":"dn","groupNameAttribute":"cn","groupSearchBase":"","groupSearchFilter":"(objectClass=group)","kerberosRealm":"","organizationId":"","pageSize":500,"startTls":false,"syncIntervalMinutes":60,"url":"","useSsl":true,"userEmailAttribute":"mail","userNameAttribute":"displayName","userSearchBase":"","userSearchFilter":"(objectClass=person)"}` | LDAP / Active Directory integration. `bindPassword` is NOT set here — provide it as `BOW_LDAP_BIND_PASSWORD` in the Secret referenced by `config.secretRef`. |
| config.ldap.admissionGroupDn | string | `""` | AD group DN admitting users; nested AD membership is evaluated by AD. |
| config.ldap.allowLocalSuperuserLogin | bool | `false` | Explicit recovery policy, only for unlinked local superusers. |
| config.ldap.autoProvisionUsers | bool | `false` | Automatically create user accounts on first LDAP login. |
| config.ldap.baseDn | string | `""` | Base DN for user and group searches (e.g. `dc=corp,dc=com`). |
| config.ldap.bindDn | string | `""` | Required least-privilege lookup account DN. Anonymous bind is prohibited. |
| config.ldap.caCertsFile | string | `""` | Mounted private CA bundle; empty uses system trust. Certificate checks cannot be disabled. |
| config.ldap.connectionTimeout | int | `10` | LDAP connection timeout in seconds. |
| config.ldap.enabled | bool | `false` | Enable LDAP authentication. |
| config.ldap.groupMemberAttribute | string | `"member"` | LDAP attribute listing group members (`member` for AD/DN, `memberUid` for OpenLDAP). |
| config.ldap.groupMemberFormat | string | `"dn"` | Format of member values in `groupMemberAttribute` (`dn` or `uid`). |
| config.ldap.groupNameAttribute | string | `"cn"` | LDAP attribute that holds the group's display name. |
| config.ldap.groupSearchBase | string | `""` | Base DN for group searches. Defaults to `baseDn` when empty. |
| config.ldap.groupSearchFilter | string | `"(objectClass=group)"` | LDAP filter for group objects. |
| config.ldap.kerberosRealm | string | `""` | AD Kerberos realm, independent of users' email/UPN suffixes. |
| config.ldap.organizationId | string | `""` | BOW organization admitted by this directory integration (required when enabled). |
| config.ldap.pageSize | int | `500` | Maximum number of entries per LDAP page request. |
| config.ldap.startTls | bool | `false` | Upgrade the connection to TLS after connecting (mutually exclusive with `useSsl`). |
| config.ldap.syncIntervalMinutes | int | `60` | How often (in minutes) to sync groups from LDAP. |
| config.ldap.url | string | `""` | LDAP server URL (e.g. `ldaps://ad.corp.com:636`). |
| config.ldap.useSsl | bool | `true` | Use SSL/TLS for the LDAP connection. |
| config.ldap.userEmailAttribute | string | `"mail"` | LDAP attribute that holds the user's email address. |
| config.ldap.userNameAttribute | string | `"displayName"` | LDAP attribute that holds the user's display name. |
| config.ldap.userSearchBase | string | `""` | Base DN for user searches. Defaults to `baseDn` when empty. |
| config.ldap.userSearchFilter | string | `"(objectClass=person)"` | LDAP filter for user objects. |
| config.licenseKey | string | `""` | Enterprise license key. Prefer `config.secretRef` key `BOW_LICENSE_KEY`. Optional: an admin can also paste a key in Settings → License, which is stored in the database and overrides this value without a redeploy. |
| config.oidcProviders | list | `[]` | OpenID Connect providers (Okta, Microsoft Entra, Auth0, etc.). `clientSecret` should always use the `${BOW_OIDC_<NAME>_CLIENT_SECRET}` placeholder — the real secret is provided via `config.secretRef`.  Example — Okta: oidcProviders:   - name: okta     enabled: true     issuer: https://YOUR_OKTA_DOMAIN.okta.com/oauth2/default     clientId: "<okta-client-id>"     clientSecret: "${BOW_OIDC_OKTA_CLIENT_SECRET}"     scopes: ["openid", "profile", "email"]     pkce: true     clientAuthMethod: basic   # basic | post     discovery: true     uidClaim: sub  Example — Microsoft Entra (Azure AD) with group sync: oidcProviders:   - name: entra     enabled: true     label: "Sign in with Microsoft"     icon: microsoft     issuer: https://login.microsoftonline.com/<tenant-id>/v2.0     clientId: "<entra-client-id>"     clientSecret: "${BOW_OIDC_ENTRA_CLIENT_SECRET}"     scopes: ["openid", "profile", "email"]     pkce: true     clientAuthMethod: post     discovery: true     uidClaim: sub     # Sync groups from id_token's `groups` claim into BOW Groups on login.     syncGroups: true     groupClaim: groups     # Entra returns group UUIDs; resolve display names via Microsoft Graph.     resolveGroupNames: true |
| config.otel.enabled | bool | `false` | Enable OpenTelemetry tracing export. |
| config.otel.headers | string | `""` | Extra OTLP headers as comma-separated `key=value` pairs. |
| config.otel.protocol | string | `"grpc"` | OTLP transport protocol (`grpc` or `http/protobuf`). |
| config.otel.serviceName | string | `"bagofwords-backend"` | Service name reported in traces. |
| config.otel.tracesEndpoint | string | `"http://localhost:4317"` | OTLP traces endpoint URL. |
| config.secretRef | string | `""` | Name of a Kubernetes Secret whose keys are injected as environment variables. Use this to supply sensitive values without landing them in the ConfigMap. |
| config.smtp | object | `{"enabled":false,"from_email":"","from_name":"","host":"","password":"","port":587,"use_credentials":true,"use_ssl":false,"use_tls":true,"username":"","validate_certs":true}` | SMTP email relay settings. `enabled` is a Helm-only render gate — the SMTPSettings object has no `enabled` field. Setting `host` also implicitly enables the block. `password` should be provided via `config.secretRef` as `BOW_SMTP_PASSWORD`. |
| config.smtp.enabled | bool | `false` | Enable SMTP email sending. |
| config.smtp.from_email | string | `""` | From address for outgoing emails. |
| config.smtp.from_name | string | `""` | Display name for outgoing emails. |
| config.smtp.host | string | `""` | SMTP server hostname. |
| config.smtp.password | string | `""` | SMTP password. Prefer `config.secretRef` key `BOW_SMTP_PASSWORD`. |
| config.smtp.port | int | `587` | SMTP server port. |
| config.smtp.use_credentials | bool | `true` | Authenticate with the SMTP server. |
| config.smtp.use_ssl | bool | `false` | Use implicit SSL/TLS (port 465). |
| config.smtp.use_tls | bool | `true` | Use STARTTLS. |
| config.smtp.username | string | `""` | SMTP username for authentication. |
| config.smtp.validate_certs | bool | `true` | Validate the server's TLS certificate. |
| config.telemetryEnabled | bool | `true` | Enable anonymous usage telemetry. |
| config.uvicornWorkers | string | `""` | Number of Uvicorn worker processes. Leave empty to use Uvicorn's default. |
| config.verifyEmails | bool | `false` | Require users to verify their email address before logging in. |
| containerSecurityContext | object | `{}` | Security context for the main app container. |
| database | object | `{"auth":{"provider":"password","region":"","sslMode":"","sslRootCert":{"key":"","secretName":""}},"host":"","name":"","port":5432,"username":""}` | Managed database with IAM auth (alternative to the bundled PostgreSQL). When `database.auth.provider` is not `password`, the bundled PostgreSQL is not deployed, the `postgresql` values above are ignored, and the app connects to the external managed database instead. |
| database.auth.provider | string | `"password"` | Authentication provider. `password` uses the bundled PostgreSQL. `aws_iam` connects to an external RDS instance using IAM token authentication. |
| database.auth.region | string | `""` | AWS region for IAM token generation (aws_iam only, e.g. `us-east-1`). |
| database.auth.sslMode | string | `""` | PostgreSQL SSL mode for managed database connections (e.g. `verify-full`). |
| database.auth.sslRootCert.key | string | `""` | Key inside the Secret that holds the CA certificate (e.g. `ca-bundle.pem`). |
| database.auth.sslRootCert.secretName | string | `""` | Name of a Kubernetes Secret containing a custom CA certificate bundle. Leave empty to use the AWS RDS public CA bundle bundled in the image. |
| database.host | string | `""` | Hostname of the managed database. |
| database.name | string | `""` | Database name. |
| database.port | int | `5432` | Database port. |
| database.username | string | `""` | Database user. Must have `GRANT rds_iam` for aws_iam auth. |
| extraContainers | list | `[]` | Sidecar containers to run alongside the main app container. |
| extraEnv | list | `[]` | Extra environment variables injected into the main app container. |
| extraEnvFrom | list | `[]` | Extra envFrom sources (ConfigMap or Secret refs) added after the main ConfigMap/secretRef. |
| extraVolumeMounts | list | `[]` | Extra volume mounts to add to the main app container. |
| extraVolumes | list | `[]` | Extra volumes to add to the pod. |
| host | string | `"app.bagofwords.com"` | Hostname for the Ingress rule (e.g. `app.example.com`). |
| image | object | `{"registry":"docker.io","repository":"bagofwords/bagofwords","tag":"latest"}` | Docker image settings for the bagofwords application container. |
| image.registry | string | `"docker.io"` | Container image registry. |
| image.repository | string | `"bagofwords/bagofwords"` | Container image repository. |
| image.tag | string | `"latest"` | Image tag to deploy. Use a specific version (e.g. `v1.2.3`) in production. |
| ingress | object | `{"annotations":{},"className":"nginx","enabled":true,"tls":{"enabled":false,"secretName":"bowapp-cert"}}` | Ingress resource configuration. Exposes the app over HTTP/HTTPS. |
| ingress.annotations | object | `{}` | Extra annotations to add to the Ingress resource. Example: `nginx.ingress.kubernetes.io/proxy-body-size: "100m"` |
| ingress.className | string | `"nginx"` | IngressClass name (e.g. `nginx`, `alb`). |
| ingress.enabled | bool | `true` | Enable the Ingress resource. |
| ingress.tls.enabled | bool | `false` | Enable TLS on the Ingress. |
| ingress.tls.secretName | string | `"bowapp-cert"` | Name of the TLS Secret (must exist in the same namespace). |
| initContainers | list | `[]` | Init containers to run before the main app container. |
| lifecycle | object | `{}` | Container lifecycle hooks (preStop / postStart). |
| livenessProbe | object | `{"failureThreshold":3,"httpGet":{"path":"/health","port":3000},"initialDelaySeconds":15,"periodSeconds":20,"successThreshold":1,"timeoutSeconds":1}` | Liveness probe. Pod is restarted when this fails. |
| nameOverride | string | `""` | Override for the resource name. Defaults to .Release.Name. Useful when deploying multiple releases of the same chart into one namespace. |
| networkPolicy.egress | list | `[]` |  |
| networkPolicy.enabled | bool | `false` |  |
| networkPolicy.ingress | list | `[]` |  |
| nodeSelector | object | `{}` | Node selector for the app pod. Does NOT affect the bundled PostgreSQL pod (use postgresql.primary.nodeSelector for that). |
| podAnnotations | object | `{}` | Annotations added to every pod (not the Deployment metadata). |
| podDisruptionBudget.enabled | bool | `false` |  |
| podDisruptionBudget.minAvailable | string | `"50%"` |  |
| podSecurityContext | object | `{}` | Security context for the pod (fsGroup, runAsUser, etc.). |
| postgresql | object | see the `postgresql.*` rows | Bundled single-instance PostgreSQL, run from the official `postgres` image. Intended for evaluation and small installs; for production point the app at a managed or operator-run database (`postgresql.enabled: false` plus `database.*`, or a Secret supplying `BOW_DATABASE_URL`). Not deployed when `database.auth.provider` is not `password`. |
| postgresql.auth.database | string | `""` | Application database name. |
| postgresql.auth.existingSecret | string | `""` | Existing Secret holding the passwords under `secretKeys`. When set the chart does not emit `BOW_DATABASE_URL`; supply it via `config.secretRef`. |
| postgresql.auth.password | string | `""` | Application user's password. Ignored when `existingSecret` is set. |
| postgresql.auth.postgresPassword | string | `""` | Password for the `postgres` superuser. Ignored when `existingSecret` is set. Empty keeps the one already stored in the chart's Secret, or generates one on first install. Set it (or use `existingSecret`) when the chart is rendered without cluster access (`helm template`, Argo CD): the stored value cannot be read there, so a new random one would be written on every sync while the database keeps the original. |
| postgresql.auth.secretKeys.adminPasswordKey | string | `"postgres-password"` | Key in the Secret holding the `postgres` superuser's password. |
| postgresql.auth.secretKeys.userPasswordKey | string | `"password"` | Key in the Secret holding the application user's password. |
| postgresql.auth.username | string | `""` | Application database user (created as the database owner, not a superuser). |
| postgresql.enabled | bool | `true` | Deploy the bundled PostgreSQL StatefulSet. |
| postgresql.image.pullPolicy | string | `"IfNotPresent"` | Image pull policy. |
| postgresql.image.registry | string | `"docker.io"` | PostgreSQL image registry. |
| postgresql.image.repository | string | `"library/postgres"` | PostgreSQL image repository (the Docker Official Image). |
| postgresql.image.tag | string | `"17-bookworm"` | Image tag. Stay on major 17 and the Debian 12 (bookworm) base: `config.database.idleSessionTimeoutMs` relies on 14+, a major-version change needs a dump/restore or `pg_upgrade`, and a different glibc can change text collation order and silently corrupt existing indexes. |
| postgresql.imagePullSecrets | list | `[]` | Image pull secrets for the PostgreSQL pod, e.g. `[{name: regcred}]`. |
| postgresql.migrateFromBitnami | bool | `false` | Confirm the one-time move of a chart 2.x database (Bitnami subchart) onto this chart's PostgreSQL. While a release still runs the Bitnami StatefulSet, `helm upgrade` fails until this is `true`, so an unpinned pipeline cannot migrate the database unattended. A backup first is recommended (there is no rollback to 2.x); see the 3.0.0 section of CHANGELOG.md. Not needed for fresh installs or after the first 3.x upgrade. |
| postgresql.primary.affinity | object | `{}` | Affinity rules for the PostgreSQL pod. |
| postgresql.primary.containerSecurityContext | object | `{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]},"readOnlyRootFilesystem":true}` | Container security context for PostgreSQL. |
| postgresql.primary.extendedConfiguration | string | `"max_connections = 300\n"` | Extra `postgresql.conf` lines. Stock PostgreSQL allows 100 connections, 3 of them reserved for superusers. One app worker's pool alone can reach `config.database.poolSize + maxOverflow` (40 by default), and `start.sh` runs up to 4 workers when the node is big enough — so the default app deployment can ask for more than the default server allows, and the server then refuses the connect outright ("remaining connection slots are reserved for roles with the SUPERUSER attribute") rather than queueing. Raise this together with `config.database.*` and `replicaCount`; each connection costs the server memory, so size it, don't max it. |
| postgresql.primary.nodeSelector | object | `{}` | Node selector for the PostgreSQL pod. |
| postgresql.primary.persistence.accessModes | list | `["ReadWriteOnce"]` | Access modes of the data volume. |
| postgresql.primary.persistence.enabled | bool | `true` | Persist data in a PersistentVolumeClaim. `false` uses an emptyDir (data is lost on restart). |
| postgresql.primary.persistence.existingClaim | string | `""` | Use an existing PersistentVolumeClaim instead of creating one. |
| postgresql.primary.persistence.size | string | `"20Gi"` | Size of the data volume. |
| postgresql.primary.persistence.storageClass | string | `""` | StorageClass for the data volume. Empty uses the cluster default; `-` sets `storageClassName: ""`. |
| postgresql.primary.podAnnotations | object | `{}` | Annotations added to the PostgreSQL pod. |
| postgresql.primary.podSecurityContext | object | `{"fsGroup":1001,"fsGroupChangePolicy":"OnRootMismatch","runAsGroup":1001,"runAsNonRoot":true,"runAsUser":1001,"seccompProfile":{"type":"RuntimeDefault"}}` | Pod security context. uid/gid 1001 matches data volumes created by the Bitnami subchart in chart <= 2.x, so they are adopted as-is. |
| postgresql.primary.resources | object | `{"requests":{"cpu":"100m","memory":"256Mi"}}` | Resource requests/limits for the PostgreSQL container. |
| postgresql.primary.terminationGracePeriodSeconds | int | `60` | Seconds PostgreSQL gets to shut down cleanly before it is killed. |
| postgresql.primary.tolerations | list | `[]` | Tolerations for the PostgreSQL pod. |
| readinessProbe | object | `{"failureThreshold":3,"httpGet":{"path":"/health","port":3000},"initialDelaySeconds":5,"periodSeconds":10,"successThreshold":1,"timeoutSeconds":1}` | Readiness probe. Pod is removed from Service endpoints while this fails. |
| replicaCount | int | `1` | Number of pod replicas. |
| resources | object | `{"limits":{"memory":""},"requests":{"cpu":2,"memory":"900Mi"}}` | CPU and memory resource requests and limits for the app container. |
| resources.limits.memory | string | `""` | Memory limit. Leave empty for no limit. |
| resources.requests.cpu | int | `2` | CPU request (e.g. `500m`, `1`, `2`). |
| resources.requests.memory | string | `"900Mi"` | Memory request (e.g. `512Mi`, `900Mi`). |
| revisionHistoryLimit | int | `2` | Number of old ReplicaSets to retain. |
| serviceAccount | object | `{"annotations":{},"imagePullSecret":"","name":"bowapp"}` | ServiceAccount used by the app pod. |
| serviceAccount.annotations | object | `{}` | Annotations added to the ServiceAccount (e.g. for IRSA / Workload Identity). |
| serviceAccount.imagePullSecret | string | `""` | Name of an existing image pull secret to attach to the ServiceAccount. |
| serviceAccount.name | string | `"bowapp"` | Name of the ServiceAccount to create and use for the app pod. |
| startupProbe | object | `{"failureThreshold":30,"httpGet":{"path":"/health","port":3000},"initialDelaySeconds":30,"periodSeconds":10}` | Startup probe. Kubernetes will not route traffic or count the pod ready until this passes. |
| strategy | object | `{}` | Deployment strategy. Leave empty for Kubernetes default (RollingUpdate 25/25). |
| terminationGracePeriodSeconds | int | `30` | Seconds to wait for in-flight requests to drain on pod shutdown. |
| tolerations | list | `[]` | Tolerations for the app pod. |
| topologySpreadConstraints | list | `[]` | Topology spread constraints for the app pod. |

----------------------------------------------
Autogenerated from chart metadata using [helm-docs v1.14.2](https://github.com/norwoodj/helm-docs/releases/v1.14.2)

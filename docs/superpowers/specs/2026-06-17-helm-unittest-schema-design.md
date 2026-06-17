# Helm Unit Tests & Values JSON Schema — Design Spec

**Date:** 2026-06-17
**Status:** Approved
**Scope:** `k8s/chart/` in the bagofwords repo
**Inspiration:** https://github.com/runatlantis/helm-charts/tree/main/charts/atlantis

---

## Goal

Add two quality-of-life improvements to the rebuilt Helm chart:
1. **helm-unittest test suite** — nine test files covering every template, runnable locally and in CI
2. **values.schema.json** — strict JSON Schema that validates types, enums, and constraints on all bowapp values; unknown top-level keys fail validation; postgresql subchart values pass through freely

---

## Section 1 — helm-unittest Tests

### Plugin

`helm-unittest` — install with:
```bash
helm plugin install https://github.com/helm-unittest/helm-unittest
```

Run with:
```bash
helm unittest k8s/chart/
```

### Test files

Nine files in `k8s/chart/tests/`, one per template:

```
k8s/chart/tests/
  deployment_test.yaml
  svc_test.yaml
  ingress_test.yaml
  config_test.yaml
  sa_test.yaml
  hpa_test.yaml
  pdb_test.yaml
  networkpolicy_test.yaml
  servicemonitor_test.yaml
```

### File structure convention

Every test file follows this skeleton:

```yaml
suite: test <resource>
templates:
  - <template.yaml>
release:
  name: test-release
chart:
  appVersion: 0.0.1-test
set:
  postgresql.auth.username: test    # required — config.yaml renders DB URL from these
  postgresql.auth.password: test
  postgresql.auth.database: testdb
tests:
  - it: renders with default values
    asserts:
      - isKind:
          of: <Kind>
      - equal:
          path: metadata.name
          value: test-release           # bowapp.name resolves to release name
      - equal:
          path: metadata.labels["app.kubernetes.io/name"]
          value: test-release
      - equal:
          path: metadata.labels["helm.sh/chart"]
          value: bagofwords-1.2.0
      # ...resource-specific assertions

  - it: is absent when disabled         # only for optional templates
    set:
      <feature>.enabled: false
    asserts:
      - hasDocuments:
          count: 0
```

### Per-template test coverage

#### `deployment_test.yaml`
- Default render: isKind Deployment, name, labels, replicas=1, image from values, `resources.requests.cpu` key present (not hardcoded), `startupProbe.httpGet.path=/health`, `readinessProbe.httpGet.path=/health`, `livenessProbe.httpGet.path=/health`, serviceAccountName, configMapRef name = release name
- Extra: verify `resources.requests.cpu` renders from values (set `resources.requests.cpu: 500m`, assert equal)

#### `svc_test.yaml`
- Default render: isKind Service, name, labels, type=ClusterIP, port=3000, port name=http

#### `ingress_test.yaml`
- Enabled (set `ingress.enabled: true`): isKind Ingress, name, labels, ingressClassName=nginx, host=app.bagofwords.com, backend service name = release name, backend port=3000
- Disabled (default `ingress.enabled: true` in values — actually default is true): test with `ingress.enabled: false` → hasDocuments count: 0
- Note: default values.yaml has `ingress.enabled: true`, so the "absent" test explicitly sets it false

#### `config_test.yaml`
- Default render: isKind ConfigMap, name, labels
- Assert `data.BOW_DATABASE_URL` exists (contains `postgresql://`)
- Assert `data.BOW_AUTH_MODE` = "hybrid"
- Assert `data.BOW_TELEMETRY_ENABLED` = "true"

#### `sa_test.yaml`
- Default render: isKind ServiceAccount, name=bowapp (from `serviceAccount.name` default), labels
- With imagePullSecret set: assert imagePullSecrets[0].name equals the set value
- Without imagePullSecret (default ""): assert `imagePullSecrets` does not exist

#### `hpa_test.yaml`
- Disabled (default): hasDocuments count: 0
- Enabled: isKind HorizontalPodAutoscaler, name, labels, minReplicas, maxReplicas, scaleTargetRef.name = release name, CPU metric present

#### `pdb_test.yaml`
- Disabled (default): hasDocuments count: 0
- Enabled with minAvailable: isKind PodDisruptionBudget, minAvailable="50%", selector matchLabels
- Enabled with maxUnavailable: set `podDisruptionBudget.minAvailable=""` and `podDisruptionBudget.maxUnavailable=1`, assert maxUnavailable=1 and minAvailable notExists

#### `networkpolicy_test.yaml`
- Disabled (default): hasDocuments count: 0
- Enabled: isKind NetworkPolicy, name, labels, podSelector matchLabels

#### `servicemonitor_test.yaml`
- Disabled (default): hasDocuments count: 0
- Enabled: isKind ServiceMonitor, name, labels, endpoints[0].port=http, endpoints[0].path=/metrics, endpoints[0].interval=30s
- With additionalLabels: set `serviceMonitor.additionalLabels.release=prometheus`, assert label present

### Assertion types used

| Assertion | Purpose |
|---|---|
| `isKind` | Resource type check |
| `equal` | Exact value match |
| `contains` | Substring / item presence |
| `notExists` | Field must be absent |
| `hasDocuments` | Document count (0 or 1) |
| `matchRegex` | Pattern match (used for DB URL) |

---

## Section 2 — `values.schema.json`

### Location
`k8s/chart/values.schema.json`

### Schema version
`https://json-schema.org/draft/2019-09/schema` (same as Atlantis)

### Root strictness
`"additionalProperties": false` — any key not defined in `properties` fails `helm lint` and `helm install`.

### postgresql subchart passthrough
```json
"postgresql": {
  "type": "object",
  "additionalProperties": true,
  "description": "Bitnami PostgreSQL subchart values — all keys permitted"
}
```

### No required fields
Every value has a default in `values.yaml`, so no field is `required` at root level.

### Key constraints

| Property path | Type | Constraint |
|---|---|---|
| `image.registry` | string | — |
| `image.repository` | string | — |
| `image.tag` | string | — |
| `host` | string | — |
| `nameOverride` | string | — |
| `replicaCount` | integer | minimum: 1 |
| `revisionHistoryLimit` | integer | minimum: 0 |
| `terminationGracePeriodSeconds` | integer | minimum: 0 |
| `database.port` | integer | — |
| `database.auth.provider` | string | enum: ["password", "aws_iam"] |
| `database.auth.region` | string | — |
| `database.auth.sslMode` | string | — |
| `database.host` | string | — |
| `database.username` | string | — |
| `database.name` | string | — |
| `config.authMode` | string | enum: ["hybrid", "local_only", "sso_only"] |
| `config.otel.protocol` | string | enum: ["grpc", "http/protobuf"] |
| `config.otel.enabled` | boolean | — |
| `config.smtp.port` | integer | — |
| `config.smtp.enabled` | boolean | — |
| `config.smtp.use_tls` | boolean | — |
| `config.smtp.use_ssl` | boolean | — |
| `config.smtp.use_credentials` | boolean | — |
| `config.smtp.validate_certs` | boolean | — |
| `config.ldap.enabled` | boolean | — |
| `config.ldap.useSsl` | boolean | — |
| `config.ldap.startTls` | boolean | — |
| `config.ldap.autoProvisionUsers` | boolean | — |
| `config.ldap.syncIntervalMinutes` | integer | minimum: 1 |
| `config.ldap.connectionTimeout` | integer | minimum: 1 |
| `config.ldap.pageSize` | integer | minimum: 1 |
| `config.googleOauthEnabled` | boolean | — |
| `config.intercomEnabled` | boolean | — |
| `config.telemetryEnabled` | boolean | — |
| `config.allowUninvitedSignups` | boolean | — |
| `config.allowMultipleOrganizations` | boolean | — |
| `config.verifyEmails` | boolean | — |
| `config.oidcProviders` | array | items: object with string name/issuer/clientId/clientSecret |
| `ingress.enabled` | boolean | — |
| `ingress.tls.enabled` | boolean | — |
| `ingress.annotations` | object | additionalProperties: true |
| `serviceAccount.annotations` | object | additionalProperties: true |
| `autoscaling.enabled` | boolean | — |
| `autoscaling.minReplicas` | integer | minimum: 1 |
| `autoscaling.maxReplicas` | integer | minimum: 1 |
| `autoscaling.targetCPUUtilizationPercentage` | integer | minimum: 1, maximum: 100 |
| `autoscaling.targetMemoryUtilizationPercentage` | integer | minimum: 1, maximum: 100 |
| `podDisruptionBudget.enabled` | boolean | — |
| `networkPolicy.enabled` | boolean | — |
| `networkPolicy.ingress` | array | — |
| `networkPolicy.egress` | array | — |
| `serviceMonitor.enabled` | boolean | — |
| `serviceMonitor.additionalLabels` | object | additionalProperties: true |
| `tolerations` | array | — |
| `extraEnv` | array | — |
| `extraContainers` | array | — |
| `initContainers` | array | — |
| `extraVolumes` | array | — |
| `extraVolumeMounts` | array | — |
| `extraEnvFrom` | array | — |
| `topologySpreadConstraints` | array | — |
| `nodeSelector` | object | additionalProperties: true |
| `podAnnotations` | object | additionalProperties: true |
| `podSecurityContext` | object | additionalProperties: true |
| `containerSecurityContext` | object | additionalProperties: true |
| `affinity` | object | additionalProperties: true |
| `strategy` | object | additionalProperties: true |
| `lifecycle` | object | additionalProperties: true |
| `startupProbe` | object | additionalProperties: true |
| `readinessProbe` | object | additionalProperties: true |
| `livenessProbe` | object | additionalProperties: true |
| `resources.requests.cpu` | string or number | — |
| `resources.requests.memory` | string | — |
| `resources.limits.memory` | string | — |

---

## Section 3 — CI/CD (GitHub Actions)

### Location
`.github/workflows/helm-test.yml`

### Trigger
```yaml
on:
  pull_request:
    paths:
      - 'k8s/chart/**'
  push:
    branches:
      - main
    paths:
      - 'k8s/chart/**'
```

### Jobs

#### `helm-lint`
- Install helm (latest stable via `azure/setup-helm`)
- `helm dependency update k8s/chart/`
- `helm lint k8s/chart/`
- `helm lint k8s/chart/ --set ingress.enabled=true`

#### `helm-test`
- Install helm
- Install helm-unittest plugin: `helm plugin install https://github.com/helm-unittest/helm-unittest`
- `helm dependency update k8s/chart/`
- `helm unittest k8s/chart/`

#### `helm-schema`
- Install helm
- `helm dependency update k8s/chart/`
- `helm template test-release k8s/chart/ --set postgresql.auth.username=u --set postgresql.auth.password=p --set postgresql.auth.database=d > /dev/null`
  — validates the schema is not broken by a default render

### Job ordering
`helm-lint` and `helm-schema` run in parallel; `helm-test` runs independently. All three must pass for the workflow to succeed. No deploy step — chart publishing is out of scope.

---

## Section 4 — helm-docs

### Tool
`helm-docs` — generates `k8s/chart/README.md` from `Chart.yaml` + `values.yaml` comments.

### Installation (local)
```bash
brew install helm-docs
```

### Generated file
`k8s/chart/README.md` — auto-generated, committed to git. Do not edit manually.

### Comment format
Values in `values.yaml` are annotated with `# -- <description>` comments immediately above each key. Example:

```yaml
# -- Number of replicas for the bowapp Deployment.
replicaCount: 1

autoscaling:
  # -- Enable Horizontal Pod Autoscaler. When enabled, `replicaCount` is ignored.
  enabled: false
  # -- Minimum number of replicas when HPA is active.
  minReplicas: 1
  # -- Maximum number of replicas when HPA is active.
  maxReplicas: 5
```

### Scope of annotation
All top-level keys and their direct children that have non-obvious semantics. Kubernetes pass-through objects (`nodeSelector`, `affinity`, `tolerations`, `podSecurityContext`, etc.) get a one-line comment pointing to the Kubernetes docs concept. Deeply nested keys (e.g. individual probe fields) are not annotated — the `startupProbe` / `readinessProbe` / `livenessProbe` parent comment explains them.

### CI integration
Add a `helm-docs` check step to the `helm-test` job:
```yaml
- name: Check helm-docs is up to date
  run: |
    helm-docs --dry-run k8s/chart/ > /tmp/expected-readme.md
    diff k8s/chart/README.md /tmp/expected-readme.md
```
If the generated output differs from the committed `README.md`, the CI step fails — enforcing that docs stay in sync with values.

---

## Out of Scope

- Snapshot testing — not used (Atlantis uses it but adds maintenance overhead)
- Chart publishing / OCI push — separate concern
- helm-docs for any chart other than `k8s/chart/`

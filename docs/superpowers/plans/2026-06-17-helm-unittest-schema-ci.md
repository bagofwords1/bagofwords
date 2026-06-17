# Helm Unit Tests, Schema, Docs & CI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add helm-unittest coverage for all 9 templates, a strict `values.schema.json`, helm-docs annotations + generated README, and a GitHub Actions CI workflow.

**Architecture:** Tests live in `k8s/chart/tests/`, one file per template. Schema at `k8s/chart/values.schema.json` is enforced by `helm lint`. Helm-docs generates `k8s/chart/README.md` from `# --` annotations in `values.yaml`. CI at `.github/workflows/helm-test.yml` runs on every PR touching `k8s/chart/**`.

**Tech Stack:** helm-unittest plugin, helm-docs, GitHub Actions, JSON Schema draft 2019-09, Helm 3

---

## Prerequisites (run once before any task)

```bash
helm plugin install https://github.com/helm-unittest/helm-unittest
brew install helm-docs
helm repo add bitnami https://charts.bitnami.com/bitnami
helm dependency update k8s/chart/
```

---

## Known values (referenced throughout)

- Release name used in tests: `test-release`
- `bowapp.name` resolves to `test-release` (nameOverride is empty, falls back to .Release.Name)
- `helm.sh/chart` label value: `bagofwords-1.2.0` (from Chart.yaml name + version)
- ServiceAccount name: `bowapp` (from `serviceAccount.name` default — NOT from bowapp.name helper)
- Default DB URL rendered when postgresql.auth.* are set and database.host is empty:
  `postgresql://test:test@test-release-postgresql:5432/testdb`

---

### Task 1: Test — deployment.yaml

**Files:**
- Create: `k8s/chart/tests/deployment_test.yaml`

- [ ] **Step 1: Create the test file**

```yaml
suite: test deployment
templates:
  - deployment.yaml
release:
  name: test-release
chart:
  appVersion: 0.0.1-test
set:
  postgresql.auth.username: test
  postgresql.auth.password: test
  postgresql.auth.database: testdb
tests:
  - it: renders with default values
    asserts:
      - isKind:
          of: Deployment
      - equal:
          path: metadata.name
          value: test-release
      - equal:
          path: metadata.labels["app.kubernetes.io/name"]
          value: test-release
      - equal:
          path: metadata.labels["helm.sh/chart"]
          value: bagofwords-1.2.0
      - equal:
          path: spec.replicas
          value: 1
      - equal:
          path: spec.template.spec.serviceAccountName
          value: bowapp
      - equal:
          path: spec.template.spec.containers[0].startupProbe.httpGet.path
          value: /health
      - equal:
          path: spec.template.spec.containers[0].readinessProbe.httpGet.path
          value: /health
      - equal:
          path: spec.template.spec.containers[0].livenessProbe.httpGet.path
          value: /health
      - equal:
          path: spec.template.spec.containers[0].envFrom[0].configMapRef.name
          value: test-release

  - it: renders cpu from values not hardcoded
    set:
      resources.requests.cpu: 500m
    asserts:
      - equal:
          path: spec.template.spec.containers[0].resources.requests.cpu
          value: 500m
```

- [ ] **Step 2: Run test**

```bash
helm unittest k8s/chart/ -f tests/deployment_test.yaml
```

Expected: `2 test(s) passed, 0 test(s) failed`

- [ ] **Step 3: Commit**

```bash
git add k8s/chart/tests/deployment_test.yaml
git commit -m "test: helm-unittest for deployment template"
```

---

### Task 2: Test — svc.yaml

**Files:**
- Create: `k8s/chart/tests/svc_test.yaml`

- [ ] **Step 1: Create the test file**

```yaml
suite: test service
templates:
  - svc.yaml
release:
  name: test-release
chart:
  appVersion: 0.0.1-test
set:
  postgresql.auth.username: test
  postgresql.auth.password: test
  postgresql.auth.database: testdb
tests:
  - it: renders with default values
    asserts:
      - isKind:
          of: Service
      - equal:
          path: metadata.name
          value: test-release
      - equal:
          path: metadata.labels["app.kubernetes.io/name"]
          value: test-release
      - equal:
          path: metadata.labels["helm.sh/chart"]
          value: bagofwords-1.2.0
      - equal:
          path: spec.type
          value: ClusterIP
      - equal:
          path: spec.ports[0].port
          value: 3000
      - equal:
          path: spec.ports[0].name
          value: http
```

- [ ] **Step 2: Run test**

```bash
helm unittest k8s/chart/ -f tests/svc_test.yaml
```

Expected: `1 test(s) passed, 0 test(s) failed`

- [ ] **Step 3: Commit**

```bash
git add k8s/chart/tests/svc_test.yaml
git commit -m "test: helm-unittest for service template"
```

---

### Task 3: Test — ingress.yaml

**Files:**
- Create: `k8s/chart/tests/ingress_test.yaml`

Note: `ingress.enabled` defaults to `true` in values.yaml.

- [ ] **Step 1: Create the test file**

```yaml
suite: test ingress
templates:
  - ingress.yaml
release:
  name: test-release
chart:
  appVersion: 0.0.1-test
set:
  postgresql.auth.username: test
  postgresql.auth.password: test
  postgresql.auth.database: testdb
tests:
  - it: renders with default values
    asserts:
      - isKind:
          of: Ingress
      - equal:
          path: metadata.name
          value: test-release
      - equal:
          path: metadata.labels["app.kubernetes.io/name"]
          value: test-release
      - equal:
          path: metadata.labels["helm.sh/chart"]
          value: bagofwords-1.2.0
      - equal:
          path: spec.ingressClassName
          value: nginx
      - equal:
          path: spec.rules[0].host
          value: app.bagofwords.com
      - equal:
          path: spec.rules[0].http.paths[0].backend.service.name
          value: test-release
      - equal:
          path: spec.rules[0].http.paths[0].backend.service.port.number
          value: 3000

  - it: is absent when disabled
    set:
      ingress.enabled: false
    asserts:
      - hasDocuments:
          count: 0
```

- [ ] **Step 2: Run test**

```bash
helm unittest k8s/chart/ -f tests/ingress_test.yaml
```

Expected: `2 test(s) passed, 0 test(s) failed`

- [ ] **Step 3: Commit**

```bash
git add k8s/chart/tests/ingress_test.yaml
git commit -m "test: helm-unittest for ingress template"
```

---

### Task 4: Test — config.yaml

**Files:**
- Create: `k8s/chart/tests/config_test.yaml`

- [ ] **Step 1: Create the test file**

```yaml
suite: test configmap
templates:
  - config.yaml
release:
  name: test-release
chart:
  appVersion: 0.0.1-test
set:
  postgresql.auth.username: test
  postgresql.auth.password: test
  postgresql.auth.database: testdb
tests:
  - it: renders with default values
    asserts:
      - isKind:
          of: ConfigMap
      - equal:
          path: metadata.name
          value: test-release
      - equal:
          path: metadata.labels["app.kubernetes.io/name"]
          value: test-release
      - equal:
          path: metadata.labels["helm.sh/chart"]
          value: bagofwords-1.2.0
      - matchRegex:
          path: data.BOW_DATABASE_URL
          pattern: "^postgresql://test:test@test-release-postgresql:5432/testdb$"
      - equal:
          path: data.BOW_AUTH_MODE
          value: hybrid
      - equal:
          path: data.BOW_TELEMETRY_ENABLED
          value: "true"
```

- [ ] **Step 2: Run test**

```bash
helm unittest k8s/chart/ -f tests/config_test.yaml
```

Expected: `1 test(s) passed, 0 test(s) failed`

- [ ] **Step 3: Commit**

```bash
git add k8s/chart/tests/config_test.yaml
git commit -m "test: helm-unittest for configmap template"
```

---

### Task 5: Test — sa.yaml

**Files:**
- Create: `k8s/chart/tests/sa_test.yaml`

- [ ] **Step 1: Create the test file**

```yaml
suite: test serviceaccount
templates:
  - sa.yaml
release:
  name: test-release
chart:
  appVersion: 0.0.1-test
set:
  postgresql.auth.username: test
  postgresql.auth.password: test
  postgresql.auth.database: testdb
tests:
  - it: renders with default values
    asserts:
      - isKind:
          of: ServiceAccount
      - equal:
          path: metadata.name
          value: bowapp
      - equal:
          path: metadata.labels["app.kubernetes.io/name"]
          value: test-release
      - equal:
          path: metadata.labels["helm.sh/chart"]
          value: bagofwords-1.2.0
      - notExists:
          path: imagePullSecrets

  - it: renders imagePullSecrets when set
    set:
      serviceAccount.imagePullSecret: my-registry-secret
    asserts:
      - equal:
          path: imagePullSecrets[0].name
          value: my-registry-secret
```

- [ ] **Step 2: Run test**

```bash
helm unittest k8s/chart/ -f tests/sa_test.yaml
```

Expected: `2 test(s) passed, 0 test(s) failed`

- [ ] **Step 3: Commit**

```bash
git add k8s/chart/tests/sa_test.yaml
git commit -m "test: helm-unittest for serviceaccount template"
```

---

### Task 6: Test — hpa.yaml

**Files:**
- Create: `k8s/chart/tests/hpa_test.yaml`

- [ ] **Step 1: Create the test file**

```yaml
suite: test horizontalpodautoscaler
templates:
  - hpa.yaml
release:
  name: test-release
chart:
  appVersion: 0.0.1-test
set:
  postgresql.auth.username: test
  postgresql.auth.password: test
  postgresql.auth.database: testdb
tests:
  - it: is absent by default
    asserts:
      - hasDocuments:
          count: 0

  - it: renders when enabled
    set:
      autoscaling.enabled: true
      autoscaling.minReplicas: 2
      autoscaling.maxReplicas: 10
    asserts:
      - isKind:
          of: HorizontalPodAutoscaler
      - equal:
          path: metadata.name
          value: test-release
      - equal:
          path: metadata.labels["app.kubernetes.io/name"]
          value: test-release
      - equal:
          path: spec.minReplicas
          value: 2
      - equal:
          path: spec.maxReplicas
          value: 10
      - equal:
          path: spec.scaleTargetRef.name
          value: test-release
      - equal:
          path: spec.metrics[0].resource.name
          value: cpu
```

- [ ] **Step 2: Run test**

```bash
helm unittest k8s/chart/ -f tests/hpa_test.yaml
```

Expected: `2 test(s) passed, 0 test(s) failed`

- [ ] **Step 3: Commit**

```bash
git add k8s/chart/tests/hpa_test.yaml
git commit -m "test: helm-unittest for hpa template"
```

---

### Task 7: Test — pdb.yaml

**Files:**
- Create: `k8s/chart/tests/pdb_test.yaml`

- [ ] **Step 1: Create the test file**

```yaml
suite: test poddisruptionbudget
templates:
  - pdb.yaml
release:
  name: test-release
chart:
  appVersion: 0.0.1-test
set:
  postgresql.auth.username: test
  postgresql.auth.password: test
  postgresql.auth.database: testdb
tests:
  - it: is absent by default
    asserts:
      - hasDocuments:
          count: 0

  - it: renders with minAvailable when enabled
    set:
      podDisruptionBudget.enabled: true
      podDisruptionBudget.minAvailable: "50%"
    asserts:
      - isKind:
          of: PodDisruptionBudget
      - equal:
          path: metadata.name
          value: test-release
      - equal:
          path: metadata.labels["app.kubernetes.io/name"]
          value: test-release
      - equal:
          path: spec.minAvailable
          value: "50%"
      - equal:
          path: spec.selector.matchLabels["app.kubernetes.io/name"]
          value: test-release

  - it: renders maxUnavailable when minAvailable is cleared
    set:
      podDisruptionBudget.enabled: true
      podDisruptionBudget.minAvailable: ""
      podDisruptionBudget.maxUnavailable: 1
    asserts:
      - equal:
          path: spec.maxUnavailable
          value: 1
      - notExists:
          path: spec.minAvailable
```

- [ ] **Step 2: Run test**

```bash
helm unittest k8s/chart/ -f tests/pdb_test.yaml
```

Expected: `3 test(s) passed, 0 test(s) failed`

- [ ] **Step 3: Commit**

```bash
git add k8s/chart/tests/pdb_test.yaml
git commit -m "test: helm-unittest for pdb template"
```

---

### Task 8: Test — networkpolicy.yaml

**Files:**
- Create: `k8s/chart/tests/networkpolicy_test.yaml`

- [ ] **Step 1: Create the test file**

```yaml
suite: test networkpolicy
templates:
  - networkpolicy.yaml
release:
  name: test-release
chart:
  appVersion: 0.0.1-test
set:
  postgresql.auth.username: test
  postgresql.auth.password: test
  postgresql.auth.database: testdb
tests:
  - it: is absent by default
    asserts:
      - hasDocuments:
          count: 0

  - it: renders when enabled
    set:
      networkPolicy.enabled: true
    asserts:
      - isKind:
          of: NetworkPolicy
      - equal:
          path: metadata.name
          value: test-release
      - equal:
          path: metadata.labels["app.kubernetes.io/name"]
          value: test-release
      - equal:
          path: spec.podSelector.matchLabels["app.kubernetes.io/name"]
          value: test-release
```

- [ ] **Step 2: Run test**

```bash
helm unittest k8s/chart/ -f tests/networkpolicy_test.yaml
```

Expected: `2 test(s) passed, 0 test(s) failed`

- [ ] **Step 3: Commit**

```bash
git add k8s/chart/tests/networkpolicy_test.yaml
git commit -m "test: helm-unittest for networkpolicy template"
```

---

### Task 9: Test — servicemonitor.yaml

**Files:**
- Create: `k8s/chart/tests/servicemonitor_test.yaml`

- [ ] **Step 1: Create the test file**

```yaml
suite: test servicemonitor
templates:
  - servicemonitor.yaml
release:
  name: test-release
chart:
  appVersion: 0.0.1-test
set:
  postgresql.auth.username: test
  postgresql.auth.password: test
  postgresql.auth.database: testdb
tests:
  - it: is absent by default
    asserts:
      - hasDocuments:
          count: 0

  - it: renders when enabled
    set:
      serviceMonitor.enabled: true
    asserts:
      - isKind:
          of: ServiceMonitor
      - equal:
          path: metadata.name
          value: test-release
      - equal:
          path: metadata.labels["app.kubernetes.io/name"]
          value: test-release
      - equal:
          path: spec.endpoints[0].port
          value: http
      - equal:
          path: spec.endpoints[0].path
          value: /metrics
      - equal:
          path: spec.endpoints[0].interval
          value: 30s

  - it: adds additionalLabels to metadata
    set:
      serviceMonitor.enabled: true
      serviceMonitor.additionalLabels.release: prometheus
    asserts:
      - equal:
          path: metadata.labels.release
          value: prometheus
```

- [ ] **Step 2: Run test**

```bash
helm unittest k8s/chart/ -f tests/servicemonitor_test.yaml
```

Expected: `3 test(s) passed, 0 test(s) failed`

- [ ] **Step 3: Run the full test suite**

```bash
helm unittest k8s/chart/
```

Expected: all 9 suites pass, 0 failures.

- [ ] **Step 4: Commit**

```bash
git add k8s/chart/tests/servicemonitor_test.yaml
git commit -m "test: helm-unittest for servicemonitor template"
```

---

### Task 10: values.schema.json

**Files:**
- Create: `k8s/chart/values.schema.json`

- [ ] **Step 1: Create the schema file**

```json
{
  "$schema": "https://json-schema.org/draft/2019-09/schema",
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "image": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "tag": { "type": "string" },
        "registry": { "type": "string" },
        "repository": { "type": "string" }
      }
    },
    "host": { "type": "string" },
    "postgresql": {
      "type": "object",
      "additionalProperties": true,
      "description": "Bitnami PostgreSQL subchart values — all keys permitted"
    },
    "database": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "auth": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "provider": { "type": "string", "enum": ["password", "aws_iam"] },
            "region": { "type": "string" },
            "sslMode": { "type": "string" },
            "sslRootCert": {
              "type": "object",
              "additionalProperties": false,
              "properties": {
                "secretName": { "type": "string" },
                "key": { "type": "string" }
              }
            }
          }
        },
        "host": { "type": "string" },
        "port": { "type": "integer" },
        "username": { "type": "string" },
        "name": { "type": "string" }
      }
    },
    "ingress": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "enabled": { "type": "boolean" },
        "className": { "type": "string" },
        "tls": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "enabled": { "type": "boolean" },
            "secretName": { "type": "string" }
          }
        },
        "annotations": { "type": "object", "additionalProperties": true }
      }
    },
    "serviceAccount": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "name": { "type": "string" },
        "imagePullSecret": { "type": "string" },
        "annotations": { "type": "object", "additionalProperties": true }
      }
    },
    "config": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "secretRef": { "type": "string" },
        "googleOauthEnabled": { "type": "boolean" },
        "googleClientId": { "type": "string" },
        "googleClientSecret": { "type": "string" },
        "encryptionKey": { "type": "string" },
        "intercomEnabled": { "type": "boolean" },
        "telemetryEnabled": { "type": "boolean" },
        "baseUrl": { "type": "string" },
        "authMode": { "type": "string", "enum": ["hybrid", "local_only", "sso_only"] },
        "allowUninvitedSignups": { "type": "boolean" },
        "allowMultipleOrganizations": { "type": "boolean" },
        "verifyEmails": { "type": "boolean" },
        "uvicornWorkers": { "type": ["string", "integer"] },
        "otel": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "enabled": { "type": "boolean" },
            "serviceName": { "type": "string" },
            "tracesEndpoint": { "type": "string" },
            "protocol": { "type": "string", "enum": ["grpc", "http/protobuf"] },
            "headers": { "type": "string" }
          }
        },
        "oidcProviders": {
          "type": "array",
          "items": { "type": "object", "additionalProperties": true }
        },
        "ldap": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "enabled": { "type": "boolean" },
            "url": { "type": "string" },
            "bindDn": { "type": "string" },
            "useSsl": { "type": "boolean" },
            "startTls": { "type": "boolean" },
            "baseDn": { "type": "string" },
            "userSearchBase": { "type": "string" },
            "userSearchFilter": { "type": "string" },
            "userEmailAttribute": { "type": "string" },
            "userNameAttribute": { "type": "string" },
            "groupSearchBase": { "type": "string" },
            "groupSearchFilter": { "type": "string" },
            "groupNameAttribute": { "type": "string" },
            "groupMemberAttribute": { "type": "string" },
            "groupMemberFormat": { "type": "string" },
            "syncIntervalMinutes": { "type": "integer", "minimum": 1 },
            "autoProvisionUsers": { "type": "boolean" },
            "connectionTimeout": { "type": "integer", "minimum": 1 },
            "pageSize": { "type": "integer", "minimum": 1 }
          }
        },
        "licenseKey": { "type": "string" },
        "smtp": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "enabled": { "type": "boolean" },
            "host": { "type": "string" },
            "port": { "type": "integer" },
            "username": { "type": "string" },
            "password": { "type": "string" },
            "from_name": { "type": "string" },
            "from_email": { "type": "string" },
            "use_tls": { "type": "boolean" },
            "use_ssl": { "type": "boolean" },
            "use_credentials": { "type": "boolean" },
            "validate_certs": { "type": "boolean" }
          }
        }
      }
    },
    "resources": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "requests": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "cpu": { "type": ["string", "number"] },
            "memory": { "type": "string" }
          }
        },
        "limits": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "memory": { "type": "string" }
          }
        }
      }
    },
    "nameOverride": { "type": "string" },
    "replicaCount": { "type": "integer", "minimum": 1 },
    "strategy": { "type": "object", "additionalProperties": true },
    "revisionHistoryLimit": { "type": "integer", "minimum": 0 },
    "terminationGracePeriodSeconds": { "type": "integer", "minimum": 0 },
    "nodeSelector": { "type": "object", "additionalProperties": true },
    "tolerations": { "type": "array" },
    "affinity": { "type": "object", "additionalProperties": true },
    "topologySpreadConstraints": { "type": "array" },
    "podAnnotations": { "type": "object", "additionalProperties": true },
    "podSecurityContext": { "type": "object", "additionalProperties": true },
    "containerSecurityContext": { "type": "object", "additionalProperties": true },
    "lifecycle": { "type": "object", "additionalProperties": true },
    "initContainers": { "type": "array" },
    "extraContainers": { "type": "array" },
    "extraEnv": { "type": "array" },
    "extraEnvFrom": { "type": "array" },
    "extraVolumes": { "type": "array" },
    "extraVolumeMounts": { "type": "array" },
    "startupProbe": { "type": "object", "additionalProperties": true },
    "readinessProbe": { "type": "object", "additionalProperties": true },
    "livenessProbe": { "type": "object", "additionalProperties": true },
    "autoscaling": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "enabled": { "type": "boolean" },
        "minReplicas": { "type": "integer", "minimum": 1 },
        "maxReplicas": { "type": "integer", "minimum": 1 },
        "targetCPUUtilizationPercentage": { "type": "integer", "minimum": 1, "maximum": 100 },
        "targetMemoryUtilizationPercentage": { "type": "integer", "minimum": 1, "maximum": 100 }
      }
    },
    "podDisruptionBudget": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "enabled": { "type": "boolean" },
        "minAvailable": { "type": ["string", "integer"] },
        "maxUnavailable": { "type": ["string", "integer"] }
      }
    },
    "networkPolicy": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "enabled": { "type": "boolean" },
        "ingress": { "type": "array" },
        "egress": { "type": "array" }
      }
    },
    "serviceMonitor": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "enabled": { "type": "boolean" },
        "interval": { "type": "string" },
        "path": { "type": "string" },
        "port": { "type": "string" },
        "additionalLabels": { "type": "object", "additionalProperties": true }
      }
    }
  }
}
```

- [ ] **Step 2: Validate with helm lint**

```bash
helm lint k8s/chart/
```

Expected: `1 chart(s) linted, 0 chart(s) failed`

- [ ] **Step 3: Verify a bad value is rejected**

```bash
helm lint k8s/chart/ --set replicaCount=-1 2>&1 | grep -i schema
```

Expected: output contains a schema validation error about `replicaCount`.

- [ ] **Step 4: Verify a bad enum is rejected**

```bash
helm lint k8s/chart/ --set config.authMode=invalid 2>&1 | grep -i schema
```

Expected: output contains a schema validation error about `authMode`.

- [ ] **Step 5: Commit**

```bash
git add k8s/chart/values.schema.json
git commit -m "feat: add strict values.schema.json (additionalProperties: false at root)"
```

---

### Task 11: helm-docs annotations + README

**Files:**
- Modify: `k8s/chart/values.yaml` (add `# --` comments to original keys, lines 1–169)
- Create: `k8s/chart/README.md` (generated by helm-docs — do not edit manually)

**Context:** Lines 171–330 already have `# --` annotations (added during the chart rebuild). Only lines 1–169 (image, host, postgresql, database, ingress, serviceAccount, config, resources) need annotations added.

- [ ] **Step 1: Replace the top of values.yaml (lines 1 through end of resources block) with the annotated version**

The content to write starts at line 1 and ends just before the `nameOverride` block. Replace that entire section with:

```yaml
# -- Docker image settings for the bagofwords application container.
image:
  # -- Image tag to deploy. Use a specific version (e.g. `v1.2.3`) in production.
  tag: latest
  # -- Container image registry.
  registry: docker.io
  # -- Container image repository.
  repository: bagofwords/bagofwords

# -- Hostname for the Ingress rule (e.g. `app.example.com`).
host: app.bagofwords.com

# -- Bitnami PostgreSQL subchart values. All subchart keys are accepted here.
# @default -- see subchart defaults
postgresql:
  auth:
    username: ""
    password: ""
    database: ""
    existingSecret: ""
  primary:
    persistence:
      size: 20Gi
    nodeSelector: {}

# -- Managed database with IAM auth (alternative to the bundled postgresql subchart).
# When `database.auth.provider` is not `password`, the postgresql subchart values
# above are ignored and the app connects to the external managed database instead.
database:
  auth:
    # -- Authentication provider. `password` uses the bundled postgresql subchart.
    # `aws_iam` connects to an external RDS instance using IAM token authentication.
    provider: password   # password | aws_iam
    # -- AWS region for IAM token generation (aws_iam only, e.g. `us-east-1`).
    region: ""
    # -- PostgreSQL SSL mode for managed database connections (e.g. `verify-full`).
    sslMode: ""
    sslRootCert:
      # -- Name of a Kubernetes Secret containing a custom CA certificate bundle.
      # Leave empty to use the AWS RDS public CA bundle bundled in the image.
      secretName: ""
      # -- Key inside the Secret that holds the CA certificate (e.g. `ca-bundle.pem`).
      key: ""
  # -- Hostname of the managed database.
  host: ""
  # -- Database port.
  port: 5432
  # -- Database user. Must have `GRANT rds_iam` for aws_iam auth.
  username: ""
  # -- Database name.
  name: ""

ingress:
  # -- Enable the Ingress resource.
  enabled: true
  # -- IngressClass name (e.g. `nginx`, `alb`).
  className: nginx
  tls:
    # -- Enable TLS on the Ingress.
    enabled: false
    # -- Name of the TLS Secret (must exist in the same namespace).
    secretName: bowapp-cert
  # -- Extra annotations to add to the Ingress resource.
  # Example: `nginx.ingress.kubernetes.io/proxy-body-size: "100m"`
  annotations: {}

serviceAccount:
  # -- Name of the ServiceAccount to create and use for the app pod.
  name: bowapp
  # -- Name of an existing image pull secret to attach to the ServiceAccount.
  imagePullSecret: ""
  # -- Annotations added to the ServiceAccount (e.g. for IRSA / Workload Identity).
  annotations: {}

# -- Application configuration.
# Sensitive fields (`encryptionKey`, `googleClientSecret`, SMTP `password`,
# OIDC `clientSecret`, LDAP `bindPassword`, `licenseKey`) should be provided
# via a Kubernetes Secret referenced by `config.secretRef`. Use the `${VAR}`
# placeholder pattern in these fields; put the real value in the Secret with
# the matching key. See README.md for details.
config:
  # -- Name of a Kubernetes Secret whose keys are injected as environment variables.
  # Use this to supply sensitive values without landing them in the ConfigMap.
  secretRef: ""
  # -- Enable Google OAuth login.
  googleOauthEnabled: false
  # -- Google OAuth client ID.
  googleClientId: ""
  # -- Google OAuth client secret. Prefer `config.secretRef` key `BOW_GOOGLE_CLIENT_SECRET`.
  googleClientSecret: ""
  # -- AES encryption key for sensitive data at rest. Prefer `config.secretRef` key `BOW_ENCRYPTION_KEY`.
  encryptionKey: ""
  # -- Enable Intercom in-app chat widget.
  intercomEnabled: false
  # -- Enable anonymous usage telemetry.
  telemetryEnabled: true
  # -- Public base URL of the application (e.g. `https://app.example.com`).
  baseUrl: ""
  # -- Authentication mode. `hybrid` allows both local and SSO login.
  # `local_only` disables SSO. `sso_only` disables local login.
  authMode: "hybrid"   # hybrid | local_only | sso_only
  # -- Allow users to sign up without an invitation.
  allowUninvitedSignups: false
  # -- Allow multiple organizations (multi-tenant mode).
  allowMultipleOrganizations: false
  # -- Require users to verify their email address before logging in.
  verifyEmails: false
  # -- Number of Uvicorn worker processes. Leave empty to use Uvicorn's default.
  uvicornWorkers: ""
  otel:
    # -- Enable OpenTelemetry tracing export.
    enabled: false
    # -- Service name reported in traces.
    serviceName: "bagofwords-backend"
    # -- OTLP traces endpoint URL.
    tracesEndpoint: "http://localhost:4317"
    # -- OTLP transport protocol (`grpc` or `http/protobuf`).
    protocol: "grpc" # grpc or http/protobuf
    # -- Extra OTLP headers as comma-separated `key=value` pairs.
    headers: "" # format: key1=value1,key2=value2

  # -- OpenID Connect providers (Okta, Microsoft Entra, Auth0, etc.).
  # `clientSecret` should always use the `${BOW_OIDC_<NAME>_CLIENT_SECRET}` placeholder
  # — the real secret is provided via `config.secretRef`.
  #
  # Example — Okta:
  # oidcProviders:
  #   - name: okta
  #     enabled: true
  #     issuer: https://YOUR_OKTA_DOMAIN.okta.com/oauth2/default
  #     clientId: "<okta-client-id>"
  #     clientSecret: "${BOW_OIDC_OKTA_CLIENT_SECRET}"
  #     scopes: ["openid", "profile", "email"]
  #     pkce: true
  #     clientAuthMethod: basic   # basic | post
  #     discovery: true
  #     uidClaim: sub
  #
  # Example — Microsoft Entra (Azure AD) with group sync:
  # oidcProviders:
  #   - name: entra
  #     enabled: true
  #     label: "Sign in with Microsoft"
  #     icon: microsoft
  #     issuer: https://login.microsoftonline.com/<tenant-id>/v2.0
  #     clientId: "<entra-client-id>"
  #     clientSecret: "${BOW_OIDC_ENTRA_CLIENT_SECRET}"
  #     scopes: ["openid", "profile", "email"]
  #     pkce: true
  #     clientAuthMethod: post
  #     discovery: true
  #     uidClaim: sub
  #     # Sync groups from id_token's `groups` claim into BOW Groups on login.
  #     syncGroups: true
  #     groupClaim: groups
  #     # Entra returns group UUIDs; resolve display names via Microsoft Graph.
  #     resolveGroupNames: true
  oidcProviders: []

  # -- LDAP / Active Directory integration.
  # `bindPassword` is NOT set here — provide it as `BOW_LDAP_BIND_PASSWORD` in
  # the Secret referenced by `config.secretRef`.
  ldap:
    # -- Enable LDAP authentication.
    enabled: false
    # -- LDAP server URL (e.g. `ldaps://ad.corp.com:636`).
    url: ""
    # -- Service account DN for binding (optional — omit for anonymous bind).
    bindDn: ""
    # -- Use SSL/TLS for the LDAP connection.
    useSsl: true
    # -- Upgrade the connection to TLS after connecting (mutually exclusive with `useSsl`).
    startTls: false
    # -- Base DN for user and group searches (e.g. `dc=corp,dc=com`).
    baseDn: ""
    # -- Base DN for user searches. Defaults to `baseDn` when empty.
    userSearchBase: ""
    # -- LDAP filter for user objects.
    userSearchFilter: "(objectClass=person)"
    # -- LDAP attribute that holds the user's email address.
    userEmailAttribute: "mail"
    # -- LDAP attribute that holds the user's display name.
    userNameAttribute: "displayName"
    # -- Base DN for group searches. Defaults to `baseDn` when empty.
    groupSearchBase: ""
    # -- LDAP filter for group objects.
    groupSearchFilter: "(objectClass=group)"
    # -- LDAP attribute that holds the group's display name.
    groupNameAttribute: "cn"
    # -- LDAP attribute listing group members (`member` for AD/DN, `memberUid` for OpenLDAP).
    groupMemberAttribute: "member"
    # -- Format of member values in `groupMemberAttribute` (`dn` or `uid`).
    groupMemberFormat: "dn"
    # -- How often (in minutes) to sync groups from LDAP.
    syncIntervalMinutes: 60
    # -- Automatically create user accounts on first LDAP login.
    autoProvisionUsers: false
    # -- LDAP connection timeout in seconds.
    connectionTimeout: 10
    # -- Maximum number of entries per LDAP page request.
    pageSize: 500

  # -- Enterprise license key. Prefer `config.secretRef` key `BOW_LICENSE_KEY`.
  licenseKey: ""

  # -- SMTP email relay settings.
  # `enabled` is a Helm-only render gate — the SMTPSettings object has no `enabled` field.
  # Setting `host` also implicitly enables the block. `password` should be provided
  # via `config.secretRef` as `BOW_SMTP_PASSWORD`.
  smtp:
    # -- Enable SMTP email sending.
    enabled: false
    # -- SMTP server hostname.
    host: ""
    # -- SMTP server port.
    port: 587
    # -- SMTP username for authentication.
    username: ""
    # -- SMTP password. Prefer `config.secretRef` key `BOW_SMTP_PASSWORD`.
    password: ""
    # -- Display name for outgoing emails.
    from_name: ""
    # -- From address for outgoing emails.
    from_email: ""
    # -- Use STARTTLS.
    use_tls: true
    # -- Use implicit SSL/TLS (port 465).
    use_ssl: false
    # -- Authenticate with the SMTP server.
    use_credentials: true
    # -- Validate the server's TLS certificate.
    validate_certs: true



# -- CPU and memory resource requests and limits for the app container.
resources:
  requests:
    # -- CPU request (e.g. `500m`, `1`, `2`).
    cpu: 2
    # -- Memory request (e.g. `512Mi`, `900Mi`).
    memory: 900Mi
  limits:
    # -- Memory limit. Leave empty for no limit.
    memory: ""
```

Everything after the resources block (from `nameOverride: ""` onward) stays unchanged.

- [ ] **Step 2: Verify no rendering errors after editing**

```bash
helm lint k8s/chart/
```

Expected: `1 chart(s) linted, 0 chart(s) failed`

- [ ] **Step 3: Generate the README**

```bash
helm-docs k8s/chart/
```

This writes `k8s/chart/README.md`. Do not edit it manually.

- [ ] **Step 4: Spot-check the README**

```bash
head -60 k8s/chart/README.md
```

You should see a parameters table with `Key`, `Type`, `Default`, `Description` columns populated from the `# --` comments.

- [ ] **Step 5: Commit both files**

```bash
git add k8s/chart/values.yaml k8s/chart/README.md
git commit -m "docs: add helm-docs annotations to values.yaml and generate README"
```

---

### Task 12: CI/CD GitHub Actions workflow

**Files:**
- Create: `.github/workflows/helm-test.yml`

- [ ] **Step 1: Ensure the directory exists**

```bash
ls .github/workflows/ 2>/dev/null || mkdir -p .github/workflows
```

- [ ] **Step 2: Create the workflow file**

```yaml
name: Helm Chart Tests

on:
  pull_request:
    paths:
      - 'k8s/chart/**'
      - '.github/workflows/helm-test.yml'
  push:
    branches:
      - main
    paths:
      - 'k8s/chart/**'

jobs:
  helm-lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install Helm
        uses: azure/setup-helm@v4

      - name: Add Bitnami repo
        run: helm repo add bitnami https://charts.bitnami.com/bitnami

      - name: Update dependencies
        run: helm dependency update k8s/chart/

      - name: Lint (default values)
        run: helm lint k8s/chart/

      - name: Lint (ingress enabled)
        run: helm lint k8s/chart/ --set ingress.enabled=true --set host=test.example.com

  helm-test:
    name: Unit Tests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install Helm
        uses: azure/setup-helm@v4

      - name: Install helm-unittest plugin
        run: helm plugin install https://github.com/helm-unittest/helm-unittest

      - name: Add Bitnami repo
        run: helm repo add bitnami https://charts.bitnami.com/bitnami

      - name: Update dependencies
        run: helm dependency update k8s/chart/

      - name: Run unit tests
        run: helm unittest k8s/chart/

  helm-schema:
    name: Schema Smoke Test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install Helm
        uses: azure/setup-helm@v4

      - name: Add Bitnami repo
        run: helm repo add bitnami https://charts.bitnami.com/bitnami

      - name: Update dependencies
        run: helm dependency update k8s/chart/

      - name: Template with default values
        run: |
          helm template test-release k8s/chart/ \
            --set postgresql.auth.username=u \
            --set postgresql.auth.password=p \
            --set postgresql.auth.database=d \
            > /dev/null

  helm-docs:
    name: Docs Up-to-Date
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install helm-docs
        run: |
          HELM_DOCS_VERSION=$(curl -s https://api.github.com/repos/norwoodj/helm-docs/releases/latest \
            | grep '"tag_name"' | sed 's/.*"v\([^"]*\)".*/\1/')
          curl -sSL "https://github.com/norwoodj/helm-docs/releases/download/v${HELM_DOCS_VERSION}/helm-docs_${HELM_DOCS_VERSION}_Linux_x86_64.tar.gz" \
            | tar xz helm-docs
          sudo mv helm-docs /usr/local/bin/

      - name: Check README is up to date
        run: |
          helm-docs k8s/chart/
          if ! git diff --exit-code k8s/chart/README.md; then
            echo "README.md is out of sync. Run 'helm-docs k8s/chart/' locally and commit."
            exit 1
          fi
```

- [ ] **Step 3: Verify valid YAML**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/helm-test.yml'))" && echo "Valid YAML"
```

Expected: `Valid YAML`

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/helm-test.yml
git commit -m "ci: add helm lint, unittest, schema smoke test, and docs-sync workflow"
```

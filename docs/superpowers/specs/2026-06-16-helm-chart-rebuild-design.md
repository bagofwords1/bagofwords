# Helm Chart Rebuild — Design Spec

**Date:** 2026-06-16
**Status:** Approved
**Scope:** `k8s/chart/` in the bagofwords repo

---

## Goal

Rebuild the bagofwords Helm chart as a standalone chart (no libra-application dependency) that adopts libra-application's patterns and structure. All existing values keys are preserved exactly — no breakage for current releases. Every new feature is opt-in and disabled by default.

---

## File Structure

### New files
```
k8s/chart/templates/_helpers.tpl
k8s/chart/templates/hpa.yaml
k8s/chart/templates/pdb.yaml
k8s/chart/templates/networkpolicy.yaml
k8s/chart/templates/servicemonitor.yaml
```

### Rewritten (renamed from .yml → .yaml)
```
k8s/chart/templates/deployment.yaml
k8s/chart/templates/svc.yaml
k8s/chart/templates/ingress.yaml
k8s/chart/templates/config.yaml
k8s/chart/templates/sa.yaml
k8s/chart/templates/NOTES.txt   (updated, extension unchanged)
```

### Old files to delete
```
k8s/chart/templates/deployment.yml
k8s/chart/templates/svc.yml
k8s/chart/templates/ingress.yml
k8s/chart/templates/config.yml
k8s/chart/templates/sa.yml
```

### Updated
```
k8s/chart/values.yaml    (new sections appended, nothing removed)
k8s/chart/Chart.yaml     (version bump to 1.2.0)
```

---

## Section 1 — Naming & Helpers (`_helpers.tpl`)

### Helpers defined

| Helper | Returns |
|---|---|
| `bowapp.name` | `default .Release.Name .Values.nameOverride \| trunc 63 \| trimSuffix "-"` |
| `bowapp.fullname` | `printf "%s-%s" .Release.Name .Chart.Name \| trunc 63 \| trimSuffix "-"` — used for sub-resources |
| `bowapp.chart` | `printf "%s-%s" .Chart.Name .Chart.Version \| replace "+" "_" \| trunc 63 \| trimSuffix "-"` |
| `bowapp.labels` | Standard label block (see below) |
| `bowapp.selectorLabels` | `app.kubernetes.io/name` + `app.kubernetes.io/instance` |

### Standard label block (`bowapp.labels`)
```yaml
helm.sh/chart: <chart-name-version>
app.kubernetes.io/name: <bowapp.name>
app.kubernetes.io/instance: <.Release.Name>
app.kubernetes.io/version: <.Chart.AppVersion>
app.kubernetes.io/managed-by: <.Release.Service>
app: <bowapp.name>
```

Applied to metadata.labels on: Deployment, Service, Ingress, ConfigMap, ServiceAccount, HPA, PDB, NetworkPolicy, ServiceMonitor.

Pod template labels use `bowapp.selectorLabels` + `app` + `version` (AppVersion).

### Backwards compatibility
Any release installed as `helm install bowapp ...` resolves `bowapp.name` to `bowapp` — DNS names, selectors, and ConfigMap references are unchanged.

A new optional `nameOverride` value (empty string default) allows overriding the name without changing the release name.

---

## Section 2 — Deployment Rewrite

### Bugs fixed
- **CPU hardcoded to `2`** in the template → replaced with `{{ .Values.resources.requests.cpu }}`.
- **`nodeSelector` reading from `postgresql.primary.nodeSelector`** (wrong) → removed. App pod node selection is now via the new top-level `nodeSelector` key.

### Preserved values (unchanged keys, unchanged behaviour)
- `image.*`
- `resources.requests.memory`, `resources.limits.memory`, `resources.requests.cpu`
- `serviceAccount.name`, `serviceAccount.imagePullSecret`, `serviceAccount.annotations`
- `config.secretRef`
- `database.auth.sslRootCert.*` volume mount

### New values added to `values.yaml`

```yaml
nameOverride: ""          # overrides .Release.Name for resource naming

replicaCount: 1
revisionHistoryLimit: 2
terminationGracePeriodSeconds: 30

strategy: {}
# e.g.:
#   type: RollingUpdate
#   rollingUpdate:
#     maxSurge: 1
#     maxUnavailable: 0

nodeSelector: {}          # app pod only (postgresql keeps postgresql.primary.nodeSelector)
tolerations: []
affinity: {}
topologySpreadConstraints: []

podAnnotations: {}
podSecurityContext: {}
containerSecurityContext: {}

lifecycle: {}
# e.g.:
#   preStop:
#     exec:
#       command: ["/bin/sh", "-c", "sleep 5"]

initContainers: []
extraContainers: []

extraEnv: []
# e.g.:
#   - name: MY_VAR
#     value: "foo"
#   - name: FROM_SECRET
#     valueFrom:
#       secretKeyRef:
#         name: my-secret
#         key: my-key

extraEnvFrom: []
# e.g.:
#   - configMapRef:
#       name: extra-config
#   - secretRef:
#       name: extra-secret

extraVolumes: []
extraVolumeMounts: []

# Probes — same defaults as current hardcoded values, now configurable
startupProbe:
  httpGet:
    path: /health
    port: 3000
  initialDelaySeconds: 30
  periodSeconds: 10
  failureThreshold: 30

readinessProbe:
  httpGet:
    path: /health
    port: 3000
  initialDelaySeconds: 5
  periodSeconds: 10
  failureThreshold: 3
  successThreshold: 1
  timeoutSeconds: 1

livenessProbe:
  httpGet:
    path: /health
    port: 3000
  initialDelaySeconds: 15
  periodSeconds: 20
  failureThreshold: 3
  successThreshold: 1
  timeoutSeconds: 1
```

### Template rendering order (deployment.yaml containers block)
1. `initContainers` (if any)
2. Main `bowapp` container
   - `envFrom`: ConfigMap ref → optional secretRef (existing) → `extraEnvFrom`
   - `env`: `extraEnv`
   - `volumeMounts`: bow-config, optional db-ca-cert, `extraVolumeMounts`
   - probes: startupProbe, readinessProbe, livenessProbe
   - resources, containerSecurityContext, lifecycle
3. `extraContainers` (sidecars)

### Volumes block
- bow-config ConfigMap (existing)
- db-ca-cert Secret (existing, conditional)
- `extraVolumes`

---

## Section 3 — Service Rewrite (`svc.yaml`)

Minimal changes:
- Names via `bowapp.name` helper.
- Labels via `bowapp.labels`.
- Add `name: http` to the port definition so ServiceMonitor can reference it by name.
- All existing values (`service.type` if added, port 3000) preserved.

No new values needed for service itself (type stays ClusterIP, port stays 3000).

---

## Section 4 — Ingress Rewrite (`ingress.yaml`)

Minimal changes:
- Names and labels via helpers.
- Existing values (`ingress.enabled`, `ingress.className`, `host`, `ingress.tls.*`, `ingress.annotations`) unchanged.

---

## Section 5 — ConfigMap Rewrite (`config.yaml`)

- Name via `bowapp.name` helper (resolves to same string for existing releases).
- Labels via `bowapp.labels`.
- All bowapp-specific config content (BOW_* env vars, bowConfig YAML block, OIDC/LDAP/SMTP rendering) preserved verbatim.

---

## Section 6 — ServiceAccount Rewrite (`sa.yaml`)

- Labels via `bowapp.labels`.
- Existing values (`serviceAccount.name`, `serviceAccount.imagePullSecret`, `serviceAccount.annotations`) unchanged.

---

## Section 7 — New Templates

### HPA (`hpa.yaml`)

Conditional on `autoscaling.enabled`. Targets the Deployment by `bowapp.name`.

```yaml
autoscaling:
  enabled: false
  minReplicas: 1
  maxReplicas: 5
  targetCPUUtilizationPercentage: 80
  targetMemoryUtilizationPercentage: ""   # omitted from spec when empty
```

Uses `autoscaling/v2` API (available since Kubernetes 1.23, well within any current cluster).

### PDB (`pdb.yaml`)

Conditional on `podDisruptionBudget.enabled`. Exactly one of `minAvailable` / `maxUnavailable` is rendered (minAvailable takes precedence when both set).

```yaml
podDisruptionBudget:
  enabled: false
  minAvailable: "50%"
  maxUnavailable: ""
```

Uses `policy/v1` API (Kubernetes 1.21+).

### NetworkPolicy (`networkpolicy.yaml`)

Conditional on `networkPolicy.enabled`. Ingress and egress rules are raw Kubernetes rule objects — the user supplies them directly.

```yaml
networkPolicy:
  enabled: false
  ingress: []
  egress: []
```

### ServiceMonitor (`servicemonitor.yaml`)

Conditional on `serviceMonitor.enabled`. Targets the Service on the `http` named port.

```yaml
serviceMonitor:
  enabled: false
  interval: "30s"
  path: /metrics
  port: http
  additionalLabels: {}   # e.g. release: prometheus — must match Prometheus operator selector
```

---

## Section 8 — Chart.yaml

- Version bumped: `1.1.0` → `1.2.0`
- `appVersion` left unchanged (tracks the application, not the chart)
- No new dependencies added

---

## Backwards Compatibility Guarantee

| Existing value key | Status |
|---|---|
| `image.*` | Unchanged |
| `host` | Unchanged |
| `postgresql.*` | Unchanged (subchart passthrough) |
| `database.*` | Unchanged |
| `ingress.*` | Unchanged |
| `serviceAccount.*` | Unchanged |
| `config.*` | Unchanged |
| `resources.*` | Unchanged (CPU bug fixed — was ignored before) |

Any `helm upgrade` using an existing values file will produce the same resources as before, plus the standard labels. The only observable difference for existing releases is the addition of `app.kubernetes.io/*` labels and `helm.sh/chart` — safe to add.

---

## Out of Scope

- ExternalSecrets integration (no ESO dependency declared)
- Gateway API / HTTPRoute (not needed for bowapp's use case)
- PVC for the app container (postgresql handles its own persistence)
- OAuth2 proxy sidecar (not in bowapp's architecture)

# Helm Chart Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild `k8s/chart/` as a standalone, libra-application-pattern Helm chart with proper helpers, standard labels, configurable deployment options, and four new optional templates (HPA, PDB, NetworkPolicy, ServiceMonitor) — preserving all existing values keys exactly.

**Architecture:** A `_helpers.tpl` defines shared name/label helpers; all templates reference them. Five existing `.yml` templates are rewritten as `.yaml` files using helper-based names and standard `app.kubernetes.io/*` labels. Four new template files are added, all disabled by default. New values are appended to `values.yaml` without removing any existing key.

**Tech Stack:** Helm v3, Kubernetes 1.23+ (`autoscaling/v2`, `policy/v1`), Prometheus Operator CRDs (for ServiceMonitor, optional)

---

## File Map

| Action | Path |
|---|---|
| Create | `k8s/chart/templates/_helpers.tpl` |
| Create | `k8s/chart/templates/deployment.yaml` |
| Create | `k8s/chart/templates/svc.yaml` |
| Create | `k8s/chart/templates/ingress.yaml` |
| Create | `k8s/chart/templates/config.yaml` |
| Create | `k8s/chart/templates/sa.yaml` |
| Create | `k8s/chart/templates/hpa.yaml` |
| Create | `k8s/chart/templates/pdb.yaml` |
| Create | `k8s/chart/templates/networkpolicy.yaml` |
| Create | `k8s/chart/templates/servicemonitor.yaml` |
| Delete | `k8s/chart/templates/deployment.yml` |
| Delete | `k8s/chart/templates/svc.yml` |
| Delete | `k8s/chart/templates/ingress.yml` |
| Delete | `k8s/chart/templates/config.yml` |
| Delete | `k8s/chart/templates/sa.yml` |
| Modify | `k8s/chart/values.yaml` |
| Modify | `k8s/chart/Chart.yaml` |

---

## Task 1: `_helpers.tpl` — shared template helpers

**Files:**
- Create: `k8s/chart/templates/_helpers.tpl`

- [ ] **Step 1: Create the helpers file**

```
k8s/chart/templates/_helpers.tpl
```

```
{{/*
Expand the name of the chart. Defaults to .Release.Name so that a release
installed as "helm install bowapp ..." resolves to "bowapp" — preserving
existing DNS names and selector labels.
*/}}
{{- define "bowapp.name" -}}
{{- default .Release.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified name for sub-resources (ConfigMap, ServiceAccount) where
collision between chart name and release name matters.
*/}}
{{- define "bowapp.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Chart name + version label value.
*/}}
{{- define "bowapp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every resource.
*/}}
{{- define "bowapp.labels" -}}
helm.sh/chart: {{ include "bowapp.chart" . }}
{{ include "bowapp.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app: {{ include "bowapp.name" . }}
{{- end }}

{{/*
Selector labels — used in Deployment.spec.selector.matchLabels and
Service.spec.selector. Must be stable across upgrades.
*/}}
{{- define "bowapp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bowapp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
```

- [ ] **Step 2: Verify helpers file is valid (lint the chart — it will fail on missing templates but helpers themselves must parse)**

```bash
cd /Users/roi/libra-projects/bagofwords
helm lint k8s/chart/ 2>&1 | head -20
```

Expected: lint errors about templates referencing helpers that exist (not parse errors). If you see `parse error`, the helpers file has a syntax issue — fix before continuing.

- [ ] **Step 3: Commit**

```bash
git add k8s/chart/templates/_helpers.tpl
git commit -m "feat(helm): add _helpers.tpl with bowapp.name, labels, selectorLabels helpers"
```

---

## Task 2: `values.yaml` — append new value keys

**Files:**
- Modify: `k8s/chart/values.yaml`

- [ ] **Step 1: Append new sections to the end of `k8s/chart/values.yaml`**

Add the following block at the very end of the file (after the existing `resources:` section):

```yaml

# -- Override for the resource name. Defaults to .Release.Name.
# Useful when deploying multiple releases of the same chart into one namespace.
nameOverride: ""

# -- Number of pod replicas.
replicaCount: 1

# -- Deployment strategy. Leave empty for Kubernetes default (RollingUpdate 25/25).
strategy: {}
# strategy:
#   type: RollingUpdate
#   rollingUpdate:
#     maxSurge: 1
#     maxUnavailable: 0

# -- Number of old ReplicaSets to retain.
revisionHistoryLimit: 2

# -- Seconds to wait for in-flight requests to drain on pod shutdown.
terminationGracePeriodSeconds: 30

# -- Node selector for the app pod. Does NOT affect the bundled PostgreSQL pod
# (use postgresql.primary.nodeSelector for that).
nodeSelector: {}

# -- Tolerations for the app pod.
tolerations: []

# -- Affinity rules for the app pod.
affinity: {}

# -- Topology spread constraints for the app pod.
topologySpreadConstraints: []

# -- Annotations added to every pod (not the Deployment metadata).
podAnnotations: {}

# -- Security context for the pod (fsGroup, runAsUser, etc.).
podSecurityContext: {}

# -- Security context for the main app container.
containerSecurityContext: {}

# -- Container lifecycle hooks (preStop / postStart).
lifecycle: {}
# lifecycle:
#   preStop:
#     exec:
#       command: ["/bin/sh", "-c", "sleep 5"]

# -- Init containers to run before the main app container.
initContainers: []

# -- Sidecar containers to run alongside the main app container.
extraContainers: []

# -- Extra environment variables injected into the main app container.
extraEnv: []
# extraEnv:
#   - name: MY_VAR
#     value: "foo"
#   - name: FROM_SECRET
#     valueFrom:
#       secretKeyRef:
#         name: my-secret
#         key: my-key

# -- Extra envFrom sources (ConfigMap or Secret refs) added after the main ConfigMap/secretRef.
extraEnvFrom: []
# extraEnvFrom:
#   - configMapRef:
#       name: extra-config
#   - secretRef:
#       name: extra-secret

# -- Extra volumes to add to the pod.
extraVolumes: []

# -- Extra volume mounts to add to the main app container.
extraVolumeMounts: []

# -- Startup probe. Kubernetes will not route traffic or count the pod ready until this passes.
startupProbe:
  httpGet:
    path: /health
    port: 3000
  initialDelaySeconds: 30
  periodSeconds: 10
  failureThreshold: 30

# -- Readiness probe. Pod is removed from Service endpoints while this fails.
readinessProbe:
  httpGet:
    path: /health
    port: 3000
  initialDelaySeconds: 5
  periodSeconds: 10
  failureThreshold: 3
  successThreshold: 1
  timeoutSeconds: 1

# -- Liveness probe. Pod is restarted when this fails.
livenessProbe:
  httpGet:
    path: /health
    port: 3000
  initialDelaySeconds: 15
  periodSeconds: 20
  failureThreshold: 3
  successThreshold: 1
  timeoutSeconds: 1

##########################################################
# Horizontal Pod Autoscaler
##########################################################
autoscaling:
  enabled: false
  minReplicas: 1
  maxReplicas: 5
  targetCPUUtilizationPercentage: 80
  # targetMemoryUtilizationPercentage: 80

##########################################################
# Pod Disruption Budget
##########################################################
podDisruptionBudget:
  enabled: false
  minAvailable: "50%"
  # maxUnavailable: 1   # set this and clear minAvailable to use maxUnavailable instead

##########################################################
# Network Policy
##########################################################
networkPolicy:
  enabled: false
  # ingress and egress are raw Kubernetes NetworkPolicy rule objects.
  ingress: []
  # ingress:
  #   - from:
  #     - namespaceSelector:
  #         matchLabels:
  #           kubernetes.io/metadata.name: ingress-nginx
  #     ports:
  #     - protocol: TCP
  #       port: 3000
  egress: []

##########################################################
# Prometheus ServiceMonitor
##########################################################
serviceMonitor:
  enabled: false
  interval: "30s"
  path: /metrics
  port: http
  # additionalLabels must match your Prometheus Operator's serviceMonitorSelector.
  additionalLabels: {}
  # additionalLabels:
  #   release: prometheus
```

- [ ] **Step 2: Verify values file parses correctly**

```bash
helm lint k8s/chart/
```

Expected: `[INFO] Chart.yaml: icon is recommended` (or similar non-fatal warnings). No `Error` lines.

- [ ] **Step 3: Commit**

```bash
git add k8s/chart/values.yaml
git commit -m "feat(helm): add new opt-in values (HPA, PDB, NetworkPolicy, ServiceMonitor, probes, extraEnv, security contexts)"
```

---

## Task 3: `Chart.yaml` — version bump

**Files:**
- Modify: `k8s/chart/Chart.yaml`

- [ ] **Step 1: Update Chart.yaml**

Change `version: 1.1.0` to `version: 1.2.0`. Leave everything else unchanged.

```yaml
apiVersion: v2
name: bagofwords
description: Bag of words - a new ai data tool
type: application
version: 1.2.0
appVersion: 1.0.1
dependencies:
  - name: postgresql
    version: 16.3.2
    repository: https://charts.bitnami.com/bitnami
    condition: postgresql.enabled
```

- [ ] **Step 2: Commit**

```bash
git add k8s/chart/Chart.yaml
git commit -m "chore(helm): bump chart version to 1.2.0"
```

---

## Task 4: `deployment.yaml` — full rewrite

**Files:**
- Create: `k8s/chart/templates/deployment.yaml`
- Delete: `k8s/chart/templates/deployment.yml`

- [ ] **Step 1: Create `k8s/chart/templates/deployment.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "bowapp.name" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "bowapp.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicaCount }}
  revisionHistoryLimit: {{ .Values.revisionHistoryLimit }}
  selector:
    matchLabels:
      {{- include "bowapp.selectorLabels" . | nindent 6 }}
  {{- with .Values.strategy }}
  strategy:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  template:
    metadata:
      labels:
        {{- include "bowapp.selectorLabels" . | nindent 8 }}
        app: {{ include "bowapp.name" . }}
        version: {{ .Chart.AppVersion | quote }}
      {{- with .Values.podAnnotations }}
      annotations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
    spec:
      {{- with .Values.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.topologySpreadConstraints }}
      topologySpreadConstraints:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.podSecurityContext }}
      securityContext:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      serviceAccountName: {{ .Values.serviceAccount.name }}
      terminationGracePeriodSeconds: {{ .Values.terminationGracePeriodSeconds }}
      {{- with .Values.initContainers }}
      initContainers:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: {{ include "bowapp.name" . }}
          image: "{{ .Values.image.registry }}/{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: Always
          ports:
            - name: http
              containerPort: 3000
              protocol: TCP
          {{- with .Values.lifecycle }}
          lifecycle:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          envFrom:
            - configMapRef:
                name: {{ include "bowapp.name" . }}
            {{- if not (empty .Values.config.secretRef) }}
            - secretRef:
                name: {{ .Values.config.secretRef }}
            {{- end }}
            {{- with .Values.extraEnvFrom }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
          {{- with .Values.extraEnv }}
          env:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          startupProbe:
            {{- toYaml .Values.startupProbe | nindent 12 }}
          readinessProbe:
            {{- toYaml .Values.readinessProbe | nindent 12 }}
          livenessProbe:
            {{- toYaml .Values.livenessProbe | nindent 12 }}
          resources:
            requests:
              cpu: {{ .Values.resources.requests.cpu }}
              memory: {{ .Values.resources.requests.memory }}
            {{- if not (empty .Values.resources.limits.memory) }}
            limits:
              memory: {{ .Values.resources.limits.memory }}
            {{- end }}
          {{- with .Values.containerSecurityContext }}
          securityContext:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          volumeMounts:
            - name: bow-config
              mountPath: /app/bow-config.yaml
              subPath: bowConfig
            {{- if .Values.database.auth.sslRootCert.secretName }}
            - name: db-ca-cert
              mountPath: /app/certs
              readOnly: true
            {{- end }}
            {{- with .Values.extraVolumeMounts }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
        {{- with .Values.extraContainers }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      volumes:
        - name: bow-config
          configMap:
            name: {{ include "bowapp.name" . }}
        {{- if .Values.database.auth.sslRootCert.secretName }}
        - name: db-ca-cert
          secret:
            secretName: {{ .Values.database.auth.sslRootCert.secretName }}
            items:
              - key: {{ .Values.database.auth.sslRootCert.key }}
                path: rds-combined-ca-bundle.pem
        {{- end }}
        {{- with .Values.extraVolumes }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
```

- [ ] **Step 2: Delete the old file**

```bash
rm k8s/chart/templates/deployment.yml
```

- [ ] **Step 3: Render and verify Deployment output**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  2>/dev/null | grep -A 60 "kind: Deployment" | head -70
```

Expected output includes:
- `name: bowapp` (release name used)
- `app.kubernetes.io/name: bowapp`
- `app.kubernetes.io/instance: bowapp`
- `helm.sh/chart: bagofwords-1.2.0`
- `replicas: 1`
- `cpu: 2` under requests (from existing `resources.requests.cpu: 2`)
- `startupProbe:` block with `path: /health`
- `readinessProbe:` block
- `livenessProbe:` block

- [ ] **Step 4: Verify backwards compat — existing values render identically to what the old template produced for core fields**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  2>/dev/null | grep -E "name:|image:|configMapRef:|secretRef:|mountPath:" | grep -v "^\-\-"
```

Expected: `name: bowapp`, image from `docker.io/bagofwords/bagofwords:latest`, `name: bowapp` for configMapRef.

- [ ] **Step 5: Commit**

```bash
git add k8s/chart/templates/deployment.yaml k8s/chart/templates/deployment.yml
git commit -m "feat(helm): rewrite deployment template with helpers, configurable probes, extraEnv, security contexts, sidecars"
```

---

## Task 5: `svc.yaml` — rewrite with helpers and named port

**Files:**
- Create: `k8s/chart/templates/svc.yaml`
- Delete: `k8s/chart/templates/svc.yml`

- [ ] **Step 1: Create `k8s/chart/templates/svc.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ include "bowapp.name" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "bowapp.labels" . | nindent 4 }}
spec:
  type: ClusterIP
  selector:
    {{- include "bowapp.selectorLabels" . | nindent 4 }}
  ports:
    - name: http
      port: 3000
      targetPort: 3000
      protocol: TCP
```

- [ ] **Step 2: Delete the old file**

```bash
rm k8s/chart/templates/svc.yml
```

- [ ] **Step 3: Render and verify**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  2>/dev/null | grep -A 20 "kind: Service" | grep -v "postgresql\|postgres" | head -25
```

Expected: `name: bowapp`, `type: ClusterIP`, `port: 3000`, `name: http` on the port.

- [ ] **Step 4: Commit**

```bash
git add k8s/chart/templates/svc.yaml k8s/chart/templates/svc.yml
git commit -m "feat(helm): rewrite service template with helpers and named http port"
```

---

## Task 6: `ingress.yaml` — rewrite with helpers

**Files:**
- Create: `k8s/chart/templates/ingress.yaml`
- Delete: `k8s/chart/templates/ingress.yml`

- [ ] **Step 1: Create `k8s/chart/templates/ingress.yaml`**

```yaml
{{- if .Values.ingress.enabled }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ include "bowapp.name" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "bowapp.labels" . | nindent 4 }}
  {{- with .Values.ingress.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  ingressClassName: {{ .Values.ingress.className }}
  rules:
    - host: {{ .Values.host }}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ include "bowapp.name" . }}
                port:
                  number: 3000
  {{- if .Values.ingress.tls.enabled }}
  tls:
    - hosts:
        - {{ .Values.host }}
      secretName: {{ .Values.ingress.tls.secretName }}
  {{- end }}
{{- end }}
```

- [ ] **Step 2: Delete the old file**

```bash
rm k8s/chart/templates/ingress.yml
```

- [ ] **Step 3: Render and verify (ingress enabled)**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  --set ingress.enabled=true \
  2>/dev/null | grep -A 30 "kind: Ingress"
```

Expected: `name: bowapp`, `ingressClassName: nginx`, `host: app.bagofwords.com`, backend `name: bowapp` port `3000`.

- [ ] **Step 4: Verify ingress is absent when disabled**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  --set ingress.enabled=false \
  2>/dev/null | grep "kind: Ingress" | wc -l
```

Expected: `0`

- [ ] **Step 5: Commit**

```bash
git add k8s/chart/templates/ingress.yaml k8s/chart/templates/ingress.yml
git commit -m "feat(helm): rewrite ingress template with helpers"
```

---

## Task 7: `config.yaml` — update metadata only, keep data verbatim

**Files:**
- Create: `k8s/chart/templates/config.yaml`
- Delete: `k8s/chart/templates/config.yml`

- [ ] **Step 1: Copy `config.yml` to `config.yaml`**

```bash
cp k8s/chart/templates/config.yml k8s/chart/templates/config.yaml
```

- [ ] **Step 2: Replace the metadata section in `config.yaml`**

Find and replace only these lines at the top of the file:

Old metadata block (lines 1-5):
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: bowapp
  namespace: {{.Release.Namespace}}
```

New metadata block:
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "bowapp.name" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "bowapp.labels" . | nindent 4 }}
```

Everything after `data:` remains byte-for-byte identical.

- [ ] **Step 3: Delete the old file**

```bash
rm k8s/chart/templates/config.yml
```

- [ ] **Step 4: Render and verify ConfigMap**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  2>/dev/null | grep -A 10 "kind: ConfigMap" | grep -v "postgresql\|postgres" | head -15
```

Expected: `name: bowapp`, labels block with `helm.sh/chart`, `BOW_DATABASE_URL` key in data (built from postgresql values).

- [ ] **Step 5: Commit**

```bash
git add k8s/chart/templates/config.yaml k8s/chart/templates/config.yml
git commit -m "feat(helm): update configmap template metadata to use helpers and standard labels"
```

---

## Task 8: `sa.yaml` — rewrite with helpers

**Files:**
- Create: `k8s/chart/templates/sa.yaml`
- Delete: `k8s/chart/templates/sa.yml`

- [ ] **Step 1: Create `k8s/chart/templates/sa.yaml`**

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ .Values.serviceAccount.name }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "bowapp.labels" . | nindent 4 }}
  {{- with .Values.serviceAccount.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- if not (empty .Values.serviceAccount.imagePullSecret) }}
imagePullSecrets:
  - name: {{ .Values.serviceAccount.imagePullSecret }}
{{- end }}
```

- [ ] **Step 2: Delete the old file**

```bash
rm k8s/chart/templates/sa.yml
```

- [ ] **Step 3: Render and verify**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  2>/dev/null | grep -A 15 "kind: ServiceAccount" | grep -v "postgresql\|postgres" | head -20
```

Expected: `name: bowapp` (from `serviceAccount.name` default), labels block, no `imagePullSecrets` (default is empty).

- [ ] **Step 4: Commit**

```bash
git add k8s/chart/templates/sa.yaml k8s/chart/templates/sa.yml
git commit -m "feat(helm): rewrite serviceaccount template with helpers and standard labels"
```

---

## Task 9: `hpa.yaml` — HorizontalPodAutoscaler

**Files:**
- Create: `k8s/chart/templates/hpa.yaml`

- [ ] **Step 1: Create `k8s/chart/templates/hpa.yaml`**

```yaml
{{- if .Values.autoscaling.enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "bowapp.name" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "bowapp.labels" . | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "bowapp.name" . }}
  minReplicas: {{ .Values.autoscaling.minReplicas }}
  maxReplicas: {{ .Values.autoscaling.maxReplicas }}
  metrics:
    {{- if .Values.autoscaling.targetCPUUtilizationPercentage }}
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .Values.autoscaling.targetCPUUtilizationPercentage }}
    {{- end }}
    {{- if .Values.autoscaling.targetMemoryUtilizationPercentage }}
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: {{ .Values.autoscaling.targetMemoryUtilizationPercentage }}
    {{- end }}
{{- end }}
```

- [ ] **Step 2: Verify HPA absent when disabled (default)**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  2>/dev/null | grep "kind: HorizontalPodAutoscaler" | wc -l
```

Expected: `0`

- [ ] **Step 3: Verify HPA rendered when enabled**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  --set autoscaling.enabled=true \
  --set autoscaling.minReplicas=2 \
  --set autoscaling.maxReplicas=8 \
  2>/dev/null | grep -A 30 "kind: HorizontalPodAutoscaler"
```

Expected: `name: bowapp`, `minReplicas: 2`, `maxReplicas: 8`, CPU metric at 80%.

- [ ] **Step 4: Commit**

```bash
git add k8s/chart/templates/hpa.yaml
git commit -m "feat(helm): add HPA template (autoscaling/v2, disabled by default)"
```

---

## Task 10: `pdb.yaml` — PodDisruptionBudget

**Files:**
- Create: `k8s/chart/templates/pdb.yaml`

- [ ] **Step 1: Create `k8s/chart/templates/pdb.yaml`**

```yaml
{{- if .Values.podDisruptionBudget.enabled }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "bowapp.name" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "bowapp.labels" . | nindent 4 }}
spec:
  selector:
    matchLabels:
      {{- include "bowapp.selectorLabels" . | nindent 6 }}
  {{- if .Values.podDisruptionBudget.minAvailable }}
  minAvailable: {{ .Values.podDisruptionBudget.minAvailable }}
  {{- else if .Values.podDisruptionBudget.maxUnavailable }}
  maxUnavailable: {{ .Values.podDisruptionBudget.maxUnavailable }}
  {{- end }}
{{- end }}
```

- [ ] **Step 2: Verify PDB absent when disabled (default)**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  2>/dev/null | grep "kind: PodDisruptionBudget" | wc -l
```

Expected: `0`

- [ ] **Step 3: Verify PDB with minAvailable**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  --set podDisruptionBudget.enabled=true \
  2>/dev/null | grep -A 15 "kind: PodDisruptionBudget"
```

Expected: `name: bowapp`, `minAvailable: 50%`, selector matching `app.kubernetes.io/name: bowapp`.

- [ ] **Step 4: Verify PDB with maxUnavailable**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  --set podDisruptionBudget.enabled=true \
  --set 'podDisruptionBudget.minAvailable=' \
  --set podDisruptionBudget.maxUnavailable=1 \
  2>/dev/null | grep -A 15 "kind: PodDisruptionBudget"
```

Expected: `maxUnavailable: 1` (no `minAvailable` key).

- [ ] **Step 5: Commit**

```bash
git add k8s/chart/templates/pdb.yaml
git commit -m "feat(helm): add PodDisruptionBudget template (policy/v1, disabled by default)"
```

---

## Task 11: `networkpolicy.yaml` — NetworkPolicy

**Files:**
- Create: `k8s/chart/templates/networkpolicy.yaml`

- [ ] **Step 1: Create `k8s/chart/templates/networkpolicy.yaml`**

```yaml
{{- if .Values.networkPolicy.enabled }}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "bowapp.name" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "bowapp.labels" . | nindent 4 }}
spec:
  podSelector:
    matchLabels:
      {{- include "bowapp.selectorLabels" . | nindent 6 }}
  {{- with .Values.networkPolicy.ingress }}
  ingress:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- with .Values.networkPolicy.egress }}
  egress:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}
```

- [ ] **Step 2: Verify NetworkPolicy absent when disabled (default)**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  2>/dev/null | grep "kind: NetworkPolicy" | wc -l
```

Expected: `0`

- [ ] **Step 3: Verify NetworkPolicy rendered with ingress rule**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  --set networkPolicy.enabled=true \
  2>/dev/null | grep -A 15 "kind: NetworkPolicy"
```

Expected: `name: bowapp`, `podSelector` matching `app.kubernetes.io/name: bowapp`, no ingress/egress block (empty lists).

- [ ] **Step 4: Commit**

```bash
git add k8s/chart/templates/networkpolicy.yaml
git commit -m "feat(helm): add NetworkPolicy template (disabled by default)"
```

---

## Task 12: `servicemonitor.yaml` — Prometheus ServiceMonitor

**Files:**
- Create: `k8s/chart/templates/servicemonitor.yaml`

- [ ] **Step 1: Create `k8s/chart/templates/servicemonitor.yaml`**

```yaml
{{- if .Values.serviceMonitor.enabled }}
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: {{ include "bowapp.name" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "bowapp.labels" . | nindent 4 }}
    {{- with .Values.serviceMonitor.additionalLabels }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
spec:
  selector:
    matchLabels:
      {{- include "bowapp.selectorLabels" . | nindent 6 }}
  endpoints:
    - port: {{ .Values.serviceMonitor.port }}
      path: {{ .Values.serviceMonitor.path }}
      interval: {{ .Values.serviceMonitor.interval }}
{{- end }}
```

- [ ] **Step 2: Verify ServiceMonitor absent when disabled (default)**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  2>/dev/null | grep "kind: ServiceMonitor" | wc -l
```

Expected: `0`

- [ ] **Step 3: Verify ServiceMonitor rendered when enabled**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  --set serviceMonitor.enabled=true \
  --set 'serviceMonitor.additionalLabels.release=prometheus' \
  2>/dev/null | grep -A 20 "kind: ServiceMonitor"
```

Expected: `name: bowapp`, `release: prometheus` in labels, `port: http`, `path: /metrics`, `interval: 30s`, selector matching `app.kubernetes.io/name: bowapp`.

- [ ] **Step 4: Commit**

```bash
git add k8s/chart/templates/servicemonitor.yaml
git commit -m "feat(helm): add ServiceMonitor template (disabled by default)"
```

---

## Task 13: Final validation

**Files:** none created

- [ ] **Step 1: Run full helm lint**

```bash
helm lint k8s/chart/
```

Expected: `1 chart(s) linted, 0 chart(s) failed`

If any `[ERROR]` lines appear, fix the reported template before continuing.

- [ ] **Step 2: Render full default manifest and count resource kinds**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  2>/dev/null | grep "^kind:" | sort | uniq -c
```

Expected output includes (counts may vary due to postgresql subchart):
```
  1 kind: ConfigMap
  1 kind: Deployment
  1 kind: Ingress
  1 kind: Service
  1 kind: ServiceAccount
```

No `HorizontalPodAutoscaler`, `PodDisruptionBudget`, `NetworkPolicy`, or `ServiceMonitor` — all disabled by default.

- [ ] **Step 3: Render with all new features enabled and verify**

```bash
helm template bowapp k8s/chart/ \
  --set postgresql.auth.username=test \
  --set postgresql.auth.password=test \
  --set postgresql.auth.database=testdb \
  --set autoscaling.enabled=true \
  --set podDisruptionBudget.enabled=true \
  --set networkPolicy.enabled=true \
  --set serviceMonitor.enabled=true \
  2>/dev/null | grep "^kind:" | sort | uniq -c
```

Expected: all four new kinds appear alongside the five core kinds.

- [ ] **Step 4: Verify no old `.yml` files remain**

```bash
ls k8s/chart/templates/*.yml 2>/dev/null && echo "FAIL: old .yml files still present" || echo "OK: no .yml files"
```

Expected: `OK: no .yml files`

- [ ] **Step 5: Final commit**

```bash
git add k8s/chart/
git commit -m "feat(helm): complete chart rebuild — helpers, standard labels, HPA/PDB/NetworkPolicy/ServiceMonitor, configurable deployment options"
```

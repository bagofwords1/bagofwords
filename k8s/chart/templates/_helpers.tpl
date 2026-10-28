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

{{/*
Bundled PostgreSQL. Names, selector labels, serviceName and the PVC template
deliberately match what the Bitnami subchart (chart <= 2.x) rendered: all of
them are immutable on a StatefulSet or bound to the data on disk, so matching
them is what lets `helm upgrade` adopt an existing database in place.
*/}}
{{- define "bowapp.postgresql.fullname" -}}
{{- if contains "postgresql" .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-postgresql" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end }}

{{- define "bowapp.postgresql.selectorLabels" -}}
app.kubernetes.io/name: postgresql
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: primary
{{- end }}

{{- define "bowapp.postgresql.labels" -}}
helm.sh/chart: {{ include "bowapp.chart" . }}
{{ include "bowapp.postgresql.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.postgresql.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Secret holding the PostgreSQL passwords: the user's `auth.existingSecret`, or
the one this chart creates.
*/}}
{{- define "bowapp.postgresql.secretName" -}}
{{- default (include "bowapp.postgresql.fullname" .) .Values.postgresql.auth.existingSecret -}}
{{- end }}

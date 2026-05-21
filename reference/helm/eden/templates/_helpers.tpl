{{/*
EDEN reference Helm chart — template helpers.

The helpers below provide:

  - eden.name              short chart name ("eden")
  - eden.fullname          release-scoped fullname ("<release>-eden")
  - eden.chart             "eden-<chart-version>" for the standard chart label
  - eden.labels            standard recommended Kubernetes labels
  - eden.selectorLabels    selector-only labels (subset of eden.labels)
  - eden.secretName        either <fullname>-secrets or .Values.secrets.existingSecret
  - eden.image             full image ref "<repository>:<tag>"
  - eden.componentLabels   labels merged with component-specific app.kubernetes.io/component

These mirror the conventions in
https://helm.sh/docs/chart_best_practices/labels/.
*/}}

{{- define "eden.name" -}}
{{- default "eden" .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "eden.fullname" -}}
{{- $name := default "eden" .Chart.Name -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "eden.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "eden.labels" -}}
helm.sh/chart: {{ include "eden.chart" . }}
{{ include "eden.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: eden
{{- end -}}

{{- define "eden.selectorLabels" -}}
app.kubernetes.io/name: {{ include "eden.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
eden.componentLabels — call with a dict {context, component}. The component
string is appended as app.kubernetes.io/component plus an EDEN-internal
"eden.io/service" label used as the selector key for the per-service
Deployment / StatefulSet / Service.
*/}}
{{- define "eden.componentLabels" -}}
{{- $ctx := .context -}}
{{- $component := .component -}}
{{ include "eden.labels" $ctx }}
app.kubernetes.io/component: {{ $component }}
eden.io/service: {{ $component }}
{{- end -}}

{{- define "eden.componentSelectorLabels" -}}
{{- $ctx := .context -}}
{{- $component := .component -}}
{{ include "eden.selectorLabels" $ctx }}
eden.io/service: {{ $component }}
{{- end -}}

{{/*
eden.secretName — the Secret name pods reference via envFrom. Returns the
existingSecret if set, otherwise the chart-managed "<fullname>-secrets".
*/}}
{{- define "eden.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "eden.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
eden.image — fully-qualified image reference. The values.schema.json
enforces image.repository and image.tag are non-empty; this helper just
concatenates.
*/}}
{{- define "eden.image" -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}

{{/*
eden.componentName — release-scoped name for a per-service workload /
Service / etc. Example: "<release>-eden-orchestrator".
*/}}
{{- define "eden.componentName" -}}
{{- $ctx := .context -}}
{{- $component := .component -}}
{{- printf "%s-%s" (include "eden.fullname" $ctx) $component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

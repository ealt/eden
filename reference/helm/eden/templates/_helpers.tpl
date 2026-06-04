{{/*
Chart name.
*/}}
{{- define "eden.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully-qualified release name. With the conventional release name "eden" and
chart name "eden" this collapses to "eden", so resources read as
"eden-orchestrator", "eden-postgres", etc. Two releases in the SAME namespace
WOULD collide on these names — operators MUST use distinct namespaces per
release (see README "Coexistence").
*/}}
{{- define "eden.fullname" -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- /* Truncate to 40, not 63: per-resource suffixes are appended after this
       (the longest, "-git-credential-helper", is 22 chars), so reserving
       headroom keeps every rendered name within Kubernetes' 63-char DNS
       label limit even for long release names. */ -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Chart label value (name-version).
*/}}
{{- define "eden.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels stamped on every resource.
*/}}
{{- define "eden.labels" -}}
helm.sh/chart: {{ include "eden.chart" . }}
{{ include "eden.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels (release-scoped, no version — version must not be a selector).
*/}}
{{- define "eden.selectorLabels" -}}
app.kubernetes.io/name: {{ include "eden.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Per-component selector labels. Call with (dict "ctx" . "component" "orchestrator").
*/}}
{{- define "eden.componentSelectorLabels" -}}
{{ include "eden.selectorLabels" .ctx }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Per-component full labels. Call with (dict "ctx" . "component" "orchestrator").
*/}}
{{- define "eden.componentLabels" -}}
{{ include "eden.labels" .ctx }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Resolved container image reference. image.repository + image.tag are both
required (values.schema.json minLength: 1).
*/}}
{{- define "eden.image" -}}
{{- printf "%s:%s" (required "image.repository is required — build + push the eden-reference image and set image.repository (see docs/deployment/helm.md)" .Values.image.repository) (required "image.tag is required — set image.tag to the pushed image tag" .Values.image.tag) -}}
{{- end -}}

{{/*
imagePullSecrets block (renders nothing when empty).
*/}}
{{- define "eden.imagePullSecrets" -}}
{{- if .Values.image.pullSecrets }}
imagePullSecrets:
{{- range .Values.image.pullSecrets }}
  - {{ toYaml . | nindent 4 | trim }}
{{- end }}
{{- end -}}
{{- end -}}

{{/*
The Secret name the workloads pull env from: the operator's existingSecret
when set, else the chart-managed "<fullname>-secrets".
*/}}
{{- define "eden.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "eden.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Whether the app tier renders. The app tier (task-store-server, orchestrator,
worker hosts, web-ui) needs a seeded base commit; see values.yaml
"experiment.baseCommitSha".
*/}}
{{- define "eden.appEnabled" -}}
{{- if .Values.experiment.baseCommitSha -}}true{{- else -}}false{{- end -}}
{{- end -}}

{{/*
In-cluster service URLs derived from the release fullname.
*/}}
{{- define "eden.taskStoreUrl" -}}
{{- printf "http://%s-task-store-server:8080" (include "eden.fullname" .) -}}
{{- end -}}

{{- define "eden.controlPlaneUrl" -}}
{{- printf "http://%s-control-plane:8081" (include "eden.fullname" .) -}}
{{- end -}}

{{- define "eden.forgejoRemoteUrl" -}}
{{- printf "http://%s-forgejo:3000/eden/%s.git" (include "eden.fullname" .) .Values.experiment.id -}}
{{- end -}}

{{/*
The Web UI ingress host: explicit value, else a per-release default that
avoids cross-release hostname collisions.
*/}}
{{- define "eden.webUiHost" -}}
{{- if .Values.ingress.hosts.webUi -}}
{{- .Values.ingress.hosts.webUi -}}
{{- else -}}
{{- printf "%s-webui.eden.local" .Release.Name -}}
{{- end -}}
{{- end -}}

{{/*
Path to the git credential-helper mounted from the ConfigMap.
*/}}
{{- define "eden.credentialHelperPath" -}}
/etc/eden/git-credential-helper
{{- end -}}

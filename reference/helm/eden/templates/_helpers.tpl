{{/*
Chart name. The chart deliberately does NOT support nameOverride: the setup
script derives the same fullname in bash, and a values-only override would
silently diverge from the resource names it targets.
*/}}
{{- define "eden.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully-qualified release name. With the conventional release name "eden" and
chart name "eden" this collapses to "eden", so resources read as
"eden-orchestrator", "eden-postgres", etc. Two releases in the SAME namespace
WOULD collide on these names — operators MUST use distinct namespaces per
release (see README "Coexistence").
*/}}
{{- define "eden.fullname" -}}
{{- $name := .Chart.Name -}}
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
Whether the store tier renders. The task-store-server needs a seeded base
commit (it stamps the seed onto the experiment row at first creation); see
values.yaml "experiment.baseCommitSha".
*/}}
{{- define "eden.appEnabled" -}}
{{- if .Values.experiment.baseCommitSha -}}true{{- else -}}false{{- end -}}
{{- end -}}

{{/*
Whether lease-driven HA mode is enabled (opt-in, default false). When true the
control-plane Deployment renders, the orchestrator + web-ui get
EDEN_CONTROL_PLANE_URL, and setup-experiment-helm.sh registers the experiment
with the control plane. DEFERRED + unvalidated behind #281.
*/}}
{{- define "eden.leaseModeEnabled" -}}
{{- if (((.Values.orchestrator).leaseMode).enabled) -}}true{{- else -}}false{{- end -}}
{{- end -}}

{{/*
Whether an identity-consuming service should render. An identity service
(orchestrator, web-ui, worker hosts) renders only once BOTH the store tier is
enabled AND its minted worker_id is set (so the app tier holds back until
setup-experiment-helm.sh has provisioned identities). Call with
(dict "ctx" . "key" "orchestrator").
*/}}
{{- define "eden.identityEnabled" -}}
{{- $id := index .ctx.Values.identity .key -}}
{{- if and (eq (include "eden.appEnabled" .ctx) "true") $id.workerId -}}true{{- else -}}false{{- end -}}
{{- end -}}

{{/*
Per-service identity Secret name. Call with (dict "ctx" . "component" "orchestrator").
*/}}
{{- define "eden.identitySecretName" -}}
{{- printf "%s-%s-identity" (include "eden.fullname" .ctx) .component -}}
{{- end -}}

{{/*
The per-service credential-provisioning initContainer: installs the minted
worker's token (from its identity Secret) into the writable credentials emptyDir
at /var/lib/eden/credentials/<workerId>.token (mode 0600), so the service's
startup credential-bootstrap verifies it via /whoami without an admin reissue.
Call with (dict "ctx" . "workerId" <id> "component" <component>).
*/}}
{{- define "eden.identityInitContainer" -}}
- name: provision-credential
  image: {{ include "eden.image" .ctx | quote }}
  imagePullPolicy: {{ .ctx.Values.image.pullPolicy }}
  command:
    - sh
    - -c
    - |
      set -e
      umask 077
      cp /etc/eden/identity/token "/var/lib/eden/credentials/{{ .workerId }}.token"
  volumeMounts:
    - name: identity-token
      mountPath: /etc/eden/identity
      readOnly: true
    - name: credentials
      mountPath: /var/lib/eden/credentials
{{- end -}}

{{/*
The volumes backing the identity flow: the read-only token Secret + the writable
credentials emptyDir the initContainer copies it into (and the service's
bootstrap lock/reissue path writes to). Call with
(dict "ctx" . "component" <component>).
*/}}
{{- define "eden.identityVolumes" -}}
- name: identity-token
  secret:
    secretName: {{ include "eden.identitySecretName" (dict "ctx" .ctx "component" .component) }}
- name: credentials
  emptyDir: {}
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

{{/*
Rollout-trigger checksums for the pod template's metadata.annotations. A change
to the chart-managed Secret or the startup-read ConfigMaps changes these
annotations, which changes the pod template and forces a rolling restart — so a
re-run that rotates a secret (or edits the experiment config) doesn't leave
pods running with stale env/config. (With secrets.existingSecret the Secret
template renders empty, so its checksum is stable — external-secret rotation is
the operator's concern.)
*/}}
{{- define "eden.rolloutChecksums" -}}
checksum/secret: {{ include (print $.Template.BasePath "/secret.yaml") . | sha256sum }}
checksum/experiment-config: {{ include (print $.Template.BasePath "/experiment-config-configmap.yaml") . | sha256sum }}
checksum/git-credential-helper: {{ include (print $.Template.BasePath "/git-credential-helper-configmap.yaml") . | sha256sum }}
{{- end -}}

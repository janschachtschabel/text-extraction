{{/* Chart name, release-scoped full name and labels. */}}
{{- define "text-extraction.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "text-extraction.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "text-extraction.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Labels shared by every object. */}}
{{- define "text-extraction.labels" -}}
helm.sh/chart: {{ include "text-extraction.chart" . }}
{{ include "text-extraction.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: {{ include "text-extraction.fullname" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Pod selector of the API Deployment. Never changes after the first install. */}}
{{- define "text-extraction.selectorLabels" -}}
app.kubernetes.io/name: {{ include "text-extraction.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Names of the Services, the Secret and the Valkey objects. */}}
{{- define "text-extraction.internalServiceName" -}}
{{- printf "%s-internal" (include "text-extraction.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "text-extraction.valkeyName" -}}
{{- printf "%s-valkey" (include "text-extraction.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "text-extraction.valkeySelectorLabels" -}}
app.kubernetes.io/name: {{ include "text-extraction.name" . }}-valkey
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Names and labels of the extract worker objects. The pod name label differs from the API's
(like Valkey's), so the API Services never select worker pods. */}}
{{- define "text-extraction.workerExtractName" -}}
{{- printf "%s-worker-extract" (include "text-extraction.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "text-extraction.workerExtractInternalServiceName" -}}
{{- printf "%s-internal" (include "text-extraction.workerExtractName" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "text-extraction.workerExtractSelectorLabels" -}}
app.kubernetes.io/name: {{ include "text-extraction.name" . }}-worker-extract
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "text-extraction.workerExtractLabels" -}}
helm.sh/chart: {{ include "text-extraction.chart" . }}
{{ include "text-extraction.workerExtractSelectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: {{ include "text-extraction.fullname" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* JOBS_ENABLED of the API pods: jobs.enabled when set, else true while the extract workers run. */}}
{{- define "text-extraction.jobsEnabled" -}}
{{- if kindIs "invalid" .Values.jobs.enabled -}}
{{- ternary "true" "false" (.Values.workers.extract.enabled | default false) -}}
{{- else -}}
{{- ternary "true" "false" (.Values.jobs.enabled | default false) -}}
{{- end -}}
{{- end -}}

{{/* Secret that holds the Valkey password: the existing one, or the chart-managed one. */}}
{{- define "text-extraction.valkeySecretName" -}}
{{- if .Values.valkey.auth.existingSecret -}}
{{- .Values.valkey.auth.existingSecret -}}
{{- else -}}
{{- printf "%s-auth" (include "text-extraction.valkeyName" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* True when the in-chart Valkey requires a password. */}}
{{- define "text-extraction.valkeyAuth" -}}
{{- if and .Values.valkey.enabled .Values.valkey.auth.enabled -}}true{{- end -}}
{{- end -}}

{{/* Secret that holds API_KEYS: the existing one, or the chart-managed one. */}}
{{- define "text-extraction.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-keys" (include "text-extraction.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* True when the service gets API_KEYS from a Secret. */}}
{{- define "text-extraction.hasApiKeys" -}}
{{- if or .Values.secrets.existingSecret .Values.secrets.apiKeys -}}true{{- end -}}
{{- end -}}

{{/* Image reference; the tag defaults to the chart appVersion. */}}
{{- define "text-extraction.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}

{{/* In-cluster Valkey URL. With a password it uses Kubernetes env expansion, so VALKEY_PASSWORD
must come before VALKEY_URL in the env list (see text-extraction.valkeyEnv). */}}
{{- define "text-extraction.valkeyUrl" -}}
{{- if include "text-extraction.valkeyAuth" . -}}
{{- printf "redis://:$(VALKEY_PASSWORD)@%s:6379/0" (include "text-extraction.valkeyName" .) -}}
{{- else -}}
{{- printf "redis://%s:6379/0" (include "text-extraction.valkeyName" .) -}}
{{- end -}}
{{- end -}}

{{/* VALKEY_PASSWORD (when Valkey has one) and VALKEY_URL, in this order. Shared by API and workers. */}}
{{- define "text-extraction.valkeyEnv" -}}
{{- if include "text-extraction.valkeyAuth" . }}
- name: VALKEY_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "text-extraction.valkeySecretName" . }}
      key: {{ .Values.valkey.auth.existingSecretKey }}
{{- end }}
- name: VALKEY_URL
  {{- if .Values.valkey.enabled }}
  value: {{ include "text-extraction.valkeyUrl" . | quote }}
  {{- else if .Values.externalValkey.existingSecret.name }}
  valueFrom:
    secretKeyRef:
      name: {{ .Values.externalValkey.existingSecret.name }}
      key: {{ .Values.externalValkey.existingSecret.key }}
  {{- else }}
  value: {{ required "externalValkey.url or externalValkey.existingSecret.name is required when valkey.enabled is false" .Values.externalValkey.url | quote }}
  {{- end }}
{{- end -}}

{{/* API_KEYS from the Secret, when keys are configured. Workers need it too: the settings
validation requires a key while AUTH_MODE is api_key. */}}
{{- define "text-extraction.apiKeysEnv" -}}
{{- if include "text-extraction.hasApiKeys" . }}
- name: API_KEYS
  valueFrom:
    secretKeyRef:
      name: {{ include "text-extraction.secretName" . }}
      key: {{ .Values.secrets.existingSecretKey }}
{{- end }}
{{- end -}}

{{/* Fail early on settings that cannot work together. */}}
{{- define "text-extraction.validate" -}}
{{- if and .Values.workers.extract.autoscaling.keda.enabled .Values.workers.extract.autoscaling.hpa.enabled -}}
{{- fail "workers.extract.autoscaling.keda.enabled and workers.extract.autoscaling.hpa.enabled are mutually exclusive: KEDA creates its own HorizontalPodAutoscaler. Enable only one." -}}
{{- end -}}
{{- if and (or .Values.workers.extract.autoscaling.keda.enabled .Values.workers.extract.autoscaling.hpa.enabled) (not .Values.workers.extract.enabled) -}}
{{- fail "workers.extract.autoscaling needs workers.extract.enabled=true." -}}
{{- end -}}
{{- /* Shutdown budget, as in server.py: graceful = max(30, EXTRACT_TIMEOUT_SECONDS + 5) after the drain,
       plus a 10 s margin before the kill. */ -}}
{{- $timeout := get .Values.config "EXTRACT_TIMEOUT_SECONDS" | default 25 | toString | float64 -}}
{{- $drain := get .Values.config "SHUTDOWN_DRAIN_SECONDS" | default 5 | toString | float64 -}}
{{- $graceful := maxf 30.0 (addf $timeout 5.0) -}}
{{- if gt (addf $drain $graceful) (subf (float64 .Values.terminationGracePeriodSeconds) 10.0) -}}
{{- fail (printf "terminationGracePeriodSeconds (%v) is too short for the API pods: SHUTDOWN_DRAIN_SECONDS (%v) + max(30, EXTRACT_TIMEOUT_SECONDS (%v) + 5) + a 10 s margin must fit. Raise terminationGracePeriodSeconds or lower the timeouts." .Values.terminationGracePeriodSeconds $drain $timeout) -}}
{{- end -}}
{{- if and .Values.workers.extract.enabled (gt $graceful (subf (float64 .Values.workers.extract.terminationGracePeriodSeconds) 10.0)) -}}
{{- fail (printf "workers.extract.terminationGracePeriodSeconds (%v) is too short: max(30, EXTRACT_TIMEOUT_SECONDS (%v) + 5) + a 10 s margin must fit. Raise it or lower EXTRACT_TIMEOUT_SECONDS." .Values.workers.extract.terminationGracePeriodSeconds $timeout) -}}
{{- end -}}
{{- if and .Values.workers.extract.autoscaling.keda.enabled (ne (include "text-extraction.jobsEnabled" .) "true") -}}
{{- fail "workers.extract.autoscaling.keda.enabled reads the queue gauges that API pods export only with JOBS_ENABLED=true; do not set jobs.enabled=false with KEDA." -}}
{{- end -}}
{{- end -}}

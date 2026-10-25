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

{{/* Worker pools, in the order their objects are rendered. Each has a `workers.<pool>` block. */}}
{{- define "text-extraction.workerPools" -}}
extract browser docling
{{- end -}}

{{/* Names and labels of the worker objects of a pool; call with (list $ "<pool>"). The pod name
label differs from the API's (like Valkey's), so the API Services never select worker pods. */}}
{{- define "text-extraction.workerName" -}}
{{- printf "%s-worker-%s" (include "text-extraction.fullname" (index . 0)) (index . 1) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "text-extraction.workerInternalServiceName" -}}
{{- printf "%s-internal" (include "text-extraction.workerName" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "text-extraction.workerSelectorLabels" -}}
app.kubernetes.io/name: {{ include "text-extraction.name" (index . 0) }}-worker-{{ index . 1 }}
app.kubernetes.io/instance: {{ (index . 0).Release.Name }}
{{- end -}}

{{- define "text-extraction.workerLabels" -}}
{{- $root := index . 0 -}}
helm.sh/chart: {{ include "text-extraction.chart" $root }}
{{ include "text-extraction.workerSelectorLabels" . }}
app.kubernetes.io/version: {{ $root.Chart.AppVersion | quote }}
app.kubernetes.io/part-of: {{ include "text-extraction.fullname" $root }}
app.kubernetes.io/managed-by: {{ $root.Release.Service }}
{{- end -}}

{{/* Image of a pool's workers; call with (list $ "<pool>"). Extract workers run the API image;
browser and Docling workers their own, with image.tag and then the appVersion as the default tag. */}}
{{- define "text-extraction.workerImage" -}}
{{- $root := index . 0 -}}
{{- $pool := index . 1 -}}
{{- if or (eq $pool "browser") (eq $pool "docling") -}}
{{- $image := (index $root.Values.workers $pool).image -}}
{{- printf "%s:%s" $image.repository ($image.tag | default $root.Values.image.tag | default $root.Chart.AppVersion) -}}
{{- else -}}
{{- include "text-extraction.image" $root -}}
{{- end -}}
{{- end -}}

{{/* Entry point of a pool's workers. The pods pass it as args: the image's ENTRYPOINT (tini, which
reaps orphaned children) stays PID 1. */}}
{{- define "text-extraction.workerCommand" -}}
{{- if eq . "browser" -}}text-extraction-browser-worker
{{- else if eq . "docling" -}}text-extraction-docling-worker
{{- else -}}text-extraction-worker{{- end -}}
{{- end -}}

{{/* The concurrency variable of a pool's workers (from workers.<pool>.concurrency). */}}
{{- define "text-extraction.workerConcurrencyVar" -}}
{{- if eq . "browser" -}}BROWSER_CONCURRENCY
{{- else if eq . "docling" -}}DOCLING_CONCURRENCY
{{- else -}}WORKER_CONCURRENCY{{- end -}}
{{- end -}}

{{/* BROWSER_ENABLED and BROWSER_UNBLOCK_ENABLED ("true"/"false") of every pod, from the browser
worker toggles. */}}
{{- define "text-extraction.browserEnabled" -}}
{{- ternary "true" "false" (.Values.workers.browser.enabled | default false) -}}
{{- end -}}

{{- define "text-extraction.browserUnblockEnabled" -}}
{{- ternary "true" "false" (and (.Values.workers.browser.enabled | default false) (.Values.workers.browser.unblock | default false)) -}}
{{- end -}}

{{/* DOCLING_ENABLED ("true"/"false") of every pod, from the Docling worker toggle. */}}
{{- define "text-extraction.doclingEnabled" -}}
{{- ternary "true" "false" (.Values.workers.docling.enabled | default false) -}}
{{- end -}}

{{/* The browser and Docling switches as env entries. They come after envFrom, so they override
config.*. */}}
{{- define "text-extraction.switchEnv" -}}
- name: BROWSER_ENABLED
  value: {{ include "text-extraction.browserEnabled" . | quote }}
- name: BROWSER_UNBLOCK_ENABLED
  value: {{ include "text-extraction.browserUnblockEnabled" . | quote }}
- name: DOCLING_ENABLED
  value: {{ include "text-extraction.doclingEnabled" . | quote }}
{{- end -}}

{{/* The models volume of the Docling workers: the existing claim, or the chart-managed one. The
chart mounts it at /models (DOCLING_MODELS_PATH). */}}
{{- define "text-extraction.doclingModelsClaim" -}}
{{- with .Values.workers.docling.models.persistence.existingClaim -}}
{{- . -}}
{{- else -}}
{{- printf "%s-docling-models" (include "text-extraction.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* The env entries of a pool's workers; call with (list $ "<pool>"). They come after envFrom,
so they override config.*. The Docling workers read the models from /models. */}}
{{- define "text-extraction.workerEnv" -}}
{{- $root := index . 0 -}}
{{- $pool := index . 1 -}}
- name: INTERNAL_PORT
  value: "9090"
- name: {{ include "text-extraction.workerConcurrencyVar" $pool }}
  value: {{ (index $root.Values.workers $pool).concurrency | toString | quote }}
{{- if eq $pool "docling" }}
- name: DOCLING_MODELS_PATH
  value: /models
{{- end }}
{{ include "text-extraction.switchEnv" $root }}
{{ include "text-extraction.valkeyEnv" $root | trim }}
{{- with include "text-extraction.apiKeysEnv" $root | trim }}
{{ . }}
{{- end }}
{{- end -}}

{{/* A number setting from config.*, else its default; call with (list $ "<NAME>" <default>). */}}
{{- define "text-extraction.configNumber" -}}
{{- $config := (index . 0).Values.config -}}
{{- if hasKey $config (index . 1) -}}
{{- get $config (index . 1) | toString | float64 -}}
{{- else -}}
{{- index . 2 | toString | float64 -}}
{{- end -}}
{{- end -}}

{{/* Shutdown budgets, as in server.py, browser_worker.py and docling_worker.py. extraction:
max(30, EXTRACT_TIMEOUT_SECONDS + 5). API: that, while BROWSER_ENABLED at least
EXTRACT_TIMEOUT_SECONDS + BROWSER_QUEUE_WAIT_SECONDS + render deadline + 10, and while
DOCLING_ENABLED at least EXTRACT_TIMEOUT_SECONDS + DOCLING_SYNC_WAIT_SECONDS + 5. Browser worker:
max(extraction, render deadline + 10). The render deadline is BROWSER_TIMEOUT_SECONDS, with unblock
mode the larger of it and BROWSER_UNBLOCK_TIMEOUT_SECONDS. Docling worker: a fixed 30 s
(docling_worker.GRACEFUL_SECONDS). */}}
{{- define "text-extraction.extractionGracefulSeconds" -}}
{{- $timeout := include "text-extraction.configNumber" (list . "EXTRACT_TIMEOUT_SECONDS" 25) | float64 -}}
{{- maxf 30.0 (ceil (addf $timeout 5.0)) -}}
{{- end -}}

{{- define "text-extraction.renderDeadlineSeconds" -}}
{{- $render := include "text-extraction.configNumber" (list . "BROWSER_TIMEOUT_SECONDS" 30) | float64 -}}
{{- if eq (include "text-extraction.browserUnblockEnabled" .) "true" -}}
{{- $render = maxf $render (include "text-extraction.configNumber" (list . "BROWSER_UNBLOCK_TIMEOUT_SECONDS" 60) | float64) -}}
{{- end -}}
{{- $render -}}
{{- end -}}

{{- define "text-extraction.apiGracefulSeconds" -}}
{{- $seconds := include "text-extraction.extractionGracefulSeconds" . | float64 -}}
{{- if eq (include "text-extraction.browserEnabled" .) "true" -}}
{{- $timeout := include "text-extraction.configNumber" (list . "EXTRACT_TIMEOUT_SECONDS" 25) | float64 -}}
{{- $wait := include "text-extraction.configNumber" (list . "BROWSER_QUEUE_WAIT_SECONDS" 10) | float64 -}}
{{- $render := include "text-extraction.renderDeadlineSeconds" . | float64 -}}
{{- $seconds = maxf $seconds (ceil (addf $timeout $wait $render 10.0)) -}}
{{- end -}}
{{- if eq (include "text-extraction.doclingEnabled" .) "true" -}}
{{- $timeout := include "text-extraction.configNumber" (list . "EXTRACT_TIMEOUT_SECONDS" 25) | float64 -}}
{{- $wait := include "text-extraction.configNumber" (list . "DOCLING_SYNC_WAIT_SECONDS" 20) | float64 -}}
{{- $seconds = maxf $seconds (ceil (addf $timeout $wait 5.0)) -}}
{{- end -}}
{{- $seconds -}}
{{- end -}}

{{- define "text-extraction.doclingGracefulSeconds" -}}30{{- end -}}

{{- define "text-extraction.browserGracefulSeconds" -}}
{{- $render := include "text-extraction.renderDeadlineSeconds" . | float64 -}}
{{- maxf (include "text-extraction.extractionGracefulSeconds" . | float64) (ceil (addf $render 10.0)) -}}
{{- end -}}

{{/* terminationGracePeriodSeconds of the API pods: the value when set, else drain + graceful
+ a 10 s margin. */}}
{{- define "text-extraction.apiGracePeriodSeconds" -}}
{{- if kindIs "invalid" .Values.terminationGracePeriodSeconds -}}
{{- $drain := include "text-extraction.configNumber" (list . "SHUTDOWN_DRAIN_SECONDS" 5) | float64 -}}
{{- addf $drain (include "text-extraction.apiGracefulSeconds" . | float64) 10.0 | ceil | int -}}
{{- else -}}
{{- .Values.terminationGracePeriodSeconds -}}
{{- end -}}
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
{{- $root := . -}}
{{- $jobs := eq (include "text-extraction.jobsEnabled" .) "true" -}}
{{- range $pool := splitList " " (include "text-extraction.workerPools" .) -}}
{{- $worker := index $root.Values.workers $pool -}}
{{- if and $worker.autoscaling.keda.enabled $worker.autoscaling.hpa.enabled -}}
{{- fail (printf "workers.%s.autoscaling.keda.enabled and workers.%s.autoscaling.hpa.enabled are mutually exclusive: KEDA creates its own HorizontalPodAutoscaler. Enable only one." $pool $pool) -}}
{{- end -}}
{{- if and (or $worker.autoscaling.keda.enabled $worker.autoscaling.hpa.enabled) (not $worker.enabled) -}}
{{- fail (printf "workers.%s.autoscaling needs workers.%s.enabled=true." $pool $pool) -}}
{{- end -}}
{{- if and $worker.autoscaling.keda.enabled (not $jobs) -}}
{{- fail (printf "workers.%s.autoscaling.keda.enabled reads the queue gauges that API pods export only with JOBS_ENABLED=true; do not set jobs.enabled=false with KEDA." $pool) -}}
{{- end -}}
{{- end -}}
{{- if and .Values.workers.browser.unblock (not .Values.workers.browser.enabled) -}}
{{- fail "workers.browser.unblock needs workers.browser.enabled=true: unblock mode runs in the browser workers." -}}
{{- end -}}
{{- $docling := .Values.workers.docling -}}
{{- if and $docling.enabled (not $jobs) -}}
{{- fail "workers.docling.enabled needs JOBS_ENABLED=true on the API pods: OCR runs as a job of the pool docling. Do not set jobs.enabled=false with Docling workers." -}}
{{- end -}}
{{- if and $docling.enabled (not $docling.models.download) (not $docling.models.persistence.existingClaim) -}}
{{- fail "workers.docling.models.download=false needs workers.docling.models.persistence.existingClaim: a volume that already holds the models. A new volume stays empty without the download." -}}
{{- end -}}
{{- /* Shutdown budgets (see text-extraction.apiGracefulSeconds), plus a 10 s margin before the kill. */ -}}
{{- $timeout := include "text-extraction.configNumber" (list . "EXTRACT_TIMEOUT_SECONDS" 25) | float64 -}}
{{- $drain := include "text-extraction.configNumber" (list . "SHUTDOWN_DRAIN_SECONDS" 5) | float64 -}}
{{- $apiGraceful := include "text-extraction.apiGracefulSeconds" . | float64 -}}
{{- $apiGrace := include "text-extraction.apiGracePeriodSeconds" . | float64 -}}
{{- if gt (addf $drain $apiGraceful) (subf $apiGrace 10.0) -}}
{{- fail (printf "terminationGracePeriodSeconds (%v) is too short for the API pods: SHUTDOWN_DRAIN_SECONDS (%v) + the graceful shutdown (%v s: max(30, EXTRACT_TIMEOUT_SECONDS + 5), with browser workers at least EXTRACT_TIMEOUT_SECONDS + BROWSER_QUEUE_WAIT_SECONDS + the render deadline + 10, with Docling workers at least EXTRACT_TIMEOUT_SECONDS + DOCLING_SYNC_WAIT_SECONDS + 5) + a 10 s margin must fit. Raise it, leave it unset (the chart then computes it) or lower the timeouts." .Values.terminationGracePeriodSeconds $drain $apiGraceful) -}}
{{- end -}}
{{- $extractionGraceful := include "text-extraction.extractionGracefulSeconds" . | float64 -}}
{{- if and .Values.workers.extract.enabled (gt $extractionGraceful (subf (float64 .Values.workers.extract.terminationGracePeriodSeconds) 10.0)) -}}
{{- fail (printf "workers.extract.terminationGracePeriodSeconds (%v) is too short: max(30, EXTRACT_TIMEOUT_SECONDS (%v) + 5) + a 10 s margin must fit. Raise it or lower EXTRACT_TIMEOUT_SECONDS." .Values.workers.extract.terminationGracePeriodSeconds $timeout) -}}
{{- end -}}
{{- $browserGraceful := include "text-extraction.browserGracefulSeconds" . | float64 -}}
{{- if and .Values.workers.browser.enabled (gt $browserGraceful (subf (float64 .Values.workers.browser.terminationGracePeriodSeconds) 10.0)) -}}
{{- fail (printf "workers.browser.terminationGracePeriodSeconds (%v) is too short: the browser workers give renders in flight %v s (max(30, EXTRACT_TIMEOUT_SECONDS + 5, render deadline + 10); the deadline is BROWSER_TIMEOUT_SECONDS, with unblock mode the larger of it and BROWSER_UNBLOCK_TIMEOUT_SECONDS) + a 10 s margin must fit. Raise it or lower the timeouts." .Values.workers.browser.terminationGracePeriodSeconds $browserGraceful) -}}
{{- end -}}
{{- $doclingGraceful := include "text-extraction.doclingGracefulSeconds" . | float64 -}}
{{- if and $docling.enabled (gt $doclingGraceful (subf (float64 $docling.terminationGracePeriodSeconds) 10.0)) -}}
{{- fail (printf "workers.docling.terminationGracePeriodSeconds (%v) is too short: the Docling workers give conversions in flight %v s + a 10 s margin must fit." $docling.terminationGracePeriodSeconds $doclingGraceful) -}}
{{- end -}}
{{- end -}}

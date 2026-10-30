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
extract browser docling asr nlp llm
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
browser, Docling, ASR and NLP workers their own, with image.tag and then the appVersion as the
default tag. The llm workers run the API image too (they hold no model and no browser). */}}
{{- define "text-extraction.workerImage" -}}
{{- $root := index . 0 -}}
{{- $pool := index . 1 -}}
{{- if has $pool (list "browser" "docling" "asr" "nlp") -}}
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
{{- else if eq . "asr" -}}text-extraction-asr-worker
{{- else if eq . "nlp" -}}text-extraction-nlp-worker
{{- else if eq . "llm" -}}text-extraction-llm-worker
{{- else -}}text-extraction-worker{{- end -}}
{{- end -}}

{{/* The concurrency variable of a pool's workers (from workers.<pool>.concurrency). */}}
{{- define "text-extraction.workerConcurrencyVar" -}}
{{- if eq . "browser" -}}BROWSER_CONCURRENCY
{{- else if eq . "docling" -}}DOCLING_CONCURRENCY
{{- else if eq . "asr" -}}ASR_CONCURRENCY
{{- else if eq . "nlp" -}}PII_CONCURRENCY
{{- else if eq . "llm" -}}LLM_CONCURRENCY
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

{{/* ASR_ENABLED ("true"/"false") of every pod, from the ASR worker toggle. */}}
{{- define "text-extraction.asrEnabled" -}}
{{- ternary "true" "false" (.Values.workers.asr.enabled | default false) -}}
{{- end -}}

{{/* PII_ENABLED ("true"/"false") of every pod, from the NLP worker toggle. */}}
{{- define "text-extraction.nlpEnabled" -}}
{{- ternary "true" "false" (.Values.workers.nlp.enabled | default false) -}}
{{- end -}}

{{/* LLM_ENABLED ("true"/"false") of every pod, from the llm worker toggle. */}}
{{- define "text-extraction.llmEnabled" -}}
{{- ternary "true" "false" (.Values.workers.llm.enabled | default false) -}}
{{- end -}}

{{/* The llm settings every pod reads while the llm workers run: the API pods check requests with
them (`semantic` needs the embedding model), the workers call the endpoint. The key is not here:
see text-extraction.llmKeyEnv. */}}
{{- define "text-extraction.llmEnv" -}}
{{- $llm := .Values.workers.llm -}}
{{- if $llm.enabled }}
- name: LLM_BASE_URL
  value: {{ $llm.baseUrl | quote }}
- name: LLM_MODEL
  value: {{ $llm.model | quote }}
{{- with $llm.embeddingModel }}
- name: LLM_EMBEDDING_MODEL
  value: {{ . | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{/* The browser, Docling, ASR, PII and LLM switches as env entries, with the ASR settings every pod
reads: the backend (the API answers 400 for the external one without a key) and the upload limit
(the API's body limit), and the LLM endpoint settings while the llm workers run. They come after
envFrom, so they override config.*. */}}
{{- define "text-extraction.switchEnv" -}}
- name: BROWSER_ENABLED
  value: {{ include "text-extraction.browserEnabled" . | quote }}
- name: BROWSER_UNBLOCK_ENABLED
  value: {{ include "text-extraction.browserUnblockEnabled" . | quote }}
- name: DOCLING_ENABLED
  value: {{ include "text-extraction.doclingEnabled" . | quote }}
- name: ASR_ENABLED
  value: {{ include "text-extraction.asrEnabled" . | quote }}
- name: PII_ENABLED
  value: {{ include "text-extraction.nlpEnabled" . | quote }}
- name: LLM_ENABLED
  value: {{ include "text-extraction.llmEnabled" . | quote }}
- name: ASR_BACKEND
  value: {{ .Values.workers.asr.backend | quote }}
- name: MEDIA_UPLOAD_MAX_BYTES
  value: {{ .Values.workers.asr.uploadMaxBytes | int64 | toString | quote }}
{{- with include "text-extraction.llmEnv" . | trim }}
{{ . }}
{{- end }}
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

{{/* The models volume of the ASR workers (local backend): the existing claim, or the
chart-managed one, mounted at /models (ASR_MODELS_PATH). */}}
{{- define "text-extraction.asrModelsClaim" -}}
{{- with .Values.workers.asr.models.persistence.existingClaim -}}
{{- . -}}
{{- else -}}
{{- printf "%s-asr-models" (include "text-extraction.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* The models volume of the NLP workers: the existing claim, or the chart-managed one, mounted
at /models (PII_MODELS_PATH). */}}
{{- define "text-extraction.nlpModelsClaim" -}}
{{- with .Values.workers.nlp.models.persistence.existingClaim -}}
{{- . -}}
{{- else -}}
{{- printf "%s-nlp-models" (include "text-extraction.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* "true" when a pool's workers read models from a volume; call with (list $ "<pool>"): the
Docling and NLP workers always, the ASR workers with the local backend. */}}
{{- define "text-extraction.workerHasModels" -}}
{{- $root := index . 0 -}}
{{- $pool := index . 1 -}}
{{- if or (eq $pool "docling") (eq $pool "nlp") (and (eq $pool "asr") (eq (toString $root.Values.workers.asr.backend) "local")) -}}true{{- end -}}
{{- end -}}

{{/* The models claim of a pool with models; call with (list $ "<pool>"). */}}
{{- define "text-extraction.workerModelsClaim" -}}
{{- if eq (index . 1) "asr" -}}
{{- include "text-extraction.asrModelsClaim" (index . 0) -}}
{{- else if eq (index . 1) "nlp" -}}
{{- include "text-extraction.nlpModelsClaim" (index . 0) -}}
{{- else -}}
{{- include "text-extraction.doclingModelsClaim" (index . 0) -}}
{{- end -}}
{{- end -}}

{{/* "init" or "job" when the chart downloads a pool's models, else nothing; call with (list $
"<pool>"). */}}
{{- define "text-extraction.modelsDownload" -}}
{{- $root := index . 0 -}}
{{- $pool := index . 1 -}}
{{- $worker := index $root.Values.workers $pool -}}
{{- if and $worker.enabled (include "text-extraction.workerHasModels" .) $worker.models.download -}}
{{- $worker.models.downloadMode | default "init" -}}
{{- end -}}
{{- end -}}

{{/* Secret that holds ASR_EXTERNAL_API_KEY: the existing one, or the chart-managed one. */}}
{{- define "text-extraction.asrSecretName" -}}
{{- if .Values.workers.asr.external.existingSecret -}}
{{- .Values.workers.asr.external.existingSecret -}}
{{- else -}}
{{- printf "%s-asr" (include "text-extraction.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* ASR_EXTERNAL_API_KEY from its Secret, while the ASR workers use the external backend and a key
is configured. The API pods and the ASR workers get it, no other pod. */}}
{{- define "text-extraction.asrKeyEnv" -}}
{{- $asr := .Values.workers.asr -}}
{{- if and $asr.enabled (eq (toString $asr.backend) "openai") (or $asr.external.existingSecret $asr.external.apiKey) }}
- name: ASR_EXTERNAL_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "text-extraction.asrSecretName" . }}
      key: {{ $asr.external.existingSecretKey }}
{{- end }}
{{- end -}}

{{/* PII_HASH_KEY from the operator's Secret, while the NLP workers run and a Secret is named. Only
the NLP worker container gets it (not its models init container, no other pod). */}}
{{- define "text-extraction.nlpKeyEnv" -}}
{{- $nlp := .Values.workers.nlp -}}
{{- if and $nlp.enabled $nlp.hashKey.existingSecret }}
- name: PII_HASH_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $nlp.hashKey.existingSecret | quote }}
      key: {{ $nlp.hashKey.existingSecretKey | quote }}
{{- end }}
{{- end -}}

{{/* LLM_API_KEY from the operator's Secret, while the llm workers run and a Secret is named. The
API pods (they only check that there is a key) and the llm worker container get it, no other pod. */}}
{{- define "text-extraction.llmKeyEnv" -}}
{{- $llm := .Values.workers.llm -}}
{{- if and $llm.enabled $llm.apiKey.existingSecret }}
- name: LLM_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $llm.apiKey.existingSecret | quote }}
      key: {{ $llm.apiKey.existingSecretKey | quote }}
{{- end }}
{{- end -}}

{{/* The env entries of a pool's workers; call with (list $ "<pool>"). They come after envFrom,
so they override config.*. The Docling workers read the models from /models; the ASR workers
too (local backend), keep downloaded media on /scratch and get the backend's settings; the NLP
workers read the models from /models and get the model size; the llm workers need nothing but
their slots (the endpoint settings are in the switches); the extract and browser workers get
DOCUMENT_MEMORY_LIMIT_MB from their memory limit. */}}
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
{{- else if eq $pool "asr" }}
{{- $asr := $root.Values.workers.asr }}
- name: ASR_MODELS_PATH
  value: /models
- name: ASR_MODEL
  value: {{ $asr.model | quote }}
- name: ASR_SCRATCH_PATH
  value: /scratch
{{- if eq (toString $asr.backend) "openai" }}
- name: ASR_EXTERNAL_BASE_URL
  value: {{ $asr.external.baseUrl | quote }}
- name: ASR_EXTERNAL_MODEL
  value: {{ $asr.external.model | quote }}
{{- with include "text-extraction.asrKeyEnv" $root | trim }}
{{ . }}
{{- end }}
{{- end }}
{{- else if eq $pool "nlp" }}
- name: PII_MODELS_PATH
  value: /models
- name: PII_MODEL_SIZE
  value: {{ $root.Values.workers.nlp.modelSize | quote }}
{{- else if eq $pool "llm" }}
{{- else }}
{{- $worker := index $root.Values.workers $pool }}
{{- with include "text-extraction.documentMemoryEnv" (list $root $worker.resources $worker.documentParentReserveMiB (printf "workers.%s.resources" $pool)) | trim }}
{{ . }}
{{- end }}
{{- end }}
{{ include "text-extraction.switchEnv" $root }}
{{ include "text-extraction.valkeyEnv" $root | trim }}
{{- with include "text-extraction.apiKeysEnv" $root | trim }}
{{ . }}
{{- end }}
{{- end -}}

{{/* A Kubernetes memory quantity (2Gi, 1536Mi, 2G, 2147483648, ...) in whole MiB. */}}
{{- define "text-extraction.memoryMiB" -}}
{{- $quantity := toString . -}}
{{- $mib := 0.0 -}}
{{- if hasSuffix "Ti" $quantity -}}
{{- $mib = mulf (trimSuffix "Ti" $quantity | float64) 1048576.0 -}}
{{- else if hasSuffix "Gi" $quantity -}}
{{- $mib = mulf (trimSuffix "Gi" $quantity | float64) 1024.0 -}}
{{- else if hasSuffix "Mi" $quantity -}}
{{- $mib = trimSuffix "Mi" $quantity | float64 -}}
{{- else if hasSuffix "Ki" $quantity -}}
{{- $mib = divf (trimSuffix "Ki" $quantity | float64) 1024.0 -}}
{{- else if hasSuffix "T" $quantity -}}
{{- $mib = divf (mulf (trimSuffix "T" $quantity | float64) 1000000000000.0) 1048576.0 -}}
{{- else if hasSuffix "G" $quantity -}}
{{- $mib = divf (mulf (trimSuffix "G" $quantity | float64) 1000000000.0) 1048576.0 -}}
{{- else if hasSuffix "M" $quantity -}}
{{- $mib = divf (mulf (trimSuffix "M" $quantity | float64) 1000000.0) 1048576.0 -}}
{{- else if hasSuffix "k" $quantity -}}
{{- $mib = divf (mulf (trimSuffix "k" $quantity | float64) 1000.0) 1048576.0 -}}
{{- else -}}
{{- $mib = divf ($quantity | float64) 1048576.0 -}}
{{- end -}}
{{- floor $mib | int -}}
{{- end -}}

{{/* DOCUMENT_MEMORY_LIMIT_MB of a pod with a DocumentPool (the API, the extract and browser
workers), as an env entry; call with (list $ <resources> <reserve MiB> "<values path of the
resources>"). Each of the DOCUMENT_CONCURRENCY conversion children gets an even share of the
container's memory limit after the reserve for the rest of the pod: floor((limit MiB - reserve)
/ DOCUMENT_CONCURRENCY).
Nothing when config.DOCUMENT_MEMORY_LIMIT_MB is set (it wins, through envFrom) or the container
has no memory limit (the default, 2048, applies). The cgroup's OOM killer counts every process
of the container: with the children's address space within the limit, a child hits its own
RLIMIT_AS (too_large) before the pod is killed. */}}
{{- define "text-extraction.documentMemoryEnv" -}}
{{- $root := index . 0 -}}
{{- $resources := index . 1 | default dict -}}
{{- $reserve := index . 2 | float64 -}}
{{- $path := index . 3 -}}
{{- $limit := dig "limits" "memory" "" $resources -}}
{{- if and $limit (not (hasKey $root.Values.config "DOCUMENT_MEMORY_LIMIT_MB")) -}}
{{- $limitMiB := include "text-extraction.memoryMiB" $limit | float64 -}}
{{- $children := include "text-extraction.configNumber" (list $root "DOCUMENT_CONCURRENCY" 2) | float64 -}}
{{- $share := divf (subf $limitMiB $reserve) $children | floor | int -}}
{{- if lt $share 256 -}}
{{- fail (printf "%s.limits.memory (%v) leaves %v MiB for each of the DOCUMENT_CONCURRENCY (%v) conversion children after the reserve (%v MiB), below the minimum of 256: raise the limit, lower config.DOCUMENT_CONCURRENCY or the reserve, or set config.DOCUMENT_MEMORY_LIMIT_MB." $path $limit $share $children $reserve) -}}
{{- end }}
- name: DOCUMENT_MEMORY_LIMIT_MB
  value: {{ $share | toString | quote }}
{{- end -}}
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

{{/* ASR worker: a fixed 30 s (asr_worker.GRACEFUL_SECONDS); nothing waits for a transcription. */}}
{{- define "text-extraction.asrGracefulSeconds" -}}30{{- end -}}

{{/* NLP worker: a fixed 30 s (nlp_worker.GRACEFUL_SECONDS). */}}
{{- define "text-extraction.nlpGracefulSeconds" -}}30{{- end -}}

{{/* llm worker: a fixed 30 s (llm_worker.GRACEFUL_SECONDS), then the items still running are given
back to the queue at once. */}}
{{- define "text-extraction.llmGracefulSeconds" -}}30{{- end -}}

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
{{- $window := $worker.autoscaling.scaleDownStabilizationWindowSeconds -}}
{{- if and (not (kindIs "invalid" $window)) (or (lt (float64 $window) 0.0) (gt (float64 $window) 3600.0)) -}}
{{- fail (printf "workers.%s.autoscaling.scaleDownStabilizationWindowSeconds must be between 0 and 3600 (the Kubernetes limit), or null for the default; got %v." $pool $window) -}}
{{- end -}}
{{- if $worker.autoscaling.keda.enabled -}}
{{- if le (include "text-extraction.kedaThreshold" (list $root $pool) | float64) 0.0 -}}
{{- fail (printf "workers.%s.autoscaling.keda.threshold must be a positive number, or null for workers.%s.concurrency." $pool $pool) -}}
{{- end -}}
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
{{- range $pool := list "docling" "asr" "nlp" -}}
{{- $mode := toString ((index $root.Values.workers $pool).models.downloadMode | default "init") -}}
{{- if not (has $mode (list "init" "job")) -}}
{{- fail (printf "workers.%s.models.downloadMode must be init or job, not %s." $pool $mode) -}}
{{- end -}}
{{- end -}}
{{- $nlp := .Values.workers.nlp -}}
{{- if $nlp.enabled -}}
{{- if not $jobs -}}
{{- fail "workers.nlp.enabled needs JOBS_ENABLED=true on the API pods: PII handling runs as a job of the pool nlp. Do not set jobs.enabled=false with NLP workers." -}}
{{- end -}}
{{- if not (has (toString $nlp.modelSize) (list "lg" "md")) -}}
{{- fail (printf "workers.nlp.modelSize must be lg or md (the models of the manifest), not %s." $nlp.modelSize) -}}
{{- end -}}
{{- if and (not $nlp.models.download) (not $nlp.models.persistence.existingClaim) -}}
{{- fail "workers.nlp.models.download=false needs workers.nlp.models.persistence.existingClaim: a volume that already holds the models. A new volume stays empty without the download." -}}
{{- end -}}
{{- end -}}
{{- $llm := .Values.workers.llm -}}
{{- if $llm.enabled -}}
{{- if not $jobs -}}
{{- fail "workers.llm.enabled needs JOBS_ENABLED=true on the API pods: LLM work runs as a job of the pool llm. Do not set jobs.enabled=false with llm workers." -}}
{{- end -}}
{{- if not $llm.baseUrl -}}
{{- fail "workers.llm.baseUrl is required with workers.llm.enabled: the pods exit with code 2 without an endpoint." -}}
{{- end -}}
{{- if not $llm.model -}}
{{- fail "workers.llm.model is required with workers.llm.enabled: the pods exit with code 2 without a model." -}}
{{- end -}}
{{- if and (not (hasPrefix "https://" (toString $llm.baseUrl))) (ne (toString (get .Values.config "LLM_ALLOW_HTTP" | default "false")) "true") -}}
{{- fail "workers.llm.baseUrl must start with https:// (the pods exit with code 2 otherwise); set config.LLM_ALLOW_HTTP=true only for a local model server in development." -}}
{{- end -}}
{{- end -}}
{{- $asr := .Values.workers.asr -}}
{{- if $asr.enabled -}}
{{- if not $jobs -}}
{{- fail "workers.asr.enabled needs JOBS_ENABLED=true on the API pods: transcription always runs as a job of the pool asr. Do not set jobs.enabled=false with ASR workers." -}}
{{- end -}}
{{- $backend := toString $asr.backend -}}
{{- if not (has $backend (list "local" "openai")) -}}
{{- fail (printf "workers.asr.backend must be local or openai, not %s." $backend) -}}
{{- end -}}
{{- if not (has (toString $asr.model) (list "small" "large-v3-turbo")) -}}
{{- fail (printf "workers.asr.model must be small or large-v3-turbo (the models of the manifest), not %s." $asr.model) -}}
{{- end -}}
{{- if eq $backend "openai" -}}
{{- if not $asr.external.baseUrl -}}
{{- fail "workers.asr.external.baseUrl is required with workers.asr.backend=openai: the ASR workers exit with code 2 without it." -}}
{{- end -}}
{{- if not (hasPrefix "https://" (toString $asr.external.baseUrl)) -}}
{{- fail "workers.asr.external.baseUrl must start with https:// (the service refuses any other URL)." -}}
{{- end -}}
{{- else if and (not $asr.models.download) (not $asr.models.persistence.existingClaim) -}}
{{- fail "workers.asr.models.download=false needs workers.asr.models.persistence.existingClaim: a volume that already holds the model. A new volume stays empty without the download." -}}
{{- end -}}
{{- /* Downloaded media: concurrency x (MEDIA_MAX_BYTES + the 5 MB of slack the download may use). */ -}}
{{- if not $asr.scratch.sizeLimit -}}
{{- fail "workers.asr.scratch.sizeLimit is required: the downloaded media of the ASR workers live on an emptyDir that must be bounded." -}}
{{- end -}}
{{- $mediaMax := include "text-extraction.configNumber" (list . "MEDIA_MAX_BYTES" 500000000) | float64 -}}
{{- $neededMiB := divf (mulf (float64 $asr.concurrency) (addf $mediaMax 5000000.0)) 1048576.0 | ceil -}}
{{- $scratchMiB := include "text-extraction.memoryMiB" $asr.scratch.sizeLimit | float64 -}}
{{- if lt $scratchMiB $neededMiB -}}
{{- fail (printf "workers.asr.scratch.sizeLimit (%v) is smaller than workers.asr.concurrency (%v) x (MEDIA_MAX_BYTES (%v) + 5 MB of download slack) = %v MiB: raise the size limit or lower config.MEDIA_MAX_BYTES." $asr.scratch.sizeLimit $asr.concurrency (int64 $mediaMax) (int64 $neededMiB)) -}}
{{- end -}}
{{- /* The service's own start-up checks (settings.py), so a bad combination fails here already. */ -}}
{{- $budget := include "text-extraction.configNumber" (list . "DOCLING_INPUT_BUDGET_BYTES" 200000000) | float64 -}}
{{- if gt (float64 $asr.uploadMaxBytes) $budget -}}
{{- fail (printf "workers.asr.uploadMaxBytes (%v) exceeds DOCLING_INPUT_BUDGET_BYTES (%v), the budget of all queued inputs: every pod would exit with code 2. Lower it or raise config.DOCLING_INPUT_BUDGET_BYTES (and valkey.maxmemory)." (int64 $asr.uploadMaxBytes) (int64 $budget)) -}}
{{- end -}}
{{- $asrTimeout := include "text-extraction.configNumber" (list . "ASR_TIMEOUT_SECONDS" 10800) | float64 -}}
{{- $inputTtl := include "text-extraction.configNumber" (list . "ASR_INPUT_TTL_SECONDS" 28800) | float64 -}}
{{- if le $inputTtl (addf (mulf 2.0 $asrTimeout) 10.0) -}}
{{- fail (printf "ASR_INPUT_TTL_SECONDS (%v) must be longer than the ASR lease, 2 x ASR_TIMEOUT_SECONDS (%v) + 10: every pod would exit with code 2. Raise config.ASR_INPUT_TTL_SECONDS with the timeout." (int64 $inputTtl) (int64 $asrTimeout)) -}}
{{- end -}}
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
{{- $asrGraceful := include "text-extraction.asrGracefulSeconds" . | float64 -}}
{{- if and $asr.enabled (gt $asrGraceful (subf (float64 $asr.terminationGracePeriodSeconds) 10.0)) -}}
{{- fail (printf "workers.asr.terminationGracePeriodSeconds (%v) is too short: the ASR workers give transcriptions in flight %v s + a 10 s margin must fit." $asr.terminationGracePeriodSeconds $asrGraceful) -}}
{{- end -}}
{{- $nlpGraceful := include "text-extraction.nlpGracefulSeconds" . | float64 -}}
{{- if and $nlp.enabled (gt $nlpGraceful (subf (float64 $nlp.terminationGracePeriodSeconds) 10.0)) -}}
{{- fail (printf "workers.nlp.terminationGracePeriodSeconds (%v) is too short: the NLP workers give analyses in flight %v s + a 10 s margin must fit." $nlp.terminationGracePeriodSeconds $nlpGraceful) -}}
{{- end -}}
{{- /* The llm workers give calls in flight 30 s and then hand the items back to the queue at once,
which takes no time to speak of; the kill comes after the grace period, so no margin is required. */ -}}
{{- $llmGraceful := include "text-extraction.llmGracefulSeconds" . | float64 -}}
{{- if and $llm.enabled (gt $llmGraceful (float64 $llm.terminationGracePeriodSeconds)) -}}
{{- fail (printf "workers.llm.terminationGracePeriodSeconds (%v) is too short: the llm workers give calls in flight %v s before they hand the items back to the queue." $llm.terminationGracePeriodSeconds $llmGraceful) -}}
{{- end -}}
{{- end -}}

{{/*
The KEDA threshold of a pool (list $root $pool): items per replica. autoscaling.keda.threshold,
or the pool's concurrency when it is null.
*/}}
{{- define "text-extraction.kedaThreshold" -}}
{{- $worker := index (index . 0).Values.workers (index . 1) -}}
{{- $threshold := $worker.autoscaling.keda.threshold -}}
{{- if kindIs "invalid" $threshold -}}
{{- $threshold = $worker.concurrency -}}
{{- end -}}
{{- $threshold | toString -}}
{{- end }}

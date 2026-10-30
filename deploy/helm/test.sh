#!/usr/bin/env bash
# Render checks of the text-extraction chart: what `helm lint` cannot see. Run from anywhere:
#   deploy/helm/test.sh            (uses `helm` from PATH; HELM=/path/to/helm overrides it)
# Every check renders the chart with `helm template` and inspects the output with grep, so the
# script needs nothing but bash, grep and helm.
set -euo pipefail

HELM="${HELM:-helm}"
CHART="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/text-extraction"
RELEASE=rel
NAMESPACE=ns
API="$RELEASE-text-extraction"
EXTRACT="$RELEASE-text-extraction-worker-extract"
BROWSER="$RELEASE-text-extraction-worker-browser"
DOCLING="$RELEASE-text-extraction-worker-docling"
MODELS="$RELEASE-text-extraction-docling-models"
ASR="$RELEASE-text-extraction-worker-asr"
ASR_MODELS="$RELEASE-text-extraction-asr-models"
NLP="$RELEASE-text-extraction-worker-nlp"
NLP_MODELS="$RELEASE-text-extraction-nlp-models"
LLM="$RELEASE-text-extraction-worker-llm"
APP_VERSION="$(sed -n 's/^appVersion: *"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$CHART/Chart.yaml")"

failures=0

render() {
  "$HELM" template "$RELEASE" "$CHART" --namespace "$NAMESPACE" "$@"
}

fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

pass() {
  echo "ok: $*"
}

# The values of env var $2 in the rendered output $1, one per line, in document order.
env_values() {
  { grep -A1 -E -- "^ *- name: $2\$" <<<"$1" || true; } | sed -n 's/^ *value: *"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p'
}

# One document (by kind and metadata name) of the rendered output $1.
document() {
  local output="$1" kind="$2" name="$3"
  awk -v kind="kind: $kind" -v name="  name: $name" '
    /^---/ { if (match_kind && match_name) { printf "%s", doc }; doc = ""; match_kind = 0; match_name = 0; next }
    { doc = doc $0 "\n" }
    $0 == kind { match_kind = 1 }
    $0 == name { match_name = 1 }
    END { if (match_kind && match_name) { printf "%s", doc } }
  ' <<<"$output"
}

expect_values() {
  local label="$1" output="$2" var="$3" expected="$4" actual
  actual="$(env_values "$output" "$var" | tr '\n' ' ' | sed 's/ $//')"
  if [[ "$actual" == "$expected" ]]; then
    pass "$label: $var = $actual"
  else
    fail "$label: $var is '$actual', expected '$expected'"
  fi
}

# Every value of env var $3 in $2 is $4, and there is at least one.
expect_every_value() {
  local label="$1" output="$2" var="$3" expected="$4" values
  values="$(env_values "$output" "$var")"
  if [[ -n "$values" ]] && ! grep -qvxF -- "$expected" <<<"$values"; then
    pass "$label: every $var = $expected"
  else
    fail "$label: $var is '$(tr '\n' ' ' <<<"$values")', expected only '$expected'"
  fi
}

expect_contains() {
  local label="$1" output="$2" needle="$3"
  if grep -qF -- "$needle" <<<"$output"; then pass "$label"; else fail "$label: '$needle' missing"; fi
}

# The output $1 on one line, runs of blanks squeezed: multi-line YAML paths become greppable.
flat() {
  tr '\n' ' ' <<<"$1" | tr -s ' '
}

expect_absent() {
  local label="$1" output="$2" needle="$3"
  if grep -qF -- "$needle" <<<"$output"; then fail "$label: '$needle' present"; else pass "$label"; fi
}

expect_render_fails() {
  local label="$1" message="$2" output
  shift 2
  if output="$(render "$@" 2>&1)"; then
    fail "$label: the render succeeded"
  elif grep -qF -- "$message" <<<"$output"; then
    pass "$label"
  else
    fail "$label: failed without '$message': $output"
  fi
}

# NOTES.txt as `helm install` would print it (`helm template` leaves it out): a copy of the chart
# renders the file through `tpl` in a ConfigMap.
render_notes() {
  local copy status=0
  copy="$(mktemp -d)"
  cp -r "$CHART" "$copy/chart"
  cp "$CHART/templates/NOTES.txt" "$copy/chart/notes.txt"
  printf '%s\n' 'apiVersion: v1' 'kind: ConfigMap' 'metadata:' '  name: notes' 'data:' '  notes: |' \
    '{{ tpl (.Files.Get "notes.txt") . | indent 4 }}' >"$copy/chart/templates/zz-notes.yaml"
  "$HELM" template "$RELEASE" "$copy/chart" --namespace "$NAMESPACE" --show-only templates/zz-notes.yaml "$@" || status=$?
  rm -rf "$copy"
  return "$status"
}

expect_render_succeeds() {
  local label="$1"
  shift
  if render "$@" >/dev/null; then pass "$label"; else fail "$label: the render failed"; fi
}

# --- Browser workers on (the default) ------------------------------------------------------------

on="$(render)"
api="$(document "$on" Deployment "$API")"
extract="$(document "$on" Deployment "$EXTRACT")"
browser="$(document "$on" Deployment "$BROWSER")"

if [[ -n "$browser" ]]; then pass "browser worker Deployment is rendered by default"; else fail "no browser worker Deployment by default"; fi
expect_contains "browser worker runs its entry point" "$browser" "- text-extraction-browser-worker"
expect_contains "browser worker uses the browser image" "$browser" "image: \"ghcr.io/janschachtschabel/text-extraction-browser:$APP_VERSION\""
expect_contains "browser worker serves the internal port" "$browser" "containerPort: 9090"
expect_absent "browser worker has no public port" "$browser" "containerPort: 8080"
expect_contains "browser worker root filesystem is read-only" "$browser" "readOnlyRootFilesystem: true"
expect_contains "browser worker /tmp is bounded" "$browser" "sizeLimit: 2Gi"
expect_contains "browser worker grace period" "$browser" "terminationGracePeriodSeconds: 150"
expect_contains "browser worker memory request" "$browser" "memory: 2Gi"
expect_contains "browser worker memory limit" "$browser" "memory: 4608Mi"
expect_values "browser worker" "$browser" BROWSER_CONCURRENCY "4"
expect_absent "browser worker gets no WORKER_CONCURRENCY" "$browser" "name: WORKER_CONCURRENCY"
expect_contains "browser worker internal Service" "$(document "$on" Service "$BROWSER-internal")" "port: 9090"

expect_values "API" "$api" BROWSER_ENABLED "true"
expect_values "extract worker" "$extract" BROWSER_ENABLED "true"
expect_values "browser worker" "$browser" BROWSER_ENABLED "true"
expect_values "API" "$api" BROWSER_UNBLOCK_ENABLED "false"
expect_values "extract worker" "$extract" BROWSER_UNBLOCK_ENABLED "false"
expect_values "browser worker" "$browser" BROWSER_UNBLOCK_ENABLED "false"
# drain 5 + (25 + 10 + 30 + 10) + 10
expect_contains "API grace period covers the browser wait" "$api" "terminationGracePeriodSeconds: 90"

# config.* entries do not win over the toggles: the env entries override envFrom.
overridden="$(render --set config.BROWSER_ENABLED=false --set config.BROWSER_UNBLOCK_ENABLED=true)"
expect_values "config.BROWSER_ENABLED ignored (API)" "$(document "$overridden" Deployment "$API")" BROWSER_ENABLED "true"
expect_values "config.BROWSER_UNBLOCK_ENABLED ignored (browser worker)" "$(document "$overridden" Deployment "$BROWSER")" BROWSER_UNBLOCK_ENABLED "false"

# --- Unblock mode -------------------------------------------------------------------------------

unblock="$(render --set workers.browser.unblock=true)"
for name in "$API" "$EXTRACT" "$BROWSER"; do
  expect_values "unblock ($name)" "$(document "$unblock" Deployment "$name")" BROWSER_UNBLOCK_ENABLED "true"
done
# drain 5 + (25 + 10 + 60 + 10) + 10
expect_contains "API grace period covers an unblock render" "$(document "$unblock" Deployment "$API")" "terminationGracePeriodSeconds: 120"
expect_render_fails "unblock needs browser workers" "workers.browser.unblock needs workers.browser.enabled" \
  --set workers.browser.enabled=false --set workers.browser.unblock=true

# --- Browser workers off ------------------------------------------------------------------------

off="$(render --set workers.browser.enabled=false)"
expect_absent "no browser worker object when disabled" "$off" "$BROWSER"
expect_values "API (browser off)" "$(document "$off" Deployment "$API")" BROWSER_ENABLED "false"
expect_values "extract worker (browser off)" "$(document "$off" Deployment "$EXTRACT")" BROWSER_ENABLED "false"
expect_contains "API grace period without the browser" "$(document "$off" Deployment "$API")" "terminationGracePeriodSeconds: 45"

# --- Autoscaling --------------------------------------------------------------------------------

keda="$(render --set workers.browser.autoscaling.keda.enabled=true)"
scaled="$(document "$keda" ScaledObject "$BROWSER")"
expect_contains "browser KEDA query names pool browser" "$scaled" 'pool=\"browser\"'
expect_contains "browser KEDA threshold" "$scaled" 'threshold: "8"'
expect_absent "browser Deployment leaves replicas to KEDA" "$(document "$keda" Deployment "$BROWSER")" "replicas:"
extract_keda="$(document "$(render --set workers.extract.autoscaling.keda.enabled=true)" ScaledObject "$EXTRACT")"
expect_contains "extract KEDA keeps pool extract" "$extract_keda" 'pool=\"extract\"'
expect_absent "extract KEDA counts only pending items" "$extract_keda" "leased_items"
expect_absent "extract KEDA keeps the default scale-down behaviour" "$extract_keda" "horizontalPodAutoscalerConfig"
expect_absent "extract HPA keeps the default scale-down behaviour" \
  "$(document "$(render --set workers.extract.autoscaling.hpa.enabled=true)" HorizontalPodAutoscaler "$EXTRACT")" "behavior:"
expect_contains "a pool's scale-down window reaches its ScaledObject" \
  "$(flat "$(document "$(render --set workers.extract.autoscaling.keda.enabled=true --set workers.extract.autoscaling.scaleDownStabilizationWindowSeconds=900)" ScaledObject "$EXTRACT")")" \
  "advanced: horizontalPodAutoscalerConfig: behavior: scaleDown: stabilizationWindowSeconds: 900"
expect_contains "browser HPA" "$(document "$(render --set workers.browser.autoscaling.hpa.enabled=true)" HorizontalPodAutoscaler "$BROWSER")" "name: $BROWSER"
expect_render_fails "browser KEDA and HPA are exclusive" "mutually exclusive" \
  --set workers.browser.autoscaling.keda.enabled=true --set workers.browser.autoscaling.hpa.enabled=true
expect_render_fails "browser KEDA needs jobs" "JOBS_ENABLED=true" \
  --set workers.browser.autoscaling.keda.enabled=true --set jobs.enabled=false

# --- Grace periods ------------------------------------------------------------------------------

expect_render_fails "API grace period too short for the browser wait" "too short for the API pods" \
  --set terminationGracePeriodSeconds=89
expect_render_succeeds "API grace period just long enough" --set terminationGracePeriodSeconds=90
expect_render_fails "API grace period too short for unblock" "too short for the API pods" \
  --set workers.browser.unblock=true --set terminationGracePeriodSeconds=119
expect_render_fails "API grace period follows config.BROWSER_TIMEOUT_SECONDS" "too short for the API pods" \
  --set terminationGracePeriodSeconds=90 --set-string config.BROWSER_TIMEOUT_SECONDS=31
expect_render_succeeds "API grace period of 45 without the browser" \
  --set workers.browser.enabled=false --set terminationGracePeriodSeconds=45
# browser worker: max(30, 25 + 5, render deadline + 10) + 10
expect_render_fails "browser worker grace period too short" "workers.browser.terminationGracePeriodSeconds" \
  --set workers.browser.terminationGracePeriodSeconds=49
expect_render_succeeds "browser worker grace period just long enough" \
  --set workers.browser.terminationGracePeriodSeconds=50
expect_render_fails "browser worker grace period too short for unblock" "workers.browser.terminationGracePeriodSeconds" \
  --set workers.browser.unblock=true --set workers.browser.terminationGracePeriodSeconds=79
expect_render_fails "browser worker grace period follows config.BROWSER_UNBLOCK_TIMEOUT_SECONDS" "workers.browser.terminationGracePeriodSeconds" \
  --set workers.browser.unblock=true --set-string config.BROWSER_UNBLOCK_TIMEOUT_SECONDS=141
expect_render_fails "extract worker grace period still checked" "workers.extract.terminationGracePeriodSeconds" \
  --set workers.extract.terminationGracePeriodSeconds=39

# --- Node selection -----------------------------------------------------------------------------

expect_contains "browser workers stay on amd64 nodes" "$browser" "kubernetes.io/arch: amd64"
expect_absent "extract workers are not pinned to an arch" "$extract" "kubernetes.io/arch"
selected="$(render --set nodeSelector.disk=ssd --set 'nodeSelector.kubernetes\.io/arch=arm64')"
expect_contains "browser workers keep the global nodeSelector" "$(document "$selected" Deployment "$BROWSER")" "disk: ssd"
expect_contains "the pool's nodeSelector wins over the global one" "$(document "$selected" Deployment "$BROWSER")" "kubernetes.io/arch: amd64"
expect_contains "extract workers get the global nodeSelector" "$(document "$selected" Deployment "$EXTRACT")" "kubernetes.io/arch: arm64"
expect_absent "the arch pin can be dropped" \
  "$(document "$(render --set 'workers.browser.nodeSelector.kubernetes\.io/arch=null')" Deployment "$BROWSER")" "nodeSelector:"

# --- Network policies and alerts ----------------------------------------------------------------

policies="$(render --set networkPolicy.enabled=true)"
expect_contains "browser worker NetworkPolicy" "$(document "$policies" NetworkPolicy "$BROWSER")" "port: 9090"
expect_contains "Valkey accepts the browser workers" \
  "$(document "$policies" NetworkPolicy "$RELEASE-text-extraction-valkey")" "app.kubernetes.io/name: text-extraction-worker-browser"

rules="$(render --set metrics.prometheusRule.enabled=true)"
for pool in extract browser; do
  expect_contains "backlog alert for pool $pool" "$rules" "text_extraction_queue_pending_items{namespace=\"$NAMESPACE\", pool=\"$pool\"}) > 1000"
  expect_contains "workers-down alert for pool $pool" "$rules" "deployment=\"$RELEASE-text-extraction-worker-$pool\""
done
expect_absent "no browser alerts without browser workers" \
  "$(render --set metrics.prometheusRule.enabled=true --set workers.browser.enabled=false)" 'pool="browser"'

# --- Init process -------------------------------------------------------------------------------

# The images start tini as PID 1 (it reaps orphaned children such as tesseract): the workers pass
# their entry point as args, so the image's ENTRYPOINT stays; `command:` would replace it.
expect_absent "workers keep the image's init process" "$extract$browser" "command:"
expect_contains "extract worker passes its entry point as args" "$extract" "- text-extraction-worker"

# --- Document conversion memory -----------------------------------------------------------------

# Each MarkItDown child gets floor((limit MiB - documentParentReserveMiB) / DOCUMENT_CONCURRENCY)
# of address space: (2560 - 1024) / 2 in the API and extract pods, (4608 - 3072) / 2 in the
# browser pods. The reserves hold the selector child (STRUCTURED_SELECTOR_MEMORY_MB, 512) beside
# the server (512) and Chromium (2048).
expect_contains "API memory limit" "$api" "memory: 2560Mi"
expect_contains "extract worker memory limit" "$extract" "memory: 2560Mi"
expect_contains "selector child memory" "$(document "$on" ConfigMap "$API")" "STRUCTURED_SELECTOR_MEMORY_MB: \"512\""
for name in "$API" "$EXTRACT" "$BROWSER"; do
  expect_values "document memory ($name)" "$(document "$on" Deployment "$name")" DOCUMENT_MEMORY_LIMIT_MB "768"
done
expect_values "config.DOCUMENT_MEMORY_LIMIT_MB wins (API)" \
  "$(document "$(render --set-string config.DOCUMENT_MEMORY_LIMIT_MB=1000)" Deployment "$API")" DOCUMENT_MEMORY_LIMIT_MB ""
expect_values "config.DOCUMENT_MEMORY_LIMIT_MB wins (browser worker)" \
  "$(document "$(render --set-string config.DOCUMENT_MEMORY_LIMIT_MB=1000)" Deployment "$BROWSER")" DOCUMENT_MEMORY_LIMIT_MB ""
expect_values "document memory follows the limit" \
  "$(document "$(render --set resources.limits.memory=3Gi)" Deployment "$API")" DOCUMENT_MEMORY_LIMIT_MB "1024"
expect_values "document memory follows DOCUMENT_CONCURRENCY" \
  "$(document "$(render --set resources.limits.memory=3Gi --set-string config.DOCUMENT_CONCURRENCY=4)" Deployment "$API")" DOCUMENT_MEMORY_LIMIT_MB "512"
expect_values "document memory from Mi" \
  "$(document "$(render --set workers.extract.resources.limits.memory=3072Mi)" Deployment "$EXTRACT")" DOCUMENT_MEMORY_LIMIT_MB "1024"
# 2G = 1907.3 MiB: floor((1907.3 - 1024) / 2)
expect_values "document memory from G" \
  "$(document "$(render --set resources.limits.memory=2G)" Deployment "$API")" DOCUMENT_MEMORY_LIMIT_MB "441"
expect_values "document memory from bytes" \
  "$(document "$(render --set-string resources.limits.memory=2147483648)" Deployment "$API")" DOCUMENT_MEMORY_LIMIT_MB "512"
expect_values "document memory follows the reserve" \
  "$(document "$(render --set workers.browser.documentParentReserveMiB=2560)" Deployment "$BROWSER")" DOCUMENT_MEMORY_LIMIT_MB "1024"
expect_values "no memory limit, no document memory" \
  "$(document "$(render --set resources.limits.memory=null)" Deployment "$API")" DOCUMENT_MEMORY_LIMIT_MB ""
# (1536 - 1024) / 2 = 256 is the minimum; 1512Mi leaves 244.
expect_render_succeeds "document memory at the minimum" --set resources.limits.memory=1536Mi
expect_render_fails "document memory below the minimum (API)" "resources.limits.memory (1512Mi) leaves 244 MiB" \
  --set resources.limits.memory=1512Mi
expect_render_fails "document memory below the minimum (extract worker)" "workers.extract.resources.limits.memory (1512Mi) leaves 244 MiB" \
  --set workers.extract.resources.limits.memory=1512Mi
expect_render_succeeds "config.DOCUMENT_MEMORY_LIMIT_MB skips the minimum" \
  --set resources.limits.memory=1512Mi --set-string config.DOCUMENT_MEMORY_LIMIT_MB=400

# --- Docling workers ----------------------------------------------------------------------------

expect_absent "docling off renders nothing" "$on" "docling"
expect_absent "docling off renders no PVC" "$on" "kind: PersistentVolumeClaim"
for name in "$API" "$EXTRACT" "$BROWSER"; do
  expect_values "docling off ($name)" "$(document "$on" Deployment "$name")" DOCLING_ENABLED "false"
done

expect_values "config.DOCLING_ENABLED ignored (API)" \
  "$(document "$(render --set config.DOCLING_ENABLED=true)" Deployment "$API")" DOCLING_ENABLED "false"

docling_on="$(render --set workers.docling.enabled=true)"
docling="$(document "$docling_on" Deployment "$DOCLING")"
if [[ -n "$docling" ]]; then pass "docling worker Deployment is rendered when enabled"; else fail "no docling worker Deployment"; fi
for name in "$API" "$EXTRACT" "$BROWSER"; do
  expect_values "docling on ($name)" "$(document "$docling_on" Deployment "$name")" DOCLING_ENABLED "true"
done
# The init container and the worker.
expect_values "docling on (docling worker and init container)" "$docling" DOCLING_ENABLED "true true"
expect_values "docling worker and init container" "$docling" DOCLING_MODELS_PATH "/models /models"
expect_values "docling worker" "$docling" DOCLING_CONCURRENCY "1 1"
expect_absent "docling worker gets no WORKER_CONCURRENCY" "$docling" "name: WORKER_CONCURRENCY"
expect_absent "docling worker gets no DOCUMENT_MEMORY_LIMIT_MB" "$docling" "name: DOCUMENT_MEMORY_LIMIT_MB"
expect_contains "docling worker uses the docling image" "$docling" "image: \"ghcr.io/janschachtschabel/text-extraction-docling:$APP_VERSION\""
expect_contains "docling worker runs its entry point" "$docling" "- text-extraction-docling-worker"
expect_absent "docling worker keeps the image's init process" "$docling" "command:"
expect_contains "docling worker serves the internal port" "$docling" "containerPort: 9090"
expect_absent "docling worker has no public port" "$docling" "containerPort: 8080"
expect_contains "docling worker grace period" "$docling" "terminationGracePeriodSeconds: 60"
expect_contains "docling worker memory request" "$docling" "memory: 3Gi"
expect_contains "docling worker memory limit" "$docling" "memory: 8Gi"
expect_contains "docling workers stay on amd64 nodes" "$docling" "kubernetes.io/arch: amd64"
expect_contains "docling worker internal Service" "$(document "$docling_on" Service "$DOCLING-internal")" "port: 9090"
# The init container downloads into a writable mount, the worker reads it read-only.
init="$(awk '/^      initContainers:/ { f = 1 } /^      containers:/ { f = 0 } f' <<<"$docling")"
worker="$(awk '/^      containers:/ { f = 1 } /^      volumes:/ { f = 0 } f' <<<"$docling")"
expect_contains "init container downloads the models" "$init" "- --download-models"
expect_contains "init container mounts the models" "$init" "mountPath: /models"
expect_absent "init container mount is writable" "$init" "readOnly: true"
expect_contains "init container root filesystem is read-only" "$init" "readOnlyRootFilesystem: true"
expect_contains "worker mounts the models" "$worker" "mountPath: /models"
expect_contains "worker mount is read-only" "$worker" "readOnly: true"
expect_absent "worker does not download" "$worker" "--download-models"
pvc="$(document "$docling_on" PersistentVolumeClaim "$MODELS")"
expect_contains "models PVC is rendered" "$pvc" "storage: 5Gi"
expect_contains "models PVC is ReadWriteMany" "$pvc" "- ReadWriteMany"
expect_absent "models PVC uses the default storage class" "$pvc" "storageClassName"
expect_contains "worker uses the models PVC" "$docling" "claimName: $MODELS"
expect_contains "storageClass is passed on" \
  "$(document "$(render --set workers.docling.enabled=true --set workers.docling.models.persistence.storageClass=nfs)" PersistentVolumeClaim "$MODELS")" \
  'storageClassName: "nfs"'

claimed="$(render --set workers.docling.enabled=true --set workers.docling.models.persistence.existingClaim=my-models)"
expect_absent "existingClaim skips the PVC" "$claimed" "kind: PersistentVolumeClaim"
expect_contains "existingClaim is mounted" "$(document "$claimed" Deployment "$DOCLING")" "claimName: my-models"
expect_contains "existingClaim still downloads by default" "$(document "$claimed" Deployment "$DOCLING")" "- --download-models"

prefilled="$(render --set workers.docling.enabled=true --set workers.docling.models.persistence.existingClaim=my-models --set workers.docling.models.download=false)"
expect_absent "download false skips the init container" "$(document "$prefilled" Deployment "$DOCLING")" "initContainers:"
expect_absent "download false runs no download" "$prefilled" "--download-models"
expect_render_fails "download false needs a pre-filled existingClaim" "workers.docling.models.download=false needs" \
  --set workers.docling.enabled=true --set workers.docling.models.download=false

expect_render_fails "docling KEDA and HPA are exclusive" "mutually exclusive" \
  --set workers.docling.enabled=true --set workers.docling.autoscaling.keda.enabled=true \
  --set workers.docling.autoscaling.hpa.enabled=true
expect_contains "docling KEDA query names pool docling" \
  "$(document "$(render --set workers.docling.enabled=true --set workers.docling.autoscaling.keda.enabled=true)" ScaledObject "$DOCLING")" \
  'pool=\"docling\"'
expect_render_fails "docling needs jobs" "workers.docling.enabled needs JOBS_ENABLED=true" \
  --set workers.docling.enabled=true --set jobs.enabled=false

# API: drain 5 + max(30, 25 + 5, 25 + DOCLING_SYNC_WAIT_SECONDS 20 + 5) + 10 without browser
# workers; with them the browser term (75) is the larger one.
docling_only="$(render --set workers.docling.enabled=true --set workers.browser.enabled=false)"
expect_contains "API grace period covers the docling wait" "$(document "$docling_only" Deployment "$API")" "terminationGracePeriodSeconds: 65"
expect_contains "API grace period with browser and docling" "$(document "$docling_on" Deployment "$API")" "terminationGracePeriodSeconds: 90"
expect_contains "API grace period follows config.DOCLING_SYNC_WAIT_SECONDS" \
  "$(document "$(render --set workers.docling.enabled=true --set workers.browser.enabled=false --set-string config.DOCLING_SYNC_WAIT_SECONDS=25)" Deployment "$API")" \
  "terminationGracePeriodSeconds: 70"
expect_render_fails "API grace period too short for the docling wait" "too short for the API pods" \
  --set workers.docling.enabled=true --set workers.browser.enabled=false --set terminationGracePeriodSeconds=64
expect_render_succeeds "API grace period just long enough for the docling wait" \
  --set workers.docling.enabled=true --set workers.browser.enabled=false --set terminationGracePeriodSeconds=65
# docling worker: 30 s for the items in flight + 10
expect_render_fails "docling worker grace period too short" "workers.docling.terminationGracePeriodSeconds" \
  --set workers.docling.enabled=true --set workers.docling.terminationGracePeriodSeconds=39
expect_render_succeeds "docling worker grace period just long enough" \
  --set workers.docling.enabled=true --set workers.docling.terminationGracePeriodSeconds=40

# Valkey holds the queued Docling inputs: the notes point at maxmemory while it is the default.
expect_contains "notes warn about the default maxmemory with docling" \
  "$(render_notes --set workers.docling.enabled=true)" "DOCLING_INPUT_BUDGET_BYTES (200000000)"
expect_contains "notes name config.DOCLING_INPUT_BUDGET_BYTES" \
  "$(render_notes --set workers.docling.enabled=true --set-string config.DOCLING_INPUT_BUDGET_BYTES=50000000)" \
  "DOCLING_INPUT_BUDGET_BYTES (50000000)"
expect_absent "no maxmemory note with a raised maxmemory" \
  "$(render_notes --set workers.docling.enabled=true --set valkey.maxmemory=2gb)" "DOCLING_INPUT_BUDGET_BYTES"
expect_absent "no maxmemory note without docling" "$(render_notes)" "DOCLING_INPUT_BUDGET_BYTES"

docling_policies="$(render --set workers.docling.enabled=true --set networkPolicy.enabled=true)"
expect_contains "docling worker NetworkPolicy" "$(document "$docling_policies" NetworkPolicy "$DOCLING")" "port: 9090"
expect_contains "Valkey accepts the docling workers" \
  "$(document "$docling_policies" NetworkPolicy "$RELEASE-text-extraction-valkey")" "app.kubernetes.io/name: text-extraction-worker-docling"

docling_rules="$(render --set workers.docling.enabled=true --set metrics.prometheusRule.enabled=true --set alerts.doclingQueueBacklogItems=7)"
expect_contains "docling backlog alert" "$docling_rules" "text_extraction_queue_pending_items{namespace=\"$NAMESPACE\", pool=\"docling\"}) > 7"
expect_contains "extract backlog keeps its threshold" "$docling_rules" "text_extraction_queue_pending_items{namespace=\"$NAMESPACE\", pool=\"extract\"}) > 1000"
expect_contains "docling workers-down alert" "$docling_rules" "deployment=\"$DOCLING\""
expect_contains "docling items are counted on the docling workers" "$docling_rules" "service=\"$DOCLING-internal\""
expect_contains "extract items leave out the browser, docling, asr and nlp workers" "$docling_rules" "service!~\"$BROWSER-internal|$DOCLING-internal|$ASR-internal|$NLP-internal|$LLM-internal\""
expect_contains "Valkey memory alert reads the docling workers" "$docling_rules" "|$DOCLING-internal|$ASR-internal|$NLP-internal|$LLM-internal\"})"
expect_absent "no docling alerts without docling workers" "$rules" 'pool="docling"'

# --- Model downloads as a hook Job (models.downloadMode job) -----------------------------------

jobs_mode="$(render --set workers.docling.enabled=true --set workers.docling.models.downloadMode=job \
  --set workers.asr.enabled=true --set workers.asr.models.downloadMode=job)"
for pool in docling asr; do
  name="$RELEASE-text-extraction-worker-$pool"
  job="$(document "$jobs_mode" Job "$name-models")"
  if [[ -n "$job" ]]; then pass "downloadMode job renders a $pool models Job"; else fail "no $pool models Job"; fi
  expect_contains "$pool models Job is a post-install and post-upgrade hook" "$job" '"helm.sh/hook": post-install,post-upgrade'
  expect_contains "$pool models Job downloads the models" "$job" "- --download-models"
  expect_contains "$pool models Job mounts the models" "$job" "mountPath: /models"
  expect_absent "$pool models Job mount is writable" "$job" "readOnly: true"
  expect_contains "$pool models Job root filesystem is read-only" "$job" "readOnlyRootFilesystem: true"
  expect_contains "$pool models Job runs as the image's user" "$job" "runAsUser: 10001"
  expect_absent "$pool models Job keeps the image's init process" "$job" "command:"
  expect_contains "$pool models Job requests little memory" "$(flat "$job")" "requests: cpu: 100m memory: 256Mi"
  expect_contains "$pool models Job has a small memory limit" "$(flat "$job")" "limits: cpu: \"1\" memory: 1Gi"
  deployment="$(document "$jobs_mode" Deployment "$name")"
  expect_absent "downloadMode job renders no init container ($pool)" "$deployment" "initContainers:"
  expect_absent "downloadMode job: the $pool worker does not download" "$deployment" "--download-models"
  expect_contains "downloadMode job: the $pool worker mounts the models read-only" "$deployment" "readOnly: true"
done
expect_contains "docling models Job uses the docling image" "$(document "$jobs_mode" Job "$DOCLING-models")" \
  "image: \"ghcr.io/janschachtschabel/text-extraction-docling:$APP_VERSION\""
expect_contains "asr models Job uses the asr image" "$(document "$jobs_mode" Job "$ASR-models")" \
  "image: \"ghcr.io/janschachtschabel/text-extraction-asr:$APP_VERSION\""
expect_values "asr models Job downloads the configured model" "$(document "$jobs_mode" Job "$ASR-models")" ASR_MODEL "large-v3-turbo"
expect_absent "downloadMode init (the default) renders no Job" "$(render --set workers.docling.enabled=true --set workers.asr.enabled=true)" "kind: Job"
expect_absent "download false renders no Job" \
  "$(render --set workers.docling.enabled=true --set workers.docling.models.downloadMode=job --set workers.docling.models.download=false --set workers.docling.models.persistence.existingClaim=m)" \
  "kind: Job"
expect_render_fails "downloadMode is init or job" "workers.docling.models.downloadMode must be init or job" \
  --set workers.docling.enabled=true --set workers.docling.models.downloadMode=sidecar

# --- ASR workers --------------------------------------------------------------------------------

expect_absent "asr off renders nothing" "$on" "worker-asr"
expect_absent "asr off renders no models PVC" "$on" "$ASR_MODELS"
for name in "$API" "$EXTRACT" "$BROWSER"; do
  expect_values "asr off ($name)" "$(document "$on" Deployment "$name")" ASR_ENABLED "false"
done
expect_values "config.ASR_ENABLED ignored (API)" \
  "$(document "$(render --set config.ASR_ENABLED=true)" Deployment "$API")" ASR_ENABLED "false"
expect_values "API gets the upload limit" "$api" MEDIA_UPLOAD_MAX_BYTES "25000000"

asr_on="$(render --set workers.asr.enabled=true --set workers.docling.enabled=true)"
asr="$(document "$asr_on" Deployment "$ASR")"
if [[ -n "$asr" ]]; then pass "asr worker Deployment is rendered when enabled"; else fail "no asr worker Deployment"; fi
# Every container of every pod (the Docling init container included).
for name in "$API" "$EXTRACT" "$BROWSER" "$DOCLING" "$ASR"; do
  deployment="$(document "$asr_on" Deployment "$name")"
  expect_every_value "asr on ($name)" "$deployment" ASR_ENABLED "true"
  expect_every_value "asr backend ($name)" "$deployment" ASR_BACKEND "local"
done
# The init container and the worker.
expect_values "asr on (asr worker and init container)" "$asr" ASR_ENABLED "true true"
expect_values "asr worker and init container" "$asr" ASR_MODELS_PATH "/models /models"
expect_values "asr worker and init container" "$asr" ASR_MODEL "large-v3-turbo large-v3-turbo"
expect_values "asr worker" "$asr" ASR_CONCURRENCY "1 1"
expect_values "asr worker" "$asr" ASR_SCRATCH_PATH "/scratch /scratch"
expect_values "asr worker gets the upload limit" "$asr" MEDIA_UPLOAD_MAX_BYTES "25000000 25000000"
expect_values "upload limit follows workers.asr.uploadMaxBytes" \
  "$(document "$(render --set workers.asr.enabled=true --set workers.asr.uploadMaxBytes=50000000)" Deployment "$API")" \
  MEDIA_UPLOAD_MAX_BYTES "50000000"
expect_absent "asr worker gets no WORKER_CONCURRENCY" "$asr" "name: WORKER_CONCURRENCY"
expect_absent "asr worker gets no DOCUMENT_MEMORY_LIMIT_MB" "$asr" "name: DOCUMENT_MEMORY_LIMIT_MB"
expect_absent "asr worker gets no external key without the openai backend" "$asr" "ASR_EXTERNAL_API_KEY"
expect_contains "asr worker uses the asr image" "$asr" "image: \"ghcr.io/janschachtschabel/text-extraction-asr:$APP_VERSION\""
expect_contains "asr worker runs its entry point" "$asr" "- text-extraction-asr-worker"
expect_absent "asr worker keeps the image's init process" "$asr" "command:"
expect_contains "asr worker serves the internal port" "$asr" "containerPort: 9090"
expect_absent "asr worker has no public port" "$asr" "containerPort: 8080"
expect_contains "asr worker grace period" "$asr" "terminationGracePeriodSeconds: 60"
expect_contains "asr worker memory request" "$asr" "memory: 3Gi"
expect_contains "asr worker memory limit" "$asr" "memory: 6Gi"
expect_contains "asr worker CPU limit" "$asr" 'cpu: "4"'
expect_contains "asr workers stay on amd64 nodes" "$asr" "kubernetes.io/arch: amd64"
expect_contains "asr worker internal Service" "$(document "$asr_on" Service "$ASR-internal")" "port: 9090"
init="$(awk '/^      initContainers:/ { f = 1 } /^      containers:/ { f = 0 } f' <<<"$asr")"
worker="$(awk '/^      containers:/ { f = 1 } /^      volumes:/ { f = 0 } f' <<<"$asr")"
volumes="$(awk '/^      volumes:/ { f = 1 } /^      nodeSelector:/ { f = 0 } f' <<<"$asr")"
expect_contains "asr init container downloads the models" "$init" "- --download-models"
expect_contains "asr init container mounts the models" "$init" "mountPath: /models"
expect_absent "asr init container mount is writable" "$init" "readOnly: true"
expect_contains "asr init container root filesystem is read-only" "$init" "readOnlyRootFilesystem: true"
expect_contains "asr init container requests little memory" "$(flat "$init")" "requests: cpu: 100m memory: 256Mi"
expect_absent "asr init container does not take the worker's resources" "$init" "memory: 3Gi"
expect_contains "models resources follow workers.asr.models.resources" \
  "$(document "$(render --set workers.asr.enabled=true --set workers.asr.models.resources.limits.memory=2Gi)" Deployment "$ASR")" \
  "memory: 2Gi"
expect_contains "asr worker mounts the models" "$worker" "mountPath: /models"
expect_contains "asr worker models mount is read-only" "$worker" "readOnly: true"
expect_absent "asr worker does not download" "$worker" "--download-models"
expect_contains "asr worker mounts the scratch volume" "$worker" "mountPath: /scratch"
expect_contains "asr scratch is a bounded emptyDir" "$volumes" "sizeLimit: 2Gi"
expect_contains "asr /tmp is a bounded emptyDir" "$(flat "$volumes")" "- name: tmp emptyDir: sizeLimit: 1Gi"
asr_pvc="$(document "$asr_on" PersistentVolumeClaim "$ASR_MODELS")"
expect_contains "asr models PVC is rendered" "$asr_pvc" "storage: 4Gi"
expect_contains "asr models PVC is ReadWriteMany" "$asr_pvc" "- ReadWriteMany"
expect_absent "asr models PVC uses the default storage class" "$asr_pvc" "storageClassName"
expect_contains "asr worker uses the models PVC" "$asr" "claimName: $ASR_MODELS"
expect_contains "docling keeps its own models PVC" "$(document "$asr_on" Deployment "$DOCLING")" "claimName: $MODELS"
expect_contains "API grace period does not wait for asr" "$(document "$asr_on" Deployment "$API")" "terminationGracePeriodSeconds: 90"
expect_values "asr model follows workers.asr.model" \
  "$(document "$(render --set workers.asr.enabled=true --set workers.asr.model=small)" Deployment "$ASR")" ASR_MODEL "small small"
expect_render_fails "asr model is one of the manifest's" "workers.asr.model must be" \
  --set workers.asr.enabled=true --set workers.asr.model=tiny

asr_claimed="$(render --set workers.asr.enabled=true --set workers.asr.models.persistence.existingClaim=whisper)"
expect_absent "existingClaim skips the asr PVC" "$asr_claimed" "kind: PersistentVolumeClaim"
expect_contains "asr existingClaim is mounted" "$(document "$asr_claimed" Deployment "$ASR")" "claimName: whisper"
expect_absent "asr download false skips the init container" \
  "$(document "$(render --set workers.asr.enabled=true --set workers.asr.models.persistence.existingClaim=whisper --set workers.asr.models.download=false)" Deployment "$ASR")" \
  "initContainers:"
expect_render_fails "asr download false needs a pre-filled existingClaim" "workers.asr.models.download=false needs" \
  --set workers.asr.enabled=true --set workers.asr.models.download=false

# MEDIA_MAX_BYTES (500000000) plus 5 MB of slack per transcription slot must fit the scratch volume.
expect_render_fails "scratch sizeLimit smaller than MEDIA_MAX_BYTES fails the render" "workers.asr.scratch.sizeLimit (400Mi)" \
  --set workers.asr.enabled=true --set workers.asr.scratch.sizeLimit=400Mi
expect_render_succeeds "scratch sizeLimit follows config.MEDIA_MAX_BYTES" \
  --set workers.asr.enabled=true --set workers.asr.scratch.sizeLimit=400Mi --set-string config.MEDIA_MAX_BYTES=300000000
expect_render_fails "scratch sizeLimit covers every transcription slot" "workers.asr.scratch.sizeLimit (2Gi)" \
  --set workers.asr.enabled=true --set workers.asr.concurrency=5
expect_render_fails "scratch sizeLimit is required" "workers.asr.scratch.sizeLimit" \
  --set workers.asr.enabled=true --set workers.asr.scratch.sizeLimit=null

asr_external="$(render --set workers.asr.enabled=true --set workers.asr.backend=openai \
  --set workers.asr.external.baseUrl=https://asr.example.org/v1 --set workers.asr.external.existingSecret=asr-key)"
asr_ext="$(document "$asr_external" Deployment "$ASR")"
expect_absent "external backend renders no PVC" "$asr_external" "kind: PersistentVolumeClaim"
expect_absent "external backend renders no init container" "$asr_ext" "initContainers:"
expect_absent "external backend mounts no models" "$asr_ext" "mountPath: /models"
expect_absent "external backend renders no models Job" \
  "$(render --set workers.asr.enabled=true --set workers.asr.backend=openai --set workers.asr.external.baseUrl=https://asr.example.org/v1 --set workers.asr.models.downloadMode=job)" \
  "kind: Job"
expect_values "external backend (asr worker)" "$asr_ext" ASR_BACKEND "openai"
expect_values "external backend (asr worker)" "$asr_ext" ASR_EXTERNAL_BASE_URL "https://asr.example.org/v1"
expect_values "external backend (asr worker)" "$asr_ext" ASR_EXTERNAL_MODEL "whisper-1"
for name in "$API" "$ASR"; do
  deployment="$(document "$asr_external" Deployment "$name")"
  expect_contains "external backend takes the key from the secret ($name)" "$deployment" "name: asr-key"
  expect_contains "external backend key is a secret reference ($name)" \
    "$(grep -A4 -- "- name: ASR_EXTERNAL_API_KEY" <<<"$deployment")" "key: ASR_EXTERNAL_API_KEY"
  expect_values "external backend ($name)" "$deployment" ASR_BACKEND "openai"
done
for name in "$EXTRACT" "$BROWSER"; do
  expect_absent "external key stays out of $name" "$(document "$asr_external" Deployment "$name")" "ASR_EXTERNAL_API_KEY"
done
expect_absent "external key never in the ConfigMap" "$(document "$asr_external" ConfigMap "$API")" "ASR_EXTERNAL_API_KEY"

asr_managed="$(render --set workers.asr.enabled=true --set workers.asr.backend=openai \
  --set workers.asr.external.baseUrl=https://asr.example.org/v1 --set workers.asr.external.apiKey=sk-test-key)"
expect_contains "external key in a chart-managed Secret" "$(document "$asr_managed" Secret "$RELEASE-text-extraction-asr")" "ASR_EXTERNAL_API_KEY: \"sk-test-key\""
expect_contains "API reads the chart-managed key" "$(document "$asr_managed" Deployment "$API")" "name: $RELEASE-text-extraction-asr"
expect_absent "external key never in clear text in a pod" "$(document "$asr_managed" Deployment "$ASR")" "sk-test-key"
expect_absent "no key Secret for the local backend" \
  "$(render --set workers.asr.enabled=true --set workers.asr.external.apiKey=sk-test-key)" "sk-test-key"
expect_absent "external backend without a key renders no key" \
  "$(render --set workers.asr.enabled=true --set workers.asr.backend=openai --set workers.asr.external.baseUrl=https://asr.example.org/v1)" \
  "ASR_EXTERNAL_API_KEY"
expect_render_fails "external backend needs a base URL" "workers.asr.external.baseUrl is required" \
  --set workers.asr.enabled=true --set workers.asr.backend=openai
expect_render_fails "external base URL is https" "workers.asr.external.baseUrl must start with https://" \
  --set workers.asr.enabled=true --set workers.asr.backend=openai --set workers.asr.external.baseUrl=http://asr.example.org/v1
expect_render_fails "asr backend is local or openai" "workers.asr.backend must be local or openai" \
  --set workers.asr.enabled=true --set workers.asr.backend=cloud

expect_render_fails "asr needs jobs" "workers.asr.enabled needs JOBS_ENABLED=true" \
  --set workers.asr.enabled=true --set jobs.enabled=false
# D6: the input TTL must outlast the lease (2 x ASR_TIMEOUT_SECONDS + 10), as the service checks.
expect_render_fails "asr input TTL shorter than the lease" "ASR_INPUT_TTL_SECONDS (28800)" \
  --set workers.asr.enabled=true --set-string config.ASR_TIMEOUT_SECONDS=14400
expect_render_succeeds "asr input TTL raised with the timeout" \
  --set workers.asr.enabled=true --set-string config.ASR_TIMEOUT_SECONDS=14400 --set-string config.ASR_INPUT_TTL_SECONDS=30000
expect_render_fails "asr upload limit above the input budget" "workers.asr.uploadMaxBytes (300000000)" \
  --set workers.asr.enabled=true --set workers.asr.uploadMaxBytes=300000000
# asr worker: 30 s for the items in flight + 10
expect_render_fails "asr worker grace period too short" "workers.asr.terminationGracePeriodSeconds" \
  --set workers.asr.enabled=true --set workers.asr.terminationGracePeriodSeconds=39
expect_render_succeeds "asr worker grace period just long enough" \
  --set workers.asr.enabled=true --set workers.asr.terminationGracePeriodSeconds=40

asr_keda="$(document "$(render --set workers.asr.enabled=true --set workers.asr.autoscaling.keda.enabled=true)" ScaledObject "$ASR")"
expect_contains "asr KEDA query names pool asr" "$asr_keda" 'pool=\"asr\"'
expect_contains "asr KEDA query counts the transcriptions in flight" "$asr_keda" \
  "query: \"max(text_extraction_queue_pending_items{pool=\\\"asr\\\", namespace=\\\"$NAMESPACE\\\"}) + max(text_extraction_queue_leased_items{pool=\\\"asr\\\", namespace=\\\"$NAMESPACE\\\"})\""
expect_contains "asr KEDA threshold is the concurrency" "$asr_keda" 'threshold: "1"'
expect_contains "asr KEDA threshold follows the concurrency" \
  "$(document "$(render --set workers.asr.enabled=true --set workers.asr.autoscaling.keda.enabled=true --set workers.asr.concurrency=2)" ScaledObject "$ASR")" \
  'threshold: "2"'
expect_contains "asr KEDA threshold can be set" \
  "$(document "$(render --set workers.asr.enabled=true --set workers.asr.autoscaling.keda.enabled=true --set workers.asr.autoscaling.keda.threshold=3)" ScaledObject "$ASR")" \
  'threshold: "3"'
expect_contains "asr KEDA scales down after an hour of less work" "$(flat "$asr_keda")" \
  "advanced: horizontalPodAutoscalerConfig: behavior: scaleDown: stabilizationWindowSeconds: 3600"
asr_hpa="$(document "$(render --set workers.asr.enabled=true --set workers.asr.autoscaling.hpa.enabled=true)" HorizontalPodAutoscaler "$ASR")"
expect_contains "asr HPA scales down after an hour of less work" "$(flat "$asr_hpa")" \
  "behavior: scaleDown: stabilizationWindowSeconds: 3600"
expect_contains "asr HPA scale-down window can be set" \
  "$(document "$(render --set workers.asr.enabled=true --set workers.asr.autoscaling.hpa.enabled=true --set workers.asr.autoscaling.scaleDownStabilizationWindowSeconds=1800)" HorizontalPodAutoscaler "$ASR")" \
  "stabilizationWindowSeconds: 1800"
expect_contains "a scale-down window of 0 is rendered, not dropped" \
  "$(document "$(render --set workers.asr.enabled=true --set workers.asr.autoscaling.hpa.enabled=true --set workers.asr.autoscaling.scaleDownStabilizationWindowSeconds=0)" HorizontalPodAutoscaler "$ASR")" \
  "stabilizationWindowSeconds: 0"
expect_render_fails "scale-down window at most an hour (the Kubernetes limit)" \
  "workers.asr.autoscaling.scaleDownStabilizationWindowSeconds must be between 0 and 3600" \
  --set workers.asr.enabled=true --set workers.asr.autoscaling.scaleDownStabilizationWindowSeconds=7200
expect_render_fails "KEDA threshold is positive" "workers.asr.autoscaling.keda.threshold must be" \
  --set workers.asr.enabled=true --set workers.asr.autoscaling.keda.enabled=true --set workers.asr.autoscaling.keda.threshold=0
expect_contains "notes say a scale-down interrupts transcriptions" \
  "$(render_notes --set workers.asr.enabled=true)" "interrupts the transcriptions in flight"
expect_render_fails "asr KEDA and HPA are exclusive" "mutually exclusive" \
  --set workers.asr.enabled=true --set workers.asr.autoscaling.keda.enabled=true \
  --set workers.asr.autoscaling.hpa.enabled=true

asr_policies="$(render --set workers.asr.enabled=true --set networkPolicy.enabled=true)"
expect_contains "asr worker NetworkPolicy" "$(document "$asr_policies" NetworkPolicy "$ASR")" "port: 9090"
expect_contains "Valkey accepts the asr workers" \
  "$(document "$asr_policies" NetworkPolicy "$RELEASE-text-extraction-valkey")" "app.kubernetes.io/name: text-extraction-worker-asr"

asr_rules="$(render --set workers.asr.enabled=true --set metrics.prometheusRule.enabled=true --set alerts.asrQueueBacklogItems=9)"
expect_contains "asr backlog alert" "$asr_rules" "text_extraction_queue_pending_items{namespace=\"$NAMESPACE\", pool=\"asr\"}) > 9"
expect_contains "asr workers-down alert" "$asr_rules" "deployment=\"$ASR\""
expect_contains "asr items are counted on the asr workers" "$asr_rules" "service=\"$ASR-internal\""
expect_contains "extract items leave out the asr workers" "$asr_rules" "service!~\"$BROWSER-internal|$DOCLING-internal|$ASR-internal|$NLP-internal|$LLM-internal\""
expect_contains "Valkey memory alert reads the asr workers" "$asr_rules" "|$ASR-internal|$NLP-internal|$LLM-internal\"})"
expect_contains "asr backlog default threshold" \
  "$(render --set workers.asr.enabled=true --set metrics.prometheusRule.enabled=true)" "pool=\"asr\"}) > 50"
expect_absent "no asr alerts without asr workers" "$rules" 'pool="asr"'

expect_contains "notes describe the asr workers" "$(render_notes --set workers.asr.enabled=true)" "Transcription is on (ASR_ENABLED=true in every pod)"
expect_contains "notes warn about the default maxmemory with asr" \
  "$(render_notes --set workers.asr.enabled=true)" "DOCLING_INPUT_BUDGET_BYTES (200000000)"
expect_contains "notes say transcription is off" "$(render_notes)" "No ASR workers are deployed"

# --- NLP workers (PII) --------------------------------------------------------------------------

expect_absent "nlp off renders nothing" "$on" "nlp"
expect_absent "nlp off renders no models PVC" "$on" "$NLP_MODELS"
for name in "$API" "$EXTRACT" "$BROWSER"; do
  expect_values "nlp off ($name)" "$(document "$on" Deployment "$name")" PII_ENABLED "false"
done
expect_values "config.PII_ENABLED ignored (API)" \
  "$(document "$(render --set config.PII_ENABLED=true)" Deployment "$API")" PII_ENABLED "false"
expect_absent "no PII_HASH_KEY without nlp workers" "$on" "PII_HASH_KEY"

nlp_on="$(render --set workers.nlp.enabled=true --set workers.docling.enabled=true --set workers.asr.enabled=true)"
nlp="$(document "$nlp_on" Deployment "$NLP")"
if [[ -n "$nlp" ]]; then pass "nlp worker Deployment is rendered when enabled"; else fail "no nlp worker Deployment"; fi
# Every container of every pod (the init containers included).
for name in "$API" "$EXTRACT" "$BROWSER" "$DOCLING" "$ASR" "$NLP"; do
  expect_every_value "nlp on ($name)" "$(document "$nlp_on" Deployment "$name")" PII_ENABLED "true"
done
# The init container and the worker.
expect_values "nlp on (nlp worker and init container)" "$nlp" PII_ENABLED "true true"
expect_values "nlp worker and init container" "$nlp" PII_MODELS_PATH "/models /models"
expect_values "nlp worker and init container" "$nlp" PII_MODEL_SIZE "lg lg"
expect_values "nlp worker" "$nlp" PII_CONCURRENCY "1 1"
expect_absent "nlp worker gets no WORKER_CONCURRENCY" "$nlp" "name: WORKER_CONCURRENCY"
expect_absent "nlp worker gets no DOCUMENT_MEMORY_LIMIT_MB" "$nlp" "name: DOCUMENT_MEMORY_LIMIT_MB"
expect_absent "the API pods get no model settings" "$(document "$nlp_on" Deployment "$API")" "PII_MODEL"
expect_contains "nlp worker uses the nlp image" "$nlp" "image: \"ghcr.io/janschachtschabel/text-extraction-nlp:$APP_VERSION\""
expect_contains "nlp worker runs its entry point" "$nlp" "- text-extraction-nlp-worker"
expect_absent "nlp worker keeps the image's init process" "$nlp" "command:"
expect_contains "nlp worker serves the internal port" "$nlp" "containerPort: 9090"
expect_absent "nlp worker has no public port" "$nlp" "containerPort: 8080"
expect_contains "nlp worker grace period" "$nlp" "terminationGracePeriodSeconds: 60"
expect_contains "nlp worker CPU request" "$(flat "$nlp")" "requests: cpu: \"1\" memory: 2Gi"
expect_contains "nlp worker memory limit" "$(flat "$nlp")" "limits: cpu: \"2\" memory: 3Gi"
expect_contains "nlp worker internal Service" "$(document "$nlp_on" Service "$NLP-internal")" "port: 9090"
# The init container downloads into a writable mount, the worker reads it read-only.
init="$(awk '/^      initContainers:/ { f = 1 } /^      containers:/ { f = 0 } f' <<<"$nlp")"
worker="$(awk '/^      containers:/ { f = 1 } /^      volumes:/ { f = 0 } f' <<<"$nlp")"
expect_contains "nlp init container downloads the models" "$init" "- --download-models"
expect_contains "nlp init container runs the nlp entry point" "$init" "- text-extraction-nlp-worker"
expect_contains "nlp init container mounts the models" "$init" "mountPath: /models"
expect_absent "nlp init container mount is writable" "$init" "readOnly: true"
expect_contains "nlp init container root filesystem is read-only" "$init" "readOnlyRootFilesystem: true"
expect_contains "nlp init container requests little memory" "$(flat "$init")" "requests: cpu: 100m memory: 256Mi"
expect_absent "nlp init container does not take the worker's resources" "$init" "memory: 3Gi"
expect_contains "nlp worker mounts the models" "$worker" "mountPath: /models"
expect_contains "nlp worker models mount is read-only" "$(flat "$worker")" "mountPath: /models readOnly: true"
expect_absent "nlp worker does not download" "$worker" "--download-models"
nlp_pvc="$(document "$nlp_on" PersistentVolumeClaim "$NLP_MODELS")"
expect_contains "nlp models PVC is rendered" "$nlp_pvc" "storage: 3Gi"
expect_contains "nlp models PVC is ReadWriteMany" "$nlp_pvc" "- ReadWriteMany"
expect_absent "nlp models PVC uses the default storage class" "$nlp_pvc" "storageClassName"
expect_contains "nlp worker uses the models PVC" "$nlp" "claimName: $NLP_MODELS"
expect_contains "nlp storageClass is passed on" \
  "$(document "$(render --set workers.nlp.enabled=true --set workers.nlp.models.persistence.storageClass=nfs)" PersistentVolumeClaim "$NLP_MODELS")" \
  'storageClassName: "nfs"'
expect_contains "docling keeps its own models PVC" "$(document "$nlp_on" Deployment "$DOCLING")" "claimName: $MODELS"
expect_contains "asr keeps its own models PVC" "$(document "$nlp_on" Deployment "$ASR")" "claimName: $ASR_MODELS"
expect_contains "API grace period does not wait for nlp" "$(document "$nlp_on" Deployment "$API")" "terminationGracePeriodSeconds: 90"
expect_absent "nlp worker gets no hash key without a secret" "$nlp" "PII_HASH_KEY"

# modelSize reaches the init container and the worker, nothing else.
nlp_md="$(render --set workers.nlp.enabled=true --set workers.nlp.modelSize=md)"
expect_values "modelSize md sets PII_MODEL_SIZE" "$(document "$nlp_md" Deployment "$NLP")" PII_MODEL_SIZE "md md"
expect_render_fails "modelSize is lg or md" "workers.nlp.modelSize must be lg or md" \
  --set workers.nlp.enabled=true --set workers.nlp.modelSize=xl
expect_values "nlp concurrency follows workers.nlp.concurrency" \
  "$(document "$(render --set workers.nlp.enabled=true --set workers.nlp.concurrency=3)" Deployment "$NLP")" PII_CONCURRENCY "3 3"

# PII_HASH_KEY only from a Secret, on the worker container only.
nlp_key="$(render --set workers.nlp.enabled=true --set workers.nlp.hashKey.existingSecret=pii-key)"
nlp_keyed="$(document "$nlp_key" Deployment "$NLP")"
expect_contains "hashKey secret renders PII_HASH_KEY from it" \
  "$(grep -A4 -- "- name: PII_HASH_KEY" <<<"$nlp_keyed")" 'name: "pii-key"'
expect_contains "hashKey secret key defaults to PII_HASH_KEY" \
  "$(grep -A4 -- "- name: PII_HASH_KEY" <<<"$nlp_keyed")" 'key: "PII_HASH_KEY"'
expect_contains "hashKey secret key can be set" \
  "$(grep -A4 -- "- name: PII_HASH_KEY" <<<"$(document "$(render --set workers.nlp.enabled=true --set workers.nlp.hashKey.existingSecret=pii-key --set workers.nlp.hashKey.existingSecretKey=hmac)" Deployment "$NLP")")" 'key: "hmac"'
expect_absent "PII_HASH_KEY stays out of the init container" \
  "$(awk '/^      initContainers:/ { f = 1 } /^      containers:/ { f = 0 } f' <<<"$nlp_keyed")" "PII_HASH_KEY"
for name in "$API" "$EXTRACT" "$BROWSER"; do
  expect_absent "PII_HASH_KEY stays out of $name" "$(document "$nlp_key" Deployment "$name")" "PII_HASH_KEY"
done
expect_absent "PII_HASH_KEY never in the ConfigMap" "$(document "$nlp_key" ConfigMap "$API")" "PII_HASH_KEY"
expect_absent "hashKey secret without nlp workers renders nothing" \
  "$(render --set workers.nlp.hashKey.existingSecret=pii-key)" "PII_HASH_KEY"

nlp_claimed="$(render --set workers.nlp.enabled=true --set workers.nlp.models.persistence.existingClaim=spacy)"
expect_absent "existingClaim skips the PVC" "$nlp_claimed" "kind: PersistentVolumeClaim"
expect_contains "nlp existingClaim is mounted" "$(document "$nlp_claimed" Deployment "$NLP")" "claimName: spacy"
expect_contains "nlp existingClaim still downloads by default" "$(document "$nlp_claimed" Deployment "$NLP")" "- --download-models"
expect_absent "nlp download false skips the init container" \
  "$(document "$(render --set workers.nlp.enabled=true --set workers.nlp.models.persistence.existingClaim=spacy --set workers.nlp.models.download=false)" Deployment "$NLP")" \
  "initContainers:"
expect_render_fails "nlp download false needs a pre-filled existingClaim" "workers.nlp.models.download=false needs" \
  --set workers.nlp.enabled=true --set workers.nlp.models.download=false

# downloadMode job: a hook Job fills the volume, the pods only check it.
nlp_job_mode="$(render --set workers.nlp.enabled=true --set workers.nlp.models.downloadMode=job)"
nlp_job="$(document "$nlp_job_mode" Job "$NLP-models")"
if [[ -n "$nlp_job" ]]; then pass "downloadMode job renders a hook Job"; else fail "downloadMode job: no nlp models Job"; fi
expect_contains "nlp models Job is a post-install and post-upgrade hook" "$nlp_job" '"helm.sh/hook": post-install,post-upgrade'
expect_contains "nlp models Job downloads the models" "$nlp_job" "- --download-models"
expect_contains "nlp models Job uses the nlp image" "$nlp_job" "image: \"ghcr.io/janschachtschabel/text-extraction-nlp:$APP_VERSION\""
expect_contains "nlp models Job mounts the models" "$nlp_job" "mountPath: /models"
expect_absent "nlp models Job mount is writable" "$nlp_job" "readOnly: true"
expect_contains "nlp models Job root filesystem is read-only" "$nlp_job" "readOnlyRootFilesystem: true"
expect_contains "nlp models Job runs as the image's user" "$nlp_job" "runAsUser: 10001"
expect_absent "nlp models Job keeps the image's init process" "$nlp_job" "command:"
expect_values "nlp models Job downloads the configured size" "$nlp_job" PII_MODEL_SIZE "lg"
expect_values "nlp models Job reads PII_MODELS_PATH" "$nlp_job" PII_MODELS_PATH "/models"
expect_contains "nlp models Job requests little memory" "$(flat "$nlp_job")" "requests: cpu: 100m memory: 256Mi"
expect_absent "nlp models Job needs no hash key" "$(document "$(render --set workers.nlp.enabled=true --set workers.nlp.models.downloadMode=job --set workers.nlp.hashKey.existingSecret=pii-key)" Job "$NLP-models")" "PII_HASH_KEY"
nlp_job_worker="$(document "$nlp_job_mode" Deployment "$NLP")"
expect_absent "downloadMode job renders no init container (nlp)" "$nlp_job_worker" "initContainers:"
expect_absent "downloadMode job: the nlp worker does not download" "$nlp_job_worker" "--download-models"
expect_contains "downloadMode job: the nlp worker mounts the models read-only" "$nlp_job_worker" "readOnly: true"
expect_absent "downloadMode init (the default) renders no nlp Job" "$nlp_on" "$NLP-models"
expect_absent "nlp download false renders no Job" \
  "$(render --set workers.nlp.enabled=true --set workers.nlp.models.downloadMode=job --set workers.nlp.models.download=false --set workers.nlp.models.persistence.existingClaim=m)" \
  "kind: Job"
expect_render_fails "nlp downloadMode is init or job" "workers.nlp.models.downloadMode must be init or job" \
  --set workers.nlp.enabled=true --set workers.nlp.models.downloadMode=sidecar

expect_render_fails "nlp needs jobs" "workers.nlp.enabled needs JOBS_ENABLED=true" \
  --set workers.nlp.enabled=true --set jobs.enabled=false
# nlp worker: 30 s for the items in flight + 10
expect_render_fails "nlp worker grace period too short" "workers.nlp.terminationGracePeriodSeconds" \
  --set workers.nlp.enabled=true --set workers.nlp.terminationGracePeriodSeconds=39
expect_render_succeeds "nlp worker grace period just long enough" \
  --set workers.nlp.enabled=true --set workers.nlp.terminationGracePeriodSeconds=40

nlp_keda="$(document "$(render --set workers.nlp.enabled=true --set workers.nlp.autoscaling.keda.enabled=true)" ScaledObject "$NLP")"
expect_contains "nlp KEDA query names pool nlp" "$nlp_keda" \
  "query: \"max(text_extraction_queue_pending_items{pool=\\\"nlp\\\", namespace=\\\"$NAMESPACE\\\"})\""
expect_contains "nlp KEDA threshold" "$nlp_keda" 'threshold: "10"'
expect_contains "nlp KEDA keeps a replica" "$nlp_keda" "minReplicaCount: 1"
nlp_hpa="$(document "$(render --set workers.nlp.enabled=true --set workers.nlp.autoscaling.hpa.enabled=true)" HorizontalPodAutoscaler "$NLP")"
expect_contains "nlp HPA scales the nlp workers" "$nlp_hpa" "name: $NLP"
expect_absent "nlp replicas are left to the scaler" \
  "$(document "$(render --set workers.nlp.enabled=true --set workers.nlp.autoscaling.hpa.enabled=true)" Deployment "$NLP")" "replicas:"
expect_render_fails "nlp KEDA and HPA are exclusive" "mutually exclusive" \
  --set workers.nlp.enabled=true --set workers.nlp.autoscaling.keda.enabled=true \
  --set workers.nlp.autoscaling.hpa.enabled=true
expect_render_fails "nlp autoscaling needs the workers" "workers.nlp.autoscaling needs workers.nlp.enabled=true" \
  --set workers.nlp.autoscaling.hpa.enabled=true

nlp_policies="$(render --set workers.nlp.enabled=true --set networkPolicy.enabled=true)"
expect_contains "nlp worker NetworkPolicy" "$(document "$nlp_policies" NetworkPolicy "$NLP")" "port: 9090"
expect_contains "Valkey accepts the nlp workers" \
  "$(document "$nlp_policies" NetworkPolicy "$RELEASE-text-extraction-valkey")" "app.kubernetes.io/name: text-extraction-worker-nlp"

nlp_rules="$(render --set workers.nlp.enabled=true --set metrics.prometheusRule.enabled=true --set alerts.nlpQueueBacklogItems=9)"
expect_contains "nlp backlog alert" "$nlp_rules" "text_extraction_queue_pending_items{namespace=\"$NAMESPACE\", pool=\"nlp\"}) > 9"
expect_contains "nlp workers-down alert" "$nlp_rules" "deployment=\"$NLP\""
expect_contains "nlp items are counted on the nlp workers" "$nlp_rules" "service=\"$NLP-internal\""
expect_contains "nlp backlog default threshold" \
  "$(render --set workers.nlp.enabled=true --set metrics.prometheusRule.enabled=true)" "pool=\"nlp\"}) > 200"
expect_absent "no nlp alerts without nlp workers" "$rules" 'pool="nlp"'

expect_contains "notes describe the nlp workers" "$(render_notes --set workers.nlp.enabled=true)" "PII handling is on (PII_ENABLED=true in every pod)"
expect_contains "notes name the models size" "$(render_notes --set workers.nlp.enabled=true --set workers.nlp.modelSize=md)" "spaCy models md"
expect_contains "notes warn about the random hash key" "$(render_notes --set workers.nlp.enabled=true)" "hash_scope: result"
expect_absent "no hash key warning with a key" "$(render_notes --set workers.nlp.enabled=true --set workers.nlp.hashKey.existingSecret=pii-key)" "hash_scope: result"
expect_contains "notes say PII handling is off" "$(render_notes)" "No NLP workers are deployed"

# --- LLM workers (structured output, semantic chunking) ------------------------------------------

expect_absent "llm off renders nothing" "$on" "worker-llm"
expect_absent "llm off renders no LLM_BASE_URL" "$on" "LLM_BASE_URL"
expect_absent "llm off renders no LLM_API_KEY" "$on" "LLM_API_KEY"
for name in "$API" "$EXTRACT" "$BROWSER"; do
  expect_values "llm off ($name)" "$(document "$on" Deployment "$name")" LLM_ENABLED "false"
done
expect_values "config.LLM_ENABLED ignored (API)" \
  "$(document "$(render --set config.LLM_ENABLED=true)" Deployment "$API")" LLM_ENABLED "false"
expect_absent "llm apiKey secret without llm workers renders nothing" \
  "$(render --set workers.llm.apiKey.existingSecret=llm-key)" "LLM_API_KEY"

llm_on="$(render --set workers.llm.enabled=true --set workers.docling.enabled=true --set workers.asr.enabled=true --set workers.nlp.enabled=true)"
llm="$(document "$llm_on" Deployment "$LLM")"
if [[ -n "$llm" ]]; then pass "llm worker Deployment is rendered when enabled"; else fail "no llm worker Deployment"; fi
for name in "$API" "$EXTRACT" "$BROWSER" "$DOCLING" "$ASR" "$NLP" "$LLM"; do
  expect_every_value "llm on ($name)" "$(document "$llm_on" Deployment "$name")" LLM_ENABLED "true"
  expect_every_value "llm on ($name)" "$(document "$llm_on" Deployment "$name")" LLM_BASE_URL "https://api.openai.com/v1"
  expect_every_value "llm on ($name)" "$(document "$llm_on" Deployment "$name")" LLM_MODEL "gpt-6-luna"
  expect_absent "llm on without embeddingModel ($name)" "$(document "$llm_on" Deployment "$name")" "LLM_EMBEDDING_MODEL"
done
expect_contains "llm worker uses the API image" "$llm" "image: \"ghcr.io/janschachtschabel/text-extraction:$APP_VERSION\""
expect_contains "llm worker runs its entry point" "$llm" "- text-extraction-llm-worker"
expect_absent "llm worker keeps the image's init process" "$llm" "command:"
expect_values "llm worker concurrency" "$llm" LLM_CONCURRENCY "4"
expect_absent "llm worker gets no WORKER_CONCURRENCY" "$llm" "name: WORKER_CONCURRENCY"
expect_absent "llm worker gets no DOCUMENT_MEMORY_LIMIT_MB" "$llm" "name: DOCUMENT_MEMORY_LIMIT_MB"
expect_absent "llm worker needs no models volume" "$llm" "claimName"
expect_absent "llm worker needs no init container" "$llm" "initContainers:"
expect_absent "llm worker needs no models mount" "$llm" "mountPath: /models"
expect_absent "llm worker is not an nlp worker" "$llm" "PII_MODEL"
expect_contains "llm worker serves the internal port" "$llm" "containerPort: 9090"
expect_absent "llm worker has no public port" "$llm" "containerPort: 8080"
expect_contains "llm worker grace period" "$llm" "terminationGracePeriodSeconds: 40"
expect_contains "llm worker CPU request" "$(flat "$llm")" "requests: cpu: 100m memory: 256Mi"
expect_contains "llm worker memory limit" "$(flat "$llm")" "limits: cpu: \"1\" memory: 1Gi"
expect_contains "llm worker internal Service" "$(document "$llm_on" Service "$LLM-internal")" "port: 9090"
expect_contains "llm worker replicas" "$llm" "replicas: 1"
expect_absent "llm workers create no volume claim" "$(render --set workers.llm.enabled=true)" "kind: PersistentVolumeClaim"
expect_contains "API grace period does not wait for llm" "$(document "$llm_on" Deployment "$API")" "terminationGracePeriodSeconds: 90"
expect_values "llm concurrency follows workers.llm.concurrency" \
  "$(document "$(render --set workers.llm.enabled=true --set workers.llm.concurrency=7)" Deployment "$LLM")" LLM_CONCURRENCY "7"

# baseUrl, model and embeddingModel reach every pod.
llm_set="$(render --set workers.llm.enabled=true --set workers.llm.baseUrl=https://llm.example.org/v1 \
  --set workers.llm.model=my-model --set workers.llm.embeddingModel=my-embeddings)"
for name in "$API" "$EXTRACT" "$BROWSER" "$LLM"; do
  expect_every_value "llm settings ($name)" "$(document "$llm_set" Deployment "$name")" LLM_BASE_URL "https://llm.example.org/v1"
  expect_every_value "llm settings ($name)" "$(document "$llm_set" Deployment "$name")" LLM_MODEL "my-model"
  expect_every_value "llm settings ($name)" "$(document "$llm_set" Deployment "$name")" LLM_EMBEDDING_MODEL "my-embeddings"
done
expect_render_fails "llm baseUrl must be https" "workers.llm.baseUrl must start with https://" \
  --set workers.llm.enabled=true --set workers.llm.baseUrl=http://llm.local/v1
expect_render_succeeds "llm baseUrl may be http with LLM_ALLOW_HTTP" \
  --set workers.llm.enabled=true --set workers.llm.baseUrl=http://llm.local/v1 --set-string config.LLM_ALLOW_HTTP=true
expect_render_fails "llm baseUrl is required" "workers.llm.baseUrl is required" \
  --set workers.llm.enabled=true --set workers.llm.baseUrl=
expect_render_fails "llm model is required" "workers.llm.model is required" \
  --set workers.llm.enabled=true --set workers.llm.model=

# LLM_API_KEY only from a Secret, on the API pods and the llm worker container only.
llm_key="$(render --set workers.llm.enabled=true --set workers.llm.apiKey.existingSecret=llm-key)"
llm_keyed="$(document "$llm_key" Deployment "$LLM")"
expect_contains "apiKey secret renders LLM_API_KEY from it" \
  "$(grep -A4 -- "- name: LLM_API_KEY" <<<"$llm_keyed")" 'name: "llm-key"'
expect_contains "apiKey secret key defaults to LLM_API_KEY" \
  "$(grep -A4 -- "- name: LLM_API_KEY" <<<"$llm_keyed")" 'key: "LLM_API_KEY"'
expect_contains "apiKey secret key is configurable" \
  "$(grep -A4 -- "- name: LLM_API_KEY" <<<"$(document "$(render --set workers.llm.enabled=true --set workers.llm.apiKey.existingSecret=llm-key --set workers.llm.apiKey.existingSecretKey=token)" Deployment "$LLM")")" 'key: "token"'
expect_contains "apiKey secret reaches the API pods (they only need to know there is a key)" \
  "$(grep -A4 -- "- name: LLM_API_KEY" <<<"$(document "$llm_key" Deployment "$API")")" 'name: "llm-key"'
expect_absent "LLM_API_KEY is never a literal value" "$(grep -A1 -- "- name: LLM_API_KEY" <<<"$llm_key")" "value:"
for name in "$EXTRACT" "$BROWSER"; do
  expect_absent "LLM_API_KEY stays out of $name" "$(document "$llm_key" Deployment "$name")" "LLM_API_KEY"
done
expect_absent "LLM_API_KEY never in the ConfigMap" "$(document "$llm_key" ConfigMap "$API")" "LLM_API_KEY"
expect_absent "llm worker gets no key without a secret" "$llm" "LLM_API_KEY"

llm_keda="$(document "$(render --set workers.llm.enabled=true --set workers.llm.autoscaling.keda.enabled=true)" ScaledObject "$LLM")"
expect_contains "llm KEDA query names pool llm" "$llm_keda" \
  "query: \"max(text_extraction_queue_pending_items{pool=\\\"llm\\\", namespace=\\\"$NAMESPACE\\\"})\""
expect_contains "llm KEDA threshold follows the concurrency" "$llm_keda" 'threshold: "4"'
expect_contains "llm KEDA keeps a replica" "$llm_keda" "minReplicaCount: 1"
llm_hpa_on="$(render --set workers.llm.enabled=true --set workers.llm.autoscaling.hpa.enabled=true)"
expect_contains "llm HPA scales the llm workers" "$(document "$llm_hpa_on" HorizontalPodAutoscaler "$LLM")" "name: $LLM"
expect_absent "llm replicas are left to the scaler" "$(document "$llm_hpa_on" Deployment "$LLM")" "replicas:"
expect_render_fails "llm KEDA and HPA are exclusive" "mutually exclusive" \
  --set workers.llm.enabled=true --set workers.llm.autoscaling.keda.enabled=true \
  --set workers.llm.autoscaling.hpa.enabled=true
expect_render_fails "llm autoscaling needs the workers" "workers.llm.autoscaling needs workers.llm.enabled=true" \
  --set workers.llm.autoscaling.hpa.enabled=true

expect_render_fails "llm needs jobs" "workers.llm.enabled needs JOBS_ENABLED=true" \
  --set workers.llm.enabled=true --set jobs.enabled=false
# llm worker: workers.llm.gracefulSeconds (30) for the items in flight (then they are given back) + 10
expect_render_fails "llm worker grace period too short" "workers.llm.terminationGracePeriodSeconds" \
  --set workers.llm.enabled=true --set workers.llm.terminationGracePeriodSeconds=39
expect_render_succeeds "llm worker grace period just long enough" \
  --set workers.llm.enabled=true --set workers.llm.terminationGracePeriodSeconds=40
# Issue #19: the grace of the items in flight is a knob, LLM_GRACEFUL_SECONDS on the llm workers.
expect_values "llm worker graceful seconds" "$llm" LLM_GRACEFUL_SECONDS "30"
llm_graceful="$(render --set workers.llm.enabled=true --set workers.llm.gracefulSeconds=130 \
  --set workers.llm.terminationGracePeriodSeconds=140 --set config.LLM_GRACEFUL_SECONDS=5)"
expect_values "llm graceful seconds follow workers.llm.gracefulSeconds" \
  "$(document "$llm_graceful" Deployment "$LLM")" LLM_GRACEFUL_SECONDS "130"
expect_absent "only the llm workers get LLM_GRACEFUL_SECONDS" \
  "$(document "$llm_graceful" Deployment "$API")" "name: LLM_GRACEFUL_SECONDS"
expect_render_fails "llm grace period must cover gracefulSeconds" "workers.llm.terminationGracePeriodSeconds" \
  --set workers.llm.enabled=true --set workers.llm.gracefulSeconds=130 \
  --set workers.llm.terminationGracePeriodSeconds=139

llm_policies="$(render --set workers.llm.enabled=true --set networkPolicy.enabled=true)"
expect_contains "llm worker NetworkPolicy" "$(document "$llm_policies" NetworkPolicy "$LLM")" "port: 9090"
expect_contains "Valkey accepts the llm workers" \
  "$(document "$llm_policies" NetworkPolicy "$RELEASE-text-extraction-valkey")" "app.kubernetes.io/name: text-extraction-worker-llm"

llm_rules="$(render --set workers.llm.enabled=true --set metrics.prometheusRule.enabled=true --set alerts.llmQueueBacklogItems=9)"
expect_contains "backlog alert for queue llm" "$llm_rules" "text_extraction_queue_pending_items{namespace=\"$NAMESPACE\", pool=\"llm\"}) > 9"
expect_contains "llm workers-down alert" "$llm_rules" "deployment=\"$LLM\""
expect_contains "llm items are counted on the llm workers" "$llm_rules" "service=\"$LLM-internal\""
expect_contains "extract items leave out the llm workers" "$llm_rules" "service!~\"$BROWSER-internal|$DOCLING-internal|$ASR-internal|$NLP-internal|$LLM-internal\""
expect_contains "Valkey memory alert reads the llm workers" "$llm_rules" "|$NLP-internal|$LLM-internal\"})"
expect_contains "llm backlog default threshold" \
  "$(render --set workers.llm.enabled=true --set metrics.prometheusRule.enabled=true)" "pool=\"llm\"}) > 100"
expect_absent "no llm alerts without llm workers" "$rules" 'pool="llm"'

expect_contains "notes describe the llm workers" "$(render_notes --set workers.llm.enabled=true)" "LLM is on (LLM_ENABLED=true in every pod)"
expect_contains "notes name the endpoint" "$(render_notes --set workers.llm.enabled=true --set workers.llm.baseUrl=https://llm.example.org/v1)" "https://llm.example.org/v1"
expect_contains "notes warn about a missing key" "$(render_notes --set workers.llm.enabled=true)" "No LLM_API_KEY is set"
expect_absent "no key warning with a secret" "$(render_notes --set workers.llm.enabled=true --set workers.llm.apiKey.existingSecret=llm-key)" "No LLM_API_KEY is set"
expect_contains "notes say the LLM is off" "$(render_notes)" "No llm workers are deployed"

if ((failures > 0)); then
  echo "$failures check(s) failed" >&2
  exit 1
fi
echo "all chart checks passed"

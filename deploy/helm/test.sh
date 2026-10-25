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

expect_contains() {
  local label="$1" output="$2" needle="$3"
  if grep -qF -- "$needle" <<<"$output"; then pass "$label"; else fail "$label: '$needle' missing"; fi
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
expect_contains "browser worker memory limit" "$browser" "memory: 4Gi"
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
expect_contains "extract KEDA keeps pool extract" \
  "$(document "$(render --set workers.extract.autoscaling.keda.enabled=true)" ScaledObject "$EXTRACT")" 'pool=\"extract\"'
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
# of address space: (2048 - 512) / 2 in the API and extract pods, (4096 - 2560) / 2 in the
# browser pods.
expect_contains "API memory limit" "$api" "memory: 2Gi"
expect_contains "extract worker memory limit" "$extract" "memory: 2Gi"
for name in "$API" "$EXTRACT" "$BROWSER"; do
  expect_values "document memory ($name)" "$(document "$on" Deployment "$name")" DOCUMENT_MEMORY_LIMIT_MB "768"
done
expect_values "config.DOCUMENT_MEMORY_LIMIT_MB wins (API)" \
  "$(document "$(render --set-string config.DOCUMENT_MEMORY_LIMIT_MB=1000)" Deployment "$API")" DOCUMENT_MEMORY_LIMIT_MB ""
expect_values "config.DOCUMENT_MEMORY_LIMIT_MB wins (browser worker)" \
  "$(document "$(render --set-string config.DOCUMENT_MEMORY_LIMIT_MB=1000)" Deployment "$BROWSER")" DOCUMENT_MEMORY_LIMIT_MB ""
expect_values "document memory follows the limit" \
  "$(document "$(render --set resources.limits.memory=3Gi)" Deployment "$API")" DOCUMENT_MEMORY_LIMIT_MB "1280"
expect_values "document memory follows DOCUMENT_CONCURRENCY" \
  "$(document "$(render --set resources.limits.memory=3Gi --set-string config.DOCUMENT_CONCURRENCY=4)" Deployment "$API")" DOCUMENT_MEMORY_LIMIT_MB "640"
expect_values "document memory from Mi" \
  "$(document "$(render --set workers.extract.resources.limits.memory=2560Mi)" Deployment "$EXTRACT")" DOCUMENT_MEMORY_LIMIT_MB "1024"
# 2G = 1907.3 MiB: floor((1907.3 - 512) / 2)
expect_values "document memory from G" \
  "$(document "$(render --set resources.limits.memory=2G)" Deployment "$API")" DOCUMENT_MEMORY_LIMIT_MB "697"
expect_values "document memory from bytes" \
  "$(document "$(render --set-string resources.limits.memory=2147483648)" Deployment "$API")" DOCUMENT_MEMORY_LIMIT_MB "768"
expect_values "document memory follows the reserve" \
  "$(document "$(render --set workers.browser.documentParentReserveMiB=2048)" Deployment "$BROWSER")" DOCUMENT_MEMORY_LIMIT_MB "1024"
expect_values "no memory limit, no document memory" \
  "$(document "$(render --set resources.limits.memory=null)" Deployment "$API")" DOCUMENT_MEMORY_LIMIT_MB ""
# (1024 - 512) / 2 = 256 is the minimum; 1000Mi leaves 244.
expect_render_succeeds "document memory at the minimum" --set resources.limits.memory=1Gi
expect_render_fails "document memory below the minimum (API)" "resources.limits.memory (1000Mi) leaves 244 MiB" \
  --set resources.limits.memory=1000Mi
expect_render_fails "document memory below the minimum (extract worker)" "workers.extract.resources.limits.memory (1000Mi) leaves 244 MiB" \
  --set workers.extract.resources.limits.memory=1000Mi
expect_render_succeeds "config.DOCUMENT_MEMORY_LIMIT_MB skips the minimum" \
  --set resources.limits.memory=1000Mi --set-string config.DOCUMENT_MEMORY_LIMIT_MB=400

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
expect_contains "extract items leave out the browser and docling workers" "$docling_rules" "service!~\"$BROWSER-internal|$DOCLING-internal\""
expect_contains "Valkey memory alert reads the docling workers" "$docling_rules" "|$DOCLING-internal\"})"
expect_absent "no docling alerts without docling workers" "$rules" 'pool="docling"'

if ((failures > 0)); then
  echo "$failures check(s) failed" >&2
  exit 1
fi
echo "all chart checks passed"

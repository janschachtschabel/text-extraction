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

if ((failures > 0)); then
  echo "$failures check(s) failed" >&2
  exit 1
fi
echo "all chart checks passed"

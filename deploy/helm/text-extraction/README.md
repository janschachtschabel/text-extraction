# text-extraction Helm chart

Deploys the text-extraction API with an optional single-replica Valkey.

```bash
helm install text-extraction ./deploy/helm/text-extraction \
  --set-file secrets.apiKeys=api-keys.json
```

Without API keys the pods refuse to start (exit code 2) while `config.AUTH_MODE` is `api_key`.
Compute the SHA-256 of a key with `printf %s "$KEY" | sha256sum | cut -d' ' -f1`.

## What it creates

| Object | When | Notes |
| --- | --- | --- |
| Deployment, ConfigMap | always | Stateless pods. Non-secret settings come from the ConfigMap through `envFrom`. |
| Service `<release>-text-extraction` | always | Public port `http` 8080. |
| Service `<release>-text-extraction-internal` | always | Port `internal` 9090 (`/health`, `/ready`, `/metrics`). Cluster-internal only. |
| Secret `<release>-text-extraction-keys` | `secrets.apiKeys` set and no `secrets.existingSecret` | Holds `API_KEYS`. |
| Valkey Deployment, Service, optional PVC | `valkey.enabled` | `redis://<release>-text-extraction-valkey:6379/0`. |
| Ingress | `ingress.enabled` | Routes to the public Service port 8080 only. |
| HorizontalPodAutoscaler | `autoscaling.enabled` | CPU utilisation. Replaces `replicaCount`. |
| ServiceMonitor | `metrics.serviceMonitor.enabled` | Scrapes the internal Service, port `internal`, path `/metrics`. |
| PrometheusRule | `metrics.prometheusRule.enabled` | `TextExtractionDown`, `TextExtractionHigh5xx`. |

Pods run as uid/gid 10001 with a read-only root filesystem, all capabilities dropped and the
`RuntimeDefault` seccomp profile. An `emptyDir` is mounted at `/tmp`. The probes use the internal
port: `/health` for liveness and `/ready` for readiness. `/ready` answers 200 only once the public
port listens and turns 503 as soon as SIGTERM arrives, so no preStop hook is needed.

## Configuration

Every service setting is an environment variable. `config` entries are non-secret and become
variables of the same name. The chart overrides `config.PUBLIC_PORT` (8080), `config.INTERNAL_PORT`
(9090) and `config.VALKEY_URL`: values set there are ignored. Set the Valkey address with
`valkey.enabled` or `externalValkey.*`.

> **Warning:** never put secrets into `config:`. It becomes a ConfigMap, which is stored and shown
> in clear text. Use `secrets.*` and `externalValkey.existingSecret.*` for secrets.

Secrets, listed separately:

| Variable | Source |
| --- | --- |
| `API_KEYS` | `secrets.apiKeys` (chart-managed Secret) or `secrets.existingSecret` + `secrets.existingSecretKey`. Only SHA-256 hashes of keys are stored. |
| `VALKEY_URL` | Plain `externalValkey.url`, or for a URL with a password `externalValkey.existingSecret.name` + `.key`. In-chart Valkey needs no secret. |

For a Valkey with a password, create a Secret and point the chart at it:

```bash
kubectl create secret generic valkey-url --from-literal=VALKEY_URL='redis://:PASSWORD@valkey.example.org:6379/0'
helm install text-extraction ./deploy/helm/text-extraction \
  --set valkey.enabled=false \
  --set externalValkey.existingSecret.name=valkey-url
```

## Values

| Key | Default | Description |
| --- | --- | --- |
| `nameOverride`, `fullnameOverride` | `""` | Override the chart or release-scoped object names. |
| `image.repository` | `ghcr.io/janschachtschabel/text-extraction` | API image. |
| `image.tag` | `""` | Image tag. Defaults to the chart `appVersion`. Production uses SemVer tags. |
| `image.pullPolicy` | `IfNotPresent` | |
| `imagePullSecrets` | `[]` | |
| `replicaCount` | `2` | Ignored when `autoscaling.enabled`. |
| `terminationGracePeriodSeconds` | `45` | Time between SIGTERM and kill. Must exceed the server's drain plus graceful shutdown, `SHUTDOWN_DRAIN_SECONDS + max(30, EXTRACT_TIMEOUT_SECONDS + 5)` seconds (35 by default); raise it with either. |
| `config.LOG_LEVEL` | `INFO` | Log level. |
| `config.LOG_FORMAT` | `json` | `json` or `console`. |
| `config.AUTH_MODE` | `api_key` | `api_key` or `none`. |
| `config.RATE_LIMIT_ENABLED` | `"true"` | Inbound rate limit per API key. |
| `config.RATE_LIMIT_PER_MINUTE` | `"60"` | Default limit per key. |
| `config.CONVERT_CONCURRENCY` | `"2"` | Conversions at once per pod. The memory limit is sized for it. |
| `config.SHUTDOWN_DRAIN_SECONDS` | `"5"` | Seconds the pod reports not ready but keeps serving after SIGTERM, before the graceful shutdown. |
| `config.*` | | Any further non-secret setting is added here. |
| `secrets.apiKeys` | `""` | `API_KEYS` JSON. Creates a chart-managed Secret. |
| `secrets.existingSecret` | `""` | Existing Secret with the `API_KEYS` JSON. Takes precedence over `secrets.apiKeys`. |
| `secrets.existingSecretKey` | `API_KEYS` | Key inside that Secret. |
| `resources` | requests 100m / 256Mi, limits 1 / 1Gi | Placeholder until package 11 measures real consumption. The memory limit is about `CONVERT_CONCURRENCY` x 450 MB (one 5 MiB page) plus the service; raise it with `CONVERT_CONCURRENCY` or `CONVERT_MAX_BYTES`. |
| `autoscaling.enabled` | `false` | |
| `autoscaling.minReplicas` / `maxReplicas` | `2` / `10` | |
| `autoscaling.targetCPUUtilizationPercentage` | `70` | |
| `service.type` | `ClusterIP` | Type of the public Service. |
| `service.port` | `8080` | Public Service port (named `http`). |
| `ingress.enabled` | `false` | The Ingress targets the public Service port only. |
| `ingress.className` | `""` | |
| `ingress.annotations` | `{}` | |
| `ingress.hosts` | one example host, path `/` | List of `host` with `paths` (`path`, `pathType`). |
| `ingress.tls` | `[]` | |
| `valkey.enabled` | `true` | Deploy a single-replica Valkey. |
| `valkey.image.repository` / `tag` / `pullPolicy` | `valkey/valkey` / `8.1-alpine` / `IfNotPresent` | |
| `valkey.persistence.enabled` | `false` | `false`: `emptyDir`, data is lost on restart. `true`: PVC and append-only file. |
| `valkey.persistence.size` | `1Gi` | |
| `valkey.persistence.storageClass` | `""` | Cluster default when empty. |
| `valkey.persistence.existingClaim` | `""` | Use this PVC instead of creating one. |
| `valkey.resources` | requests 50m / 64Mi, limits 500m / 256Mi | |
| `externalValkey.url` | `""` | Valkey or Redis URL used when `valkey.enabled` is `false`. No password in here. |
| `externalValkey.existingSecret.name` | `""` | Secret holding a `VALKEY_URL` that carries a password. Takes precedence over `url`. |
| `externalValkey.existingSecret.key` | `VALKEY_URL` | Key inside that Secret. |
| `metrics.serviceMonitor.enabled` | `false` | Needs the Prometheus Operator CRDs. |
| `metrics.serviceMonitor.interval` / `scrapeTimeout` | `30s` / `10s` | |
| `metrics.serviceMonitor.labels` | `{}` | For example the `release` label your Prometheus selects on. |
| `metrics.prometheusRule.enabled` | `false` | Needs the Prometheus Operator CRDs and kube-state-metrics. |
| `metrics.prometheusRule.labels` | `{}` | |
| `podAnnotations`, `nodeSelector`, `tolerations`, `affinity` | empty | Applied to the API pods. Valkey gets only `nodeSelector` and `tolerations`, not `affinity`. |

With `valkey.enabled=false`, the render fails unless `externalValkey.url` or
`externalValkey.existingSecret.name` is set.

## Alerts

| Alert | Expression | For |
| --- | --- | --- |
| `TextExtractionDown` | `sum(kube_deployment_status_replicas_available{...}) or vector(0)` is below 1 | 5 m |
| `TextExtractionHigh5xx` | share of `http_requests_total{status="5xx"}` above 5 % | 10 m |

`TextExtractionDown` needs kube-state-metrics. It uses the Deployment's available replicas rather
than `absent(up{...})`: `up` only says that a scrape target answers, not that a pod is ready, and
the series disappears with the pod. The `or vector(0)` branch makes the alert fire when the
series is missing too, so it also fires if kube-state-metrics itself is down.

`TextExtractionHigh5xx` relies on the label values emitted by prometheus-fastapi-instrumentator,
which groups `status` as `2xx`, `4xx` and `5xx`. It selects the series by the ServiceMonitor target
labels `namespace` and `service` (the internal Service), so it works only with the ServiceMonitor
enabled. Only 5xx answers from the public API count; a target failure is a `200` with
`status: failed` and does not raise this alert.

## Local checks

```bash
docker run --rm -v "$PWD/deploy/helm:/apps" -w /apps alpine/helm:3.19.0 lint --strict text-extraction
docker run --rm -v "$PWD/deploy/helm:/apps" -w /apps alpine/helm:3.19.0 template rel text-extraction
```

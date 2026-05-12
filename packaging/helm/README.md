# Registry — Helm Chart

Self-hosted semantic search and temporal retrieval for AI capabilities.

> **Configuration source of truth.** The canonical inventory of every
> environment variable the app reads lives in [`.env.example`](../../.env.example)
> at the repo root. This chart is **one supported deployment wiring** (Kubernetes
> via Helm). Other supported deployment targets — AWS ECS / Fargate, AWS Lambda,
> EC2 + systemd, Google Cloud Run, Nomad, App Runner — consume the same env
> vars; only the wiring layer differs. When in doubt about what to set, grep
> `.env.example` for the variable name; defaults and per-var notes live there.

## Prerequisites

| Tool | Minimum version |
|------|----------------|
| Kubernetes | 1.27 |
| Helm | 3.12 |
| CloudNativePG operator | 1.22 (not bundled — see below) |

## Install from the OCI registry

```bash
# 1. Install the CloudNativePG operator (skip if already present):
helm repo add cnpg https://cloudnative-pg.github.io/charts
helm upgrade --install cnpg cnpg/cloudnative-pg \
  --namespace cnpg-system --create-namespace --wait

# 2. Create the target namespace:
kubectl create namespace registry

# 3. Install the chart:
helm upgrade --install registry \
  oci://ghcr.io/registry/helm/registry \
  --version 0.0.1 \
  --namespace registry \
  --set secrets.databaseUrl="postgresql://registry:CHANGEME@postgres-rw.registry/registry" \
  --set secrets.apiToken="$(openssl rand -hex 32)" \
  --set secrets.oidcDiscoveryUrl="https://accounts.example.com/.well-known/openid-configuration" \
  --wait
```

The chart defaults to two API replicas with a PgBouncer sidecar and one sync-worker replica.

## Verify

```bash
kubectl -n registry rollout status deployment/registry-api
kubectl port-forward -n registry svc/registry 8080:80
curl http://localhost:8080/healthz
# {"status":"ok"}
```

## Key values

| Key | Default | Description |
|-----|---------|-------------|
| `replicaCount` | `2` | API server replicas |
| `image.tag` | chart appVersion | Image tag to deploy |
| `secrets.databaseUrl` | `""` | **Required.** PostgreSQL connection string |
| `secrets.apiToken` | `""` | **Required.** Bearer token for the API |
| `secrets.oidcDiscoveryUrl` | `""` | OIDC discovery URL (optional if not using OIDC) |
| `pgbouncer.enabled` | `true` | Enable PgBouncer sidecar |
| `pgbouncer.poolMode` | `transaction` | PgBouncer pool mode |
| `syncWorker.replicaCount` | `1` | Sync worker replicas (keep at 1 to avoid duplicate runs) |
| `ingress.enabled` | `false` | Expose via Ingress |
| `grafana.dashboards.enabled` | `false` | Mount Grafana dashboard ConfigMap |

See `values.yaml` for the full surface.

## Secrets management

For production, avoid passing secrets on the CLI. Recommended alternatives:

- **External Secrets Operator**: create an `ExternalSecret` that writes a `Secret`
  named `registry` in the release namespace before install.
- **Vault Agent**: inject secrets as environment variables via annotations.

The chart skips rendering the `Secret` resource when all `secrets.*` values are
empty, so you can pre-create the secret and the chart will reference it.

## Postgres (CloudNativePG example)

```yaml
# cnpg-cluster.yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: postgres
  namespace: registry
spec:
  instances: 3
  storage:
    size: 20Gi
  bootstrap:
    initdb:
      database: catalog
      owner: catalog
```

```bash
kubectl apply -f cnpg-cluster.yaml
```

## Grafana dashboards

Copy `helm/grafana-dashboards/` to your Grafana provisioning setup, or enable
auto-provisioning if you use the Grafana operator:

```bash
helm upgrade registry oci://ghcr.io/registry/helm/registry \
  --reuse-values \
  --set grafana.dashboards.enabled=true \
  --set grafana.dashboards.namespace=monitoring
```

## Upgrading

```bash
helm upgrade registry \
  oci://ghcr.io/registry/helm/registry \
  --version <new-version> \
  --namespace registry \
  --reuse-values
```

Run database migrations after the rollout completes:

```bash
kubectl -n registry exec -it deploy/registry-api -- \
  alembic upgrade head
```

## Uninstall

```bash
helm uninstall registry --namespace registry
# The Secret is annotated helm.sh/resource-policy: keep — delete manually if desired:
kubectl delete secret registry -n registry
```

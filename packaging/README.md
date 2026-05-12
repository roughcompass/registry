# packaging/

Production packaging examples for registry. **One example
deployment ships here; substitute your own at any level.**

The product is deployment-target-agnostic — `Settings` reads env vars,
and every env var the app reads is documented in
[`../.env.example`](../.env.example). You can run registry on
Kubernetes, AWS ECS / Fargate, AWS Lambda, EC2 + systemd, Cloud Run,
Nomad, App Runner, or a plain `docker run` — the application doesn't
know or care.

## What's here

| Path | What it is |
|---|---|
| [`helm/`](helm/) | One example Kubernetes deployment chart (with PgBouncer, Grafana dashboards, optional Helm-managed Postgres-as-CRD pattern). Fork or substitute. |

## What's deliberately not here

- **CI/CD configuration.** A `.github/workflows/` directory exists at
  the repo root — that's the maintainer's CI for releasing this
  product. Consumers wire CI in their own fork using their own
  platform's syntax; the `Makefile` at the repo root defines the
  gates so any CI invocation can stay thin.
- **Local-dev tooling** (`docker-compose.yml`, `prometheus.yml`).
  Those live at the product root because they're for contributors
  working on the code, not for production deployment.
- **Per-target wiring examples** for AWS, GCP, Azure, etc. Choosing
  one over another is operator policy; we don't.

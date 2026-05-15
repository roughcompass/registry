# =============================================================================
# registry — canonical command surface
# =============================================================================
#
# This file is the SPEC for every gate the project enforces. Local dev,
# pre-commit hooks, and CI platforms all invoke these targets — they do
# not redefine the commands themselves. Wire your CI of choice (GitHub
# Actions, GitLab CI, Jenkins, Buildkite, CircleCI, Bitbucket Pipelines,
# Azure DevOps, an air-gapped on-prem runner, or a plain `bash` script)
# to invoke `make <target>`.
#
# Two example wirings ship with the project:
#   - .github/workflows/ — GitHub Actions
#   - .gitlab-ci.yml     — GitLab CI
# Both call the targets defined below. Neither is required; both can
# coexist; either can be deleted without affecting the gates.
#
# See `docs/ci.md` for the architecture rationale.
#
# Conventions:
#   - All commands run from the repo root (this directory).
#   - All Python invocations assume the dev extras are installed
#     (`make install-dev`).
#   - Secrets and configuration come from environment variables — set
#     them however your platform sets env vars; the targets don't care.
#   - Each target is a single command or short pipeline. No logic in
#     Make beyond invocation.

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Prefer the project's venv at the conventional path; otherwise fall back
# to python3 (universally available on modern macOS / Linux). Operators
# can still override: `PYTHON=python3.13 make dev-token`.
#
# Resolved as an absolute path so recipes that `cd` still find the binary.
PYTHON      ?= $(if $(wildcard .venv/bin/python),$(CURDIR)/.venv/bin/python,python3)
PIP         ?= $(PYTHON) -m pip
PYTEST      ?= $(PYTHON) -m pytest
RUFF        ?= $(PYTHON) -m ruff
MYPY        ?= $(PYTHON) -m mypy
ALEMBIC     ?= $(PYTHON) -m alembic

# Source roots that ruff/mypy/pytest care about.
SRC_ROOTS   := registry sync scripts
TEST_ROOT   := tests

# Default target — print help.
.DEFAULT_GOAL := help

.PHONY: help install-dev lint format format-check typecheck doc-refs test-hygiene \
        test-unit test-integration test-conformance test-perf test all \
        migrate openapi-export dev-token dev-jwt dev-seed seeds-validate clean \
        build-docker helm-package

# -----------------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------------

help: ## Print this help.
	@printf "registry — Make targets\n\n"
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf "\nThe gates a PR must pass: lint + format-check + typecheck + doc-refs + test-unit.\n"
	@printf "Run them in one shot with: make all\n"

# -----------------------------------------------------------------------------
# Setup
# -----------------------------------------------------------------------------

install-dev: ## Install the project + dev extras into the current Python env.
	$(PIP) install -e ".[dev]"

# -----------------------------------------------------------------------------
# Lint, format, type-check, doc-refs (PR gates — fast)
# -----------------------------------------------------------------------------

lint: ## Run ruff in lint mode.
	$(RUFF) check .

format: ## Apply ruff format to the whole tree (writes).
	$(RUFF) format .

format-check: ## Verify the whole tree is formatted (read-only).
	$(RUFF) format --check .

typecheck: ## Run mypy --strict on the source tree.
	$(MYPY) --strict $(SRC_ROOTS)

doc-refs: ## Verify no internal-doc references in shipped code (see CLAUDE.md).
	$(PYTHON) scripts/check_no_doc_refs.py

test-hygiene: ## Verify no phase-named test files or stale phase comments.
	$(PYTHON) scripts/check_no_phase_named_tests.py

auth-consolidation-gate: ## Fail if any auth-path discriminator / api_token symbol survives outside migrations.
	@# Pattern set is narrower than the original spec: it covers names
	@# that are unambiguously dead (auth_mode discriminator, RSAM/rsam
	@# legacy naming, the api_token validator + hasher + admin path).
	@# `actor_roles`/`api_token` table-name strings still appear in
	@# workspace.py + ratelimit.py SQL queries — those are tracked as
	@# their own follow-ups since the workspace permission model and
	@# the ratelimit api_token fallback are not part of this auth
	@# consolidation. The gate exists to prevent re-introduction of
	@# the deleted symbols, not to police every legacy name.
	@if grep -rn 'auth_mode\|AUTH_MODE\|\bRSAM\b\|\brsam\b\|validate_token\|hash_token\|upsert_rsam\|admin_tokens\.' \
	    registry/ --include='*.py' --exclude-dir=migrations 2>/dev/null; then \
	  echo "auth-consolidation-gate: FAIL — legacy auth names found in registry/"; \
	  exit 1; \
	else \
	  echo "auth-consolidation-gate: PASS"; \
	fi

# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

test-unit: ## Run unit tests (no DB; ~2s).
	$(PYTEST) $(TEST_ROOT)/unit -q

test-integration: ## Run integration tests (testcontainers Postgres; slow).
	$(PYTEST) $(TEST_ROOT)/integration -q --timeout=180

test-conformance: ## Run conformance suite (openapi drift, tenant isolation, MCP).
	$(PYTEST) $(TEST_ROOT)/conformance -v --timeout=60

test-perf: ## Run perf tests (SLO p95 verification; marked @slow).
	$(PYTEST) $(TEST_ROOT)/perf -q --timeout=300 -m perf

test: test-unit test-conformance ## Run the fast test gates (unit + conformance).

all: lint format-check typecheck doc-refs test-hygiene test ## Run every gate a PR must pass.

# -----------------------------------------------------------------------------
# Operational helpers
# -----------------------------------------------------------------------------

migrate: ## Apply Alembic migrations to the database in $DATABASE_URL.
	$(ALEMBIC) upgrade head

openapi-export: ## Regenerate the committed openapi.json from the live app.
	$(PYTHON) scripts/export_openapi.py

# Bootstrap a local-dev tenant + actor + mock-IDP/entitlement seed.
# Idempotent — re-running reuses the existing tenant + actor rows and
# re-registers the client + canned entitlements without minting new
# credentials. Writes DEV_TENANT_SLUG, DEV_TENANT_ID, DEV_ACTOR_ID,
# DEV_USER_ID, CLIENT_ID, and CLIENT_SECRET to .env.dev so the
# developer can fetch a JWT from the mock IDP and call the API.
# Requires $DATABASE_URL pointing at a migrated DB plus the mock OIDC
# + mock entitlement services running on their default compose ports
# (or pass --skip-mock-seed). Pass --env-file=PATH to write somewhere
# other than .env.dev. See docs/02-get-started/01-quickstart.md for
# the JWT-fetch step.
dev-token: ## Seed dev tenant + actor + mock-IDP/entitlement state. Writes .env.dev.
	$(PYTHON) scripts/bootstrap_dev_tenant.py

# Mint a fresh JWT from the local mock IDP using the client credentials
# in .env.dev. Stdout is the bare access_token so it composes:
#   export TOKEN=$(make dev-jwt)
#   curl -H "Authorization: Bearer $(make dev-jwt)" http://localhost:8000/v1/whoami
# Requires `make dev-token` to have been run (for .env.dev) and the mock
# OIDC server reachable on its compose port. Token TTL is 3600s — re-run
# to refresh.
dev-jwt: ## Mint a fresh JWT from the local mock IDP. Stdout-only (pipe-friendly).
	@if [ ! -f .env.dev ]; then \
	  echo "error: .env.dev not found; run \`make dev-token\` first" >&2; exit 1; \
	fi; \
	set -a; . ./.env.dev; set +a; \
	curl -fsS -X POST http://localhost:8090/default/token \
	  -d grant_type=client_credentials \
	  -d client_id=$$CLIENT_ID \
	  -d client_secret=$$CLIENT_SECRET \
	  -d scope=registry \
	| $(PYTHON) -c "import json,sys; t=json.load(sys.stdin).get('access_token'); sys.exit('error: no access_token in mock IDP response') if not t else print(t)"

# Follow-up to dev-token: seed the dev tenant from every numbered bundle
# directory under seeds/ (00-core, 01-capability, …). One command, full
# demo. Idempotent — re-running yields the same entity_ids.
dev-seed: ## Seed dev tenant from every bundle under seeds/. Idempotent.
	$(PYTHON) scripts/seed.py

# Validate every capability entity in seeds/ against the capability JSON
# Schema (seeds/_templates/capability-schema.json). Operates on the merged
# attribute state across bundles — runs without a database so it can gate CI.
seeds-validate: ## Validate seeds/ capabilities against the capability JSON Schema.
	$(PYTHON) scripts/validate_seeds.py

# -----------------------------------------------------------------------------
# Release-side targets — the build/package commands. Image push and
# signing are platform-specific (each CI platform has its own login/secret
# story); they stay in the workflow YAML, not here. These targets give
# operators a portable starting point.
# -----------------------------------------------------------------------------

# Override these on the command line: `make build-docker IMAGE_TAG=v1.7.0`.
IMAGE_NAME ?= registry
IMAGE_TAG  ?= dev
HELM_VERSION ?= 0.0.1

build-docker: ## Build the application Docker image. Overrides: IMAGE_NAME, IMAGE_TAG.
	docker build -t "$(IMAGE_NAME):$(IMAGE_TAG)" .

helm-package: ## Package the Helm chart into /tmp/helm-pkg/. Overrides: HELM_VERSION.
	mkdir -p /tmp/helm-pkg
	helm package packaging/helm/ \
		--version "$(HELM_VERSION)" \
		--app-version "$(HELM_VERSION)" \
		--destination /tmp/helm-pkg

clean: ## Remove build artefacts + caches.
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -prune -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -prune -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -prune -exec rm -rf {} +
	find . -type d -name "*.egg-info" -prune -exec rm -rf {} +

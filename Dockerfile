# syntax=docker/dockerfile:1
# ── build stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Build-time deps only — not copied to the final image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY registry ./registry
COPY sync ./sync
COPY scripts ./scripts
COPY alembic.ini ./

# NOT --editable: the editable install records absolute paths from the builder
# (/build/registry), which don't exist in the runtime stage at /app/registry.
# A regular install lays the package under site-packages where the path is
# self-contained, and `docker exec ... python` resolves `registry` correctly.
# Hot-reload via the docker-compose volume mount still works because uvicorn's
# `--reload` watches the source files at /app/registry/.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --prefix=/install .

# ── runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# No curl, wget, or shell utilities in the final image.
# libpq-dev runtime is the only external dep needed for asyncpg at runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/* \
 && apt-get purge -y --auto-remove

# Non-root user.
RUN useradd -m -u 999 -g 0 registry

WORKDIR /app

# Copy installed packages from builder.
COPY --from=builder --chown=registry:root /install /usr/local
# Copy application source with correct ownership.
COPY --from=builder --chown=registry:root /build/registry ./registry
COPY --from=builder --chown=registry:root /build/sync ./sync
COPY --from=builder --chown=registry:root /build/scripts ./scripts
COPY --from=builder --chown=registry:root /build/alembic.ini ./
COPY --from=builder --chown=registry:root /build/pyproject.toml ./

# Drop to non-root.
USER registry

EXPOSE 8000

# Default: run API server. Override command to run sync-worker:
#   command: ["python", "-m", "registry.sync_worker"]
CMD ["uvicorn", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "5", "--factory", "registry.main:create_app"]

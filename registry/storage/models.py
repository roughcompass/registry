"""SQLAlchemy 2.0 declarative ORM mapped classes for the catalog schema.

Performance-critical indexes are declared both in the Alembic migration that
creates them (authoritative DDL source) and in ``__table_args__`` on the
relevant model class (documentation for service-code readers). PARTITION
declarations live in the migrations only.

The ORM exists to give service code a typed Python surface; SQL constraints
(NOT NULL, CHECK, foreign keys) are the authoritative isolation guard.

`TenantMixin` adds an `INSERT`-time assertion that `tenant_id is not None` —
defense-in-depth on top of the SQL `NOT NULL` constraint.

`Fact.sync_run_id` is a nullable UUID column with no FK until the sync_runs
migration runs (the FK is activated in the migration that creates sync_runs).

`Role`, `ActorRole`, and `RateLimit` mapped classes support RBAC.
Default role seeding (4 roles per tenant: consumer, producer, admin, auditor) is
performed by `CatalogService.seed_default_roles(session, tenant_id)` at tenant
creation time — not in the migration.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector  # type: ignore[import-untyped]
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Mapper,
    mapped_column,
)


class Base(DeclarativeBase):
    pass


class TenantMixin:
    """Defense-in-depth: every tenant-scoped row must carry a non-NULL tenant_id."""

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


def _assert_tenant_id(_mapper: Mapper[Any], _connection: Any, target: Any) -> None:
    if target.tenant_id is None:
        msg = f"{type(target).__name__} insert without tenant_id (TenantMixin invariant)"
        raise ValueError(msg)


@event.listens_for(TenantMixin, "before_insert", propagate=True)
def _tenant_mixin_before_insert(mapper: Mapper[Any], connection: Any, target: Any) -> None:
    _assert_tenant_id(mapper, connection, target)


class Tenant(Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Opaque ID assigned by an upstream identity system. NULL for manually-provisioned
    # tenants. Uniqueness among non-NULL rows is enforced by a partial DB index
    # (ix_tenants_external_tenant_id_provider) — see the 0015 migration.
    external_tenant_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How this tenant was created. CHECK constraint in DB enforces 'manual' | 'jit' | 'system'.
    # The specific upstream source name belongs in audit-log payloads, not here.
    provider: Mapped[str] = mapped_column(Text, nullable=False, default="manual")


class Actor(Base, TenantMixin):
    __tablename__ = "actors"

    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    oidc_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 'human' | 'sync_worker'.  Partial unique index uq_actors_tenant_sync_type
    # enforces (tenant_id, display_name) uniqueness for sync_worker actors only
    # (see 0004_phase3_sync_infra migration), while human actors may share display names.
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False, default="human")


class ApiToken(Base, TenantMixin):
    __tablename__ = "api_tokens"

    token_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VocabularyValue(Base, TenantMixin):
    __tablename__ = "vocabulary_values"

    vocab_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deprecated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Entity(Base, TenantMixin):
    __tablename__ = "entities"
    __table_args__ = (
        # Supports keyset pagination: WHERE tenant_id = :t AND (created_at, entity_id) < (:ts, :id)
        # ORDER BY created_at DESC, entity_id.  Without this index Postgres scans all
        # tenant rows and sorts them before applying LIMIT — degrades linearly with table size.
        # Authoritative DDL: migration 0013_missing_indexes.
        Index("idx_entities_tenant_created", "tenant_id", "created_at", "entity_id"),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )
    # visibility column added by the provider/consumer Alembic migration.
    # CHECK (visibility IN ('private', 'tenant-shared', 'public'))
    # ORM column declared here so service code compiles before the migration runs.
    visibility: Mapped[str] = mapped_column(Text, nullable=False, default="private")


# notification_deliveries is queried via raw SQL (see workers/webhook_delivery.py).
# The partial index on that table is declared here for discoverability:
#
#   idx_delivery_pending_sort  ON notification_deliveries
#       (tenant_id, next_retry_at, attempted_at) WHERE status = 'pending'
#
# The webhook worker's claim query sorts by next_retry_at NULLS FIRST, attempted_at.
# Including attempted_at in the index avoids a re-sort pass on the filtered rows.
# Authoritative DDL: migration 0013_missing_indexes.


class Attribute(Base, TenantMixin):
    __tablename__ = "attributes"

    attr_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    t_valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_valid_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class Fact(Base, TenantMixin):
    __tablename__ = "facts"

    fact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_format: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_authoritative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_authoritative_superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # FK to sync_runs(sync_run_id) activated by the sync-infra migration once
    # the sync_runs table exists.
    sync_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sync_runs.sync_run_id"), nullable=True
    )
    t_valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_valid_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class Edge(Base, TenantMixin):
    __tablename__ = "edges"

    edge_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    src_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False
    )
    rel: Mapped[str] = mapped_column(Text, nullable=False)
    dst_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False
    )
    properties: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_authoritative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    t_valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_valid_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class Episode(Base, TenantMixin):
    __tablename__ = "episodes"

    episode_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    episode_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True)
    content_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Provenance(Base, TenantMixin):
    __tablename__ = "provenance"

    prov_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    claim_type: Mapped[str] = mapped_column(Text, nullable=False)
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    episode_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("episodes.episode_id"), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditLog(Base, TenantMixin):
    __tablename__ = "audit_log"

    audit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    before_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ts: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)


# --- Schema registry additions ---


class CapabilityTypeSchema(Base, TenantMixin):
    __tablename__ = "capability_type_schemas"

    schema_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    type_name: Mapped[str] = mapped_column(Text, nullable=False)
    json_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_advisory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    t_valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_valid_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


# --- Embedding additions ---


class Embedding(Base, TenantMixin):
    """One row per embedded text chunk.

    `claim_id` references `facts.fact_id`.  `chunk_index` is 0 for
    whole-body embeds; >0 for sliding-window chunks.  `ts_fact` mirrors
    the source fact's `t_valid_from` so retrieval can apply temporal
    pre-filters without joining `facts`.  `ts_vector` is a GENERATED
    ALWAYS column managed by Postgres — it must not be written by the
    ORM; declare as server_default with no Python-side setter.

    After ``scripts/partition_migrate.py`` runs, the physical table becomes
    ``PARTITION BY HASH (tenant_id)`` with 8 child partitions
    ``embeddings_p{0..7}``.  Each child carries its own HNSW index
    (``idx_embed_new_hnsw_p{n}``).  SQLAlchemy does not declare native
    partitioning on the ORM class — this mapping targets the parent table name
    ``embeddings`` and works identically before and after the cutover; the
    query planner prunes to the relevant hash bucket automatically when a
    ``WHERE tenant_id = :tid`` filter is present (as in every RetrievalService
    ANN query).  No ORM change is required for the per-partition HNSW benefit.
    """

    __tablename__ = "embeddings"

    embedding_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    claim_type: Mapped[str] = mapped_column(Text, nullable=False)
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("facts.fact_id"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Vector(384) requires the pgvector SQLAlchemy extension type.
    vector: Mapped[Any] = mapped_column(Vector(384), nullable=False)
    text_chunk: Mapped[str] = mapped_column(Text, nullable=False)
    ts_fact: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EmbeddingOutbox(Base, TenantMixin):
    """Transactional outbox for the async embedding drain job.

    Written in the same transaction as the parent fact row so a rollback
    removes both atomically.  The drain job deletes rows from this table
    after successfully inserting into `embeddings`.
    """

    __tablename__ = "embedding_outbox"

    outbox_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    claim_type: Mapped[str] = mapped_column(Text, nullable=False)
    fact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("facts.fact_id"), nullable=False)
    text_to_embed: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_plan: Mapped[Any] = mapped_column(JSONB, nullable=False)
    enqueued_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmbeddingOutboxFailed(Base, TenantMixin):
    """Dead-letter table for outbox rows that exceeded `outbox_max_attempts`.

    The drain job moves rows here after `attempts >= settings.outbox_max_attempts`
    (default 5).  A Prometheus alert fires when this table grows.
    """

    __tablename__ = "embedding_outbox_failed"

    failed_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    claim_type: Mapped[str] = mapped_column(Text, nullable=False)
    fact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("facts.fact_id"), nullable=False)
    text_to_embed: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_plan: Mapped[Any] = mapped_column(JSONB, nullable=False)
    failed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_text: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)


# --- Sync infrastructure additions ---


class SyncSource(Base, TenantMixin):
    """One row per configured connector source.

    `source_type` is vocab-validated at the service layer against
    `vocabulary_values` (kind='source_type'); no DB CHECK constraint is
    used here, matching the pattern for `entity_type` on `entities`.

    `config` is an opaque JSONB blob; the connector implementation is
    responsible for interpreting it.  `credentials_ref` is an environment
    variable name resolved at runtime — never stored as a credential value.
    """

    __tablename__ = "sync_sources"

    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[Any] = mapped_column(JSONB, nullable=False)
    credentials_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class SyncRun(Base, TenantMixin):
    """One row per execution of a sync source ingestion.

    `status` and `trigger` are CHECK-constrained at the DB level
    (allowed status values: 'running', 'done', 'partial', 'failed';
    allowed trigger values: 'scheduled', 'webhook', 'manual').  The ORM
    does not re-declare these constraints — they live in the migration DDL.

    `duration_s` and `artifact_count` are NULL until the run finishes.
    `error_summary` is set when status is 'partial' or 'failed'.
    """

    __tablename__ = "sync_runs"

    sync_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sync_sources.source_id"), nullable=False
    )
    # Allowed values: 'running' | 'done' | 'partial' | 'failed' (CHECK in DB)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Allowed values: 'scheduled' | 'webhook' | 'manual' (CHECK in DB)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class WebhookDelivery(Base, TenantMixin):
    """Idempotency log for inbound webhook payloads.

    Composite PK `(tenant_id, delivery_id)` ensures a given provider-assigned
    delivery ID cannot be processed twice within a tenant.  `processed_at`
    being NULL indicates the payload arrived but has not yet been drained.
    """

    __tablename__ = "webhook_deliveries"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sync_sources.source_id"), nullable=False
    )
    received_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --- RBAC additions ---


class Role(Base, TenantMixin):
    """One row per named role per tenant.

    ``name`` is one of 'consumer', 'producer', 'admin', 'auditor' (CHECK in
    DB).  ``permissions`` is an opaque TEXT[] blob interpreted by service code.

    Default roles (all four names, empty permissions) are seeded at tenant
    creation time by ``CatalogService.seed_default_roles`` — not by the
    migration.
    """

    __tablename__ = "roles"

    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActorRole(Base, TenantMixin):
    """Junction table assigning roles to actors within a tenant.

    Composite PK ``(tenant_id, actor_id, role_id)`` prevents duplicate grants.
    ``granted_by`` is a nullable FK to actors — NULL means system-seeded.
    """

    __tablename__ = "actor_roles"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), primary_key=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("actors.actor_id"), primary_key=True)
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("roles.role_id"), primary_key=True)
    granted_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


# --- External-ID registry ---


class ExternalSystem(Base, TenantMixin):
    """Registry of upstream external systems whose IDs are mapped onto entities.

    ``(tenant_id, slug)`` is the composite primary key — slugs are
    tenant-scoped so different tenants may independently use the same slug.
    ``url_template`` is optional; when present the service substitutes
    ``{external_id}`` at mapping-insert time.
    """

    __tablename__ = "external_systems"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    url_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EntityExternalId(Base, TenantMixin):
    """Passive external-system ID mapping.  Hard-delete only; no soft-history.

    External IDs are immutable once written; use hard-delete and re-insert
    to replace them.  Unique constraint ``uq_entity_external_id`` on
    ``(tenant_id, external_system_slug, external_id)`` is enforced at the DB
    level; the service converts ``IntegrityError`` to ``ConflictError``.
    """

    __tablename__ = "entity_external_ids"

    external_id_pk: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    external_system_slug: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --- PII pattern admin ---


class PiiPatternRow(Base, TenantMixin):
    """Tenant PII pattern registry row (both built-in system rows and custom tenant rows).

    ``is_system=True`` rows are seeded by the graph-primitives migration and must
    not be modified or deleted by tenant admins (403).  ``regex='__entropy__'`` is
    the sentinel for the entropy-based aws_secret_key pattern.

    ``policy_override`` overrides the tenant-default policy for this pattern;
    NULL means "fall back to tenant default" (level 2 in the three-level
    resolution hierarchy).

    ``uq_pii_pattern_tenant_name`` index enforces name uniqueness per tenant.
    """

    __tablename__ = "pii_patterns"

    pattern_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    regex: Mapped[str] = mapped_column(Text, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    detector_module: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class PiiFieldPolicyRow(Base, TenantMixin):
    """Per-field (and optionally per-pattern) PII policy override.

    ``pattern_id`` may be NULL, meaning the policy applies to ALL patterns for
    this field.  The DB unique index ``uq_field_policy`` uses
    ``COALESCE(pattern_id, zero_uuid)`` so that at most one NULL-pattern row
    exists per ``(tenant_id, field_type)`` pair.
    """

    __tablename__ = "pii_field_policies"

    policy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    field_type: Mapped[str] = mapped_column(Text, nullable=False)
    pattern_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pii_patterns.pattern_id"), nullable=True
    )
    policy: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RateLimit(Base, TenantMixin):
    """Per-actor (or per-tenant default) rate-limit override row.

    When ``actor_id IS NULL`` the row is the tenant-level default.  The DB
    enforces at most one default row per tenant and at most one per
    (tenant_id, actor_id) pair via partial unique indexes in the migration.

    Reactive activation: per-actor rows are inserted only when a runaway
    actor is detected (OQ3); the tenant default row is inserted at tenant
    creation time by ``CatalogService.seed_default_roles``.
    """

    __tablename__ = "rate_limits"

    limit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True)
    reads_per_second: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    writes_per_second: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --- Progression definitions ---


class ProgressionDefinition(Base, TenantMixin):
    """Bi-temporal definition of stage-transition rules for an entity type.

    Each row describes how entities of a given ``entity_type`` within a tenant
    may move between stages. ``definition`` is an opaque JSONB blob interpreted
    by the progression service; the schema is validated at write time, not here.

    ``is_advisory`` controls enforcement: FALSE means the service rejects
    invalid transitions; TRUE means it records a warning and allows them.

    Bi-temporal columns follow the registry standard:
      - ``t_valid_from`` / ``t_valid_to``   — real-world validity window
      - ``t_ingested_at`` / ``t_invalidated_at`` — registry observation window

    The unique constraint on ``(tenant_id, entity_type, t_valid_from)`` prevents
    two definitions from starting at the same instant, removing ambiguity when
    the service resolves the active definition for a given (tenant, entity_type).
    """

    __tablename__ = "progression_definitions"

    progression_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    definition: Mapped[Any] = mapped_column(JSONB, nullable=False)
    is_advisory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    t_valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_valid_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProgressionOverride(Base, TenantMixin):
    """Single-use grant authorizing an entity to bypass a gate for a specific transition.

    Each row represents an explicit override issued by an authorized actor. The
    validity window (``t_valid_from`` / ``t_valid_to``) bounds when the override
    may be consumed. ``gate_id`` identifies the specific gate to bypass, or "*"
    meaning any gate on that transition.

    ``bypass_skip_rules`` defaults to False. Set it to True only when the override
    is intended to allow skipping intermediate states as well as the gate check —
    this must be an explicit opt-in per the override schema.

    Single-use invariant: ``consumed_at IS NULL`` means the override is available.
    The progression service writes ``consumed_at`` in the same transaction as the
    transition it authorizes. No DB constraint enforces single-use — the service
    owns this invariant and must check ``consumed_at IS NULL`` before consuming.
    """

    __tablename__ = "progression_overrides"

    override_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False
    )
    from_state: Mapped[str] = mapped_column(Text, nullable=False)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    gate_id: Mapped[str] = mapped_column(Text, nullable=False)
    bypass_skip_rules: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    authorized_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=False
    )
    t_valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_valid_to: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # References audit_log(audit_id) — the audit record written at the time the
    # override was issued. Column is named audit_event_id to match the domain term
    # used in override-creation requests; the DB FK resolves to audit_log.audit_id.
    audit_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audit_log.audit_id"), nullable=False
    )


# --- Capability annotations ---


class AnnotationRecord(Base):
    """Bi-temporal record of a consumer annotation against a capability.

    One row per annotation submission.  Soft-delete is implemented via
    ``t_invalidated_at``: active annotations always have
    ``t_invalidated_at IS NULL``.  Hard-delete is never performed in this
    phase; physical purge is a future concern.

    ``body`` is NOT NULL in this phase — every annotation must carry a body.
    ``triage_note`` is optional; it is written by the capability owner during
    triage and may remain NULL throughout the annotation's lifetime.

    ``author_actor_id`` and ``author_tenant_id`` are both NOT NULL; they record
    the submitting actor and their tenant so the service can enforce the
    provider path vs. author path distinction on list queries without an extra
    join to the capabilities table.

    Bi-temporal columns follow the registry standard:
      - ``t_valid_from`` / ``t_valid_to``     — real-world validity window
      - ``t_ingested_at`` / ``t_invalidated_at`` — registry observation window

    Body access: always go through ``_serialize_body()`` rather than reading
    ``record.body`` directly.  That single method is the ENC-phase handoff
    seam: when encryption is retrofitted, only that one method grows the
    conditional decrypt branch.  Scattered ``row.body`` accesses elsewhere
    would require a broad sweep at that point and risk missing a callsite.
    """

    __tablename__ = "capability_annotations"

    annotation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    capability_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    author_actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    author_tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    triage_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    version_target: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_valid_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def _serialize_body(self) -> str:
        """Return the annotation body as a string.

        This phase: body is always NOT NULL plaintext; this returns it directly.
        This single accessor is the ENC-phase handoff seam: when encryption ships,
        only this method gets the conditional decrypt branch. Every body access
        in the service layer goes through this helper, not through ``record.body``.
        """
        return self.body


# --- Workspace additions ---


class WorkspaceRecord(Base, TenantMixin):
    """One row per workspace.

    ``owner_kind`` is CHECK-constrained to 'actor' | 'tenant' in the DB.
    When ``owner_kind = 'actor'``, ``owner_actor_id`` must be non-NULL
    (enforced by ``chk_actor_owner`` in the DB).

    ``encryption_tier`` is NOT NULL with a server default of 'none' — it is a
    forward-compatibility column so the regulated-tenant block and future ENC
    detection can read it without a schema change. WS-phase service code only
    reads it to enforce the regulated-tenant gate; it never writes a value other
    than 'none'.

    Soft-delete is implemented via ``t_invalidated_at``: active workspaces always
    have ``t_invalidated_at IS NULL``. Hard-delete is not performed in this phase.

    ``archived_at`` marks a workspace as archived (read-only) without
    soft-deleting it. A non-NULL ``archived_at`` means entry writes are rejected
    by the service layer.
    """

    __tablename__ = "workspaces"

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # CHECK (owner_kind IN ('actor','tenant')) enforced in DB
    owner_kind: Mapped[str] = mapped_column(Text, nullable=False)
    owner_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )
    # Forward-compatibility column for future ENC-phase detection. WS-phase code
    # only reads this to enforce the regulated-tenant block; it never writes a
    # value other than 'none'. NOT NULL with DB DEFAULT 'none'.
    encryption_tier: Mapped[str] = mapped_column(Text, nullable=False, default="none")
    archived_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class WorkspaceEntryRecord(Base, TenantMixin):
    """One row per entry within a workspace.

    ``body_md`` is NOT NULL in this phase — every entry must carry a plaintext
    body. The ENC-phase ALTER TABLE will drop the NOT NULL constraint and add
    ``body_ciphertext`` / ``body_nonce`` columns at that point. No ciphertext
    columns exist on this ORM class; their presence is a contract violation.

    ``references_jsonb`` is an optional JSONB blob for structured cross-reference
    metadata (e.g. linked entity schemas).

    ``reference_ids`` is a UUID[] column holding the IDs of entities or facts
    this entry directly references. The GIN index ``idx_we_refs`` enables
    efficient ``ANY(reference_ids)`` lookups in the service layer without
    loading every entry row.

    ``kind`` is CHECK-constrained in the DB to the set of known entry kinds
    ('note', 'decision', 'open_question', 'saved_query', 'saved_view',
    'private_annotation').

    Soft-delete via ``t_invalidated_at``. Hard-delete is performed only by the
    RTBF purge path (physical purge, not soft-delete).
    """

    __tablename__ = "workspace_entries"

    entry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.workspace_id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    # CHECK (kind IN ('note','decision','open_question','saved_query','saved_view',
    #   'private_annotation')) enforced in DB
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # NOT NULL in this phase: plaintext body required. ENC-phase ALTER drops this
    # constraint and adds body_ciphertext/body_nonce — no ORM change here until then.
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional JSONB blob for structured cross-reference metadata.
    references_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # UUID[] — GIN-indexed (idx_we_refs) for fast ANY(reference_ids) filtering.
    reference_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class WorkspaceShareRecord(Base, TenantMixin):
    """One row per share grant on a workspace.

    ``role`` is CHECK-constrained to 'reader' | 'contributor' in the DB.

    ``grantee_tenant_id`` enables cross-tenant share detection: when
    ``tenant_id != grantee_tenant_id``, the share is cross-tenant. The
    BEFORE INSERT trigger ``trg_ws_share_cross_tenant`` rejects cross-tenant
    shares on actor-owned workspaces at the DB layer; the service layer guard
    returns HTTP 422 before the INSERT is attempted.

    ``revoked_at`` is the soft-delete sentinel. A NULL ``revoked_at`` means the
    share is active. The unique partial index ``uq_share`` on
    ``(workspace_id, grantee_actor_id) WHERE revoked_at IS NULL`` enforces at
    most one active share per (workspace, grantee) pair.
    """

    __tablename__ = "workspace_shares"

    share_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.workspace_id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    grantee_actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=False
    )
    grantee_tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False
    )
    # CHECK (role IN ('reader','contributor')) enforced in DB
    role: Mapped[str] = mapped_column(Text, nullable=False, default="reader")
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )
    granted_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceShareAcceptanceRecord(Base):
    """One row per explicit acceptance of a cross-tenant share by a grantee actor.

    Written on first cross-tenant workspace access so the service can record
    that the grantee acknowledged the share. The unique index
    ``uq_acceptance`` on ``(share_id, accepting_actor_id)`` makes acceptance
    idempotent — repeated first-access calls are safe.

    ``accepting_tenant_id`` is stored denormalized so the service can filter
    acceptances by grantee tenant without joining ``workspace_shares``.

    This table does NOT carry a ``tenant_id`` column (unlike most tables in
    this schema). The accepting side is identified by ``accepting_tenant_id``;
    the granting side is reachable via ``share_id → workspace_shares``.
    TenantMixin is intentionally not applied here.
    """

    __tablename__ = "workspace_share_acceptances"

    acceptance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    share_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_shares.share_id"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.workspace_id"), nullable=False
    )
    accepting_actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=False
    )
    accepting_tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False
    )
    accepted_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

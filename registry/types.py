"""Shared dataclasses and protocols. Single import leaf for the package.

No imports from other catalog/ modules. Stdlib + numpy only.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True)
class TenantContext:
    """Injected by auth middleware into every request. Never constructed by service code."""

    tenant_id: uuid.UUID
    actor_id: uuid.UUID
    roles: list[str]


@dataclass
class EntityRef:
    entity_id: uuid.UUID
    tenant_id: uuid.UUID
    entity_type: str
    name: str
    external_id: str | None
    is_active: bool
    created_at: datetime.datetime


@dataclass
class FactRef:
    fact_id: uuid.UUID
    tenant_id: uuid.UUID
    entity_id: uuid.UUID
    category: str
    body: str
    is_authoritative: bool
    is_authoritative_superseded: bool
    sync_run_id: uuid.UUID | None
    t_valid_from: datetime.datetime
    t_valid_to: datetime.datetime | None
    t_ingested_at: datetime.datetime
    t_invalidated_at: datetime.datetime | None
    title: str | None = None
    body_format: str | None = None
    created_by: uuid.UUID | None = None


@dataclass
class EdgeRef:
    edge_id: uuid.UUID
    tenant_id: uuid.UUID
    src_entity_id: uuid.UUID
    rel: str
    dst_entity_id: uuid.UUID
    properties: dict[str, Any] | None
    t_valid_from: datetime.datetime
    t_valid_to: datetime.datetime | None
    t_ingested_at: datetime.datetime
    t_invalidated_at: datetime.datetime | None


@dataclass
class AdoptionEventRef:
    """One adoption event.

    A consumer tenant declaring intent to depend on a provider tenant's
    capability. ``tenant_id`` is the **provider** (owner of the capability);
    ``consumer_tenant_id`` is the consumer that adopted it. Adoption is the
    only path that legitimately creates a cross-tenant ``provides_to`` edge.
    """

    adoption_id: uuid.UUID
    tenant_id: uuid.UUID
    provider_capability_id: uuid.UUID
    consumer_tenant_id: uuid.UUID
    actor_id: uuid.UUID | None
    intent: str | None
    version_pin: str | None
    t_valid_from: datetime.datetime
    t_valid_to: datetime.datetime | None
    t_ingested_at: datetime.datetime
    t_invalidated_at: datetime.datetime | None


@dataclass
class SubscriptionRef:
    """One subscription row.

    Bi-temporal: a subscription is "soft-deleted" by setting
    ``t_invalidated_at``; the row persists for audit.
    ``digest_window`` snapshots the tenant's ``notification_digest_window``
    at create time and is NOT retroactively updated — callers that change
    the tenant window after subscription creation must re-subscribe if they
    need the new window applied.
    Inbox-only subscriptions have ``webhook_url`` and
    ``webhook_hmac_secret_ref`` set to ``None``.
    """

    subscription_id: uuid.UUID
    tenant_id: uuid.UUID
    actor_id: uuid.UUID | None
    capability_id: uuid.UUID
    event_kinds: list[str]
    webhook_url: str | None
    webhook_hmac_secret_ref: str | None
    is_enabled: bool
    digest_window: str
    t_valid_from: datetime.datetime
    t_valid_to: datetime.datetime | None
    t_ingested_at: datetime.datetime
    t_invalidated_at: datetime.datetime | None


@dataclass
class InterfaceSurface:
    """Canonical interface surface.

    Produced by ``interface_normalize.normalize`` from one of the three
    supported source formats (json_schema, typescript, openapi). The
    diff engine (T19) and breaking-change advisor (T20) operate on this
    shape exclusively — the raw ``interface_source`` is retained for
    audit but never re-parsed downstream.

    Attributes
    ----------
    operations:
        List of ``{name, method, params: list[{name, type, required}],
        returns: type}`` dicts. Sourced from OpenAPI paths.
    events:
        List of ``{name, payload_fields: list[{name, type}]}`` dicts.
        Reserved for json_schema "events" key or TypeScript event types
        (T19 may extend).
    fields:
        List of ``{name, type, required}`` dicts. Captures plain
        TypeScript ``type``/``interface`` declarations and JSON Schema
        ``properties``.
    """

    operations: list[dict[str, Any]]
    events: list[dict[str, Any]]
    fields: list[dict[str, Any]]


@dataclass
class BreakingChangePreview:
    """Read-only advisory result from the breaking-change advisor endpoint.

    ``affected_consumers`` anonymises cross-tenant identifiers: same-tenant
    consumers expose full identifiers while cross-tenant consumers carry
    opaque counter/hash placeholders so the provider learns impact size and
    shape without learning *which* external tenants are affected.
    """

    capability_id: uuid.UUID
    proposed_version: str
    diff_classification: str
    changes: list[dict[str, Any]]
    affected_consumers: list[dict[str, Any]]
    release_notes_scaffold: str


@dataclass
class CapabilityRegistryEvent:
    """Payload-minimal subscription event envelope.

    No body text, descriptions, fact text, or freeform content is ever included.
    Consumers must follow ``fetch_url`` to retrieve the canonical record. This
    dataclass is the JSON shape delivered by both the webhook worker and the
    in-catalog notification inbox.
    """

    notification_id: uuid.UUID
    tenant_id: uuid.UUID
    subscription_id: uuid.UUID | None
    capability_id: uuid.UUID
    capability_slug: str
    event_kind: str
    change_classification: str | None
    version_before: str | None
    version_after: str | None
    occurred_at: datetime.datetime
    fetch_url: str


@dataclass
class TraversalResult:
    """Returned by dependents, dependencies, and blast-radius endpoints.

    Attributes
    ----------
    root_entity_id:
        The entity from which the traversal was started.
    depth:
        The maximum hop depth requested by the caller (already capped at 5 by
        the service layer).
    direction:
        ``'forward'`` — who does root depend on?
        ``'reverse'`` — who depends on root?
    as_of:
        The effective bi-temporal anchor used for the traversal, or ``None``
        for current-truth queries.
    nodes:
        Deduplicated list of ``EntityRef`` objects reached during traversal
        (root not included).
    edges:
        Ordered list of ``EdgeRef`` objects traversed, one per hop path member.
        This is a flat representation; callers can reconstruct the path from
        ``edge_path`` lists in the raw CTE rows if needed.
    version_satisfied:
        Maps ``edge_id → predicate result``.  ``True`` for all edges when
        version predicates are not yet evaluated; populated once version
        predicate support is active.
    cache_hit:
        ``True`` if the result was served from ``closure_cache``; ``False`` if
        the live CTE was executed.  ``False`` when the cache is not yet
        populated.
    """

    root_entity_id: uuid.UUID
    depth: int
    direction: Literal["forward", "reverse"]
    as_of: datetime.datetime | None
    nodes: list[EntityRef]
    edges: list[EdgeRef]
    version_satisfied: dict[uuid.UUID, bool]
    cache_hit: bool


@dataclass
class CapabilityRecord:
    """Full capability with attached facts and edges; returned by get_capability."""

    entity: EntityRef
    attributes: dict[str, Any]
    lifecycle: str
    facts: list[FactRef]
    edges_out: list[EdgeRef]
    edges_in: list[EdgeRef]
    superseded_facts_count: int = 0
    superseded_fact_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass
class SyncWriteResult:
    """Counts returned by ``CatalogService.upsert_synced_facts``."""

    created: int
    skipped: int
    superseded: int


@dataclass
class SearchResult:
    entity: EntityRef
    matching_facts: list[FactRef]
    score: float
    retrieval_arms: dict[str, float]


@dataclass(frozen=True)
class PiiMatchResult:
    """A single PII match returned by a pattern's scan() method.

    Attributes
    ----------
    name:
        Canonical pattern name (e.g. ``'email'``, ``'aws_secret_key'``).
        Matches ``Scanner.name`` on the pattern module.
    offset:
        Zero-based character offset of the match start within the scanned text.
    length:
        Character length of the matched substring.
    category:
        PII category label (e.g. ``'CONTACT'``, ``'CREDENTIALS'``).
        Matches ``Scanner.category`` on the pattern module.
    """

    name: str
    offset: int
    length: int
    category: str


@dataclass
class PiiScanResponse:
    """Aggregated result returned by ``PiiScanner.scan()``.

    Attributes
    ----------
    matched_patterns:
        All matches across all enabled patterns, in scan order.  Empty when no
        PII was detected.
    action_taken:
        Effective action: max severity across all match-level effective policies.
        ``'advisory'`` when no matches; otherwise the highest severity present.
        Severity order: ``advisory`` < ``warn`` < ``block``.
    pii_warning:
        Human-readable warning message populated when ``action_taken == 'warn'``.
        ``None`` for ``'advisory'`` and ``'block'`` actions.
    """

    matched_patterns: list[PiiMatchResult]
    action_taken: Literal["advisory", "warn", "block"]
    pii_warning: str | None = None


@dataclass
class ExternalIdRef:
    """A single external-system ID mapping for an entity.

    Attributes
    ----------
    external_id_pk:
        Primary key of the ``entity_external_ids`` row. Opaque to callers;
        used for update and hard-delete operations.
    entity_id:
        The entity this mapping belongs to.
    tenant_id:
        Owning tenant. Always matches the calling ``TenantContext``.
    external_system_slug:
        Slug of the registered external system (FK into ``external_systems``).
    external_id:
        The raw ID as it appears in the upstream system.
    url:
        Resolved URL (from url_template substitution or explicit override).
        ``None`` when neither template nor explicit URL is available.
    metadata_jsonb:
        Arbitrary tenant-provided key/value payload. ``None`` when not set.
    created_at:
        Row creation timestamp (UTC).
    updated_at:
        Last modification timestamp (UTC).
    """

    external_id_pk: uuid.UUID
    entity_id: uuid.UUID
    tenant_id: uuid.UUID
    external_system_slug: str
    external_id: str
    url: str | None
    metadata_jsonb: dict[str, Any] | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


@dataclass
class TemporalFilter:
    as_of: datetime.datetime | None

    def is_time_travel(self) -> bool:
        return self.as_of is not None


# Time injection — always inject a Clock rather than calling datetime.now() directly.
# This makes test time controllable without monkeypatching.
class Clock(Protocol):
    """Source of UTC `now()`. All service code takes a Clock; never calls datetime.now()."""

    def now(self) -> datetime.datetime: ...


class SystemClock:
    """Production Clock. Returns wall-clock UTC."""

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(tz=datetime.UTC)


class FakeClock:
    """Test Clock. Returns a configurable instant; tick() advances it."""

    def __init__(self, start: datetime.datetime) -> None:
        self._t = start.astimezone(datetime.UTC)

    def now(self) -> datetime.datetime:
        return self._t

    def set(self, t: datetime.datetime) -> None:
        self._t = t.astimezone(datetime.UTC)

    def tick(self, delta: datetime.timedelta) -> None:
        self._t = self._t + delta


# Embedder injection — constructed once at app startup, injected into RetrievalService.
class Embedder(Protocol):
    """In-process embedding model. Constructed once at app startup; injected into RetrievalService."""

    model_version: str

    def encode(self, texts: list[str]) -> npt.NDArray[np.float32]: ...

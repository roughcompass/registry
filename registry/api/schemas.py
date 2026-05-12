"""Pydantic request/response models for the producer and consumer surfaces.

These models are the only type seam between HTTP and the service layer.
Routers are thin adapters over CatalogService / RetrievalService.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateCapabilityRequest(BaseModel):
    name: str
    entity_type: Literal["capability"] = "capability"
    external_id: str | None = None
    capability_type: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime.datetime | None = None


class CreateConceptRequest(BaseModel):
    name: str
    entity_type: Literal["concept"] = "concept"
    external_id: str | None = None
    parent_capability_id: uuid.UUID | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime.datetime | None = None


class CreateOperationRequest(BaseModel):
    name: str
    entity_type: Literal["operation"] = "operation"
    external_id: str | None = None
    parent_capability_id: uuid.UUID | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime.datetime | None = None


class UpdateEntityRequest(BaseModel):
    """Bag of attribute updates applied bi-temporally; does not change entity_type or name directly."""

    updates: dict[str, Any]
    valid_from: datetime.datetime | None = None


class SetVisibilityRequest(BaseModel):
    """Body for PATCH /v1/capabilities/{entity_id}/visibility.

    ``visibility`` must be one of: ``private``, ``tenant-shared``, ``public``.
    ``shared_with_tenants`` is required (non-empty) when ``visibility='tenant-shared'``;
    validation is enforced at the service layer (VisibilityService) and surfaced as HTTP 422.
    """

    visibility: str
    shared_with_tenants: list[uuid.UUID] | None = None


class Links(BaseModel):
    """HATEOAS-style navigation pointers for a resource.

    Every detail response includes ``_links.self``; richer resources
    expose pointers to related sub-resources (e.g. capability detail
    points at its dependencies, artifacts, interface).

    URLs preserve the address form the caller used — slug paths get
    slug URLs back, UUID paths get UUID URLs.
    """

    self: str
    artifacts: str | None = None
    dependencies: str | None = None
    interface: str | None = None
    capability: str | None = None
    parent: str | None = None
    tenant: str | None = None
    actor: str | None = None


class WhoAmIResponse(BaseModel):
    """Session-context payload — what the calling token resolves to.

    Returned by ``GET /v1/whoami`` and the MCP ``whoami`` tool. UIs use
    it to render permission-gated buttons before any other call.
    """

    actor_id: uuid.UUID
    actor_display_name: str | None
    actor_email: str | None
    tenant_id: uuid.UUID
    tenant_slug: str
    tenant_display_name: str
    roles: list[str]
    token_id: uuid.UUID | None = None
    token_expires_at: datetime.datetime | None = None
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class TenantResponse(BaseModel):
    """Response shape for GET /v1/admin/tenants/{slug}.

    Callers only see their own tenant — cross-tenant lookup returns 404
    so existence of other tenants is never confirmed through this surface.

    Audit view (``?view=audit``) adds ``is_active``; default view omits it
    since active/inactive is an operator-level detail not needed by
    day-to-day clients.
    """

    tenant_id: uuid.UUID
    slug: str
    display_name: str
    created_at: datetime.datetime

    # Audit-only — populated when ?view=audit is passed.
    is_active: bool | None = None

    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class ActorResponse(BaseModel):
    """Response shape for GET /v1/admin/actors/{id} and list.

    Secrets (password hashes, token secrets) are never included.
    ``oidc_subject`` is omitted from the default view; present in
    the audit view (``?view=audit``) for traceability.
    """

    actor_id: uuid.UUID
    tenant_id: uuid.UUID
    display_name: str
    email: str | None
    actor_kind: str
    created_at: datetime.datetime

    # Audit-only — populated when ?view=audit is passed.
    oidc_subject: str | None = None

    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class ActorListResponse(BaseModel):
    """Paginated list of actors for GET /v1/admin/actors."""

    items: list[ActorResponse]
    next_cursor: str | None


class CapabilityResponse(BaseModel):
    entity_id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    external_id: str | None
    lifecycle: str
    attributes: dict[str, Any]
    created_at: datetime.datetime


class EntityDetailResponse(BaseModel):
    """Detail-GET response for concept and operation entities.

    Extends the base capability-record shape with HATEOAS navigation pointers.
    ``_links.self`` is always populated; ``_links.parent`` is populated when the
    entity carries a parent_capability_id (concept_of / operation_of edge) and
    that id is already present in the response — no extra fetch is performed.
    """

    entity_id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    external_id: str | None
    lifecycle: str
    attributes: dict[str, Any]
    created_at: datetime.datetime
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class CreateArtifactRequest(BaseModel):
    """Body for ``POST /v1/capabilities/{id}/artifacts``.

    ``title`` is required and validated server-side (1-200 chars, no
    leading/trailing whitespace). ``body_format`` is one of
    ``markdown`` (default), ``html``, ``plain``.
    """

    category: str
    title: str
    body: str
    body_format: str = "markdown"
    valid_from: datetime.datetime | None = None


class ArtifactListResponse(BaseModel):
    """Paginated artifact list. Same envelope shape as CapabilityListResponse.

    ``items`` carry the artifact rows shaped per the ``?fields=`` param;
    callers that want the full body must opt in via
    ``?fields=fact_id,category,title,body_format,created_at,body``.

    ``next_cursor`` is ``None`` when no further pages exist; pass it as
    ``cursor=`` on the next request to retrieve the following page.
    """

    items: list[ArtifactResponse]
    next_cursor: str | None


class ArtifactResponse(BaseModel):
    """An artifact (fact) attached to a capability.

    By default returns the UI-flavoured shape: the fact identifier, the
    category vocabulary value, the body, and when it was ingested. The
    bitemporal columns + tenant/entity FKs are audit-only and present
    only when ``?view=audit`` is passed (route-level ``exclude_unset``
    strips them otherwise).
    """

    fact_id: uuid.UUID
    # `category` and `created_at` are conceptually always present, but they're
    # Optional in the schema so the sparse `?fields=` projection can omit them.
    # `fact_id` is always included.
    category: str | None = None
    title: str | None = None
    body: str | None = None  # excluded by default in list responses unless ?fields=...,body
    body_format: str | None = None
    created_at: datetime.datetime | None = None  # source: t_ingested_at
    created_by_display_name: str | None = None

    # Audit-only fields — set by the handler when ?view=audit.
    # Field names drop the storage-side `t_` prefix; that's DB
    # nomenclature, not an API contract.
    tenant_id: uuid.UUID | None = None
    entity_id: uuid.UUID | None = None
    is_authoritative: bool | None = None
    valid_from: datetime.datetime | None = None
    valid_to: datetime.datetime | None = None
    ingested_at: datetime.datetime | None = None
    invalidated_at: datetime.datetime | None = None

    # HATEOAS-style navigation pointers (T08).
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Consumer read surface
# ---------------------------------------------------------------------------


class SearchResultItem(BaseModel):
    entity_id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    entity_type: str
    score: float
    retrieval_arms: dict[str, float]
    matching_facts: list[ArtifactResponse]


class SearchResponse(BaseModel):
    # `items` is the standard envelope field name for list payloads.
    # `total` is kept here because search results are bounded by `top_k` —
    # there is no cursor-based next page, so a total count is accurate and cheap.
    items: list[SearchResultItem]
    total: int
    took_ms: float


class EdgeRefItem(BaseModel):
    """An edge between two entities.

    UI shape by default (edge id, both endpoints, relation, properties).
    Bitemporal cols + tenant_id are audit-only — present only when the
    caller passes ``?view=audit``.
    """

    edge_id: uuid.UUID
    src_entity_id: uuid.UUID
    rel: str
    dst_entity_id: uuid.UUID
    properties: dict[str, Any] | None

    # Audit-only fields — set by the handler when ?view=audit.
    tenant_id: uuid.UUID | None = None
    valid_from: datetime.datetime | None = None
    valid_to: datetime.datetime | None = None
    ingested_at: datetime.datetime | None = None
    invalidated_at: datetime.datetime | None = None


class IncludedEntityItem(BaseModel):
    """An entity surfaced by ``?include=components`` or ``?include=depends_on``.

    Same shape as EntityRefItem plus the entity's current attribute set so
    consumers don't need a second round-trip to inspect basic metadata
    (display_name, summary, lifecycle.state, owner, …).
    """

    entity_id: uuid.UUID
    tenant_id: uuid.UUID
    entity_type: str
    name: str
    external_id: str | None
    is_active: bool
    created_at: datetime.datetime
    attributes: dict[str, Any]


class EntityCollectionExpansion(BaseModel):
    """Container for an included entity collection.

    ``truncated`` signals that the per-include cap was hit and ``next`` points
    at the dedicated endpoint that returns the full set.
    """

    items: list[IncludedEntityItem]
    truncated: bool
    next: str | None = None


class ExternalIdItem(BaseModel):
    """One row of the entity_external_ids registry, surfaced via ``?include=external_ids``."""

    external_system_slug: str
    external_id: str
    url: str | None
    metadata: dict[str, Any] | None


class ExternalIdsExpansion(BaseModel):
    items: list[ExternalIdItem]
    truncated: bool


class InterfaceExpansion(BaseModel):
    """Latest interface surface for the capability, surfaced via ``?include=interface``.

    ``surface`` is the canonical normalised JSON Schema document; ``raw`` is
    the original artifact the caller submitted (JSON Schema, TypeScript, or
    OpenAPI 3.x). Either may be ``None`` if no surface is registered.
    """

    surface: dict[str, Any] | None
    raw: dict[str, Any] | None
    format: str | None
    version: str | None


class CapabilityDetailResponse(BaseModel):
    """Capability record serialised for the consumer GET endpoint.

    Default shape is UI-flavoured. Audit-only fields (``tenant_id``,
    ``is_active``, ``superseded_facts_count``, ``as_of``) are present
    only when the caller passes ``?view=audit``; route-level
    ``response_model_exclude_unset`` strips them otherwise.

    The ``components``, ``depends_on``, ``external_ids``, and
    ``interface`` fields are populated only when the corresponding
    value appears in the ``?include=`` query parameter.
    """

    entity_id: uuid.UUID
    entity_type: str
    name: str
    external_id: str | None
    created_at: datetime.datetime
    lifecycle: str
    attributes: dict[str, Any]
    facts: list[ArtifactResponse]
    edges_out: list[EdgeRefItem]
    edges_in: list[EdgeRefItem]

    # Audit-only fields — set by the handler when ?view=audit.
    tenant_id: uuid.UUID | None = None
    is_active: bool | None = None
    superseded_facts_count: int | None = None
    as_of: datetime.datetime | None = None

    # HATEOAS-style navigation pointers (T08).
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}
    components: EntityCollectionExpansion | None = None
    depends_on: EntityCollectionExpansion | None = None
    external_ids: ExternalIdsExpansion | None = None
    interface: InterfaceExpansion | None = None


class DependencyResponse(BaseModel):
    root_entity_id: uuid.UUID
    depth: int
    as_of: datetime.datetime | None
    edges: list[EdgeRefItem]


class AdoptionResponse(BaseModel):
    """An adoption event linking a consumer to a provider capability.

    Default shape is UI-flavoured (core identifiers and intent fields only).
    Bitemporal columns are audit-only — present only when the caller passes
    ``?view=audit``. Route-level ``response_model_exclude_unset`` strips
    unset audit fields so they don't appear as null keys in default responses.
    """

    adoption_id: uuid.UUID
    tenant_id: uuid.UUID
    provider_capability_id: uuid.UUID
    consumer_tenant_id: uuid.UUID
    actor_id: uuid.UUID | None
    intent: str | None
    version_pin: str | None

    # Audit-only fields — set by the handler when ?view=audit.
    # Field names drop the storage-side `t_` prefix; that's DB nomenclature,
    # not an API contract.
    valid_from: datetime.datetime | None = None
    valid_to: datetime.datetime | None = None
    ingested_at: datetime.datetime | None = None
    invalidated_at: datetime.datetime | None = None

    # HATEOAS-style navigation pointers: self + capability pointer.
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class SubscriptionResponse(BaseModel):
    """A subscription that watches events on a capability.

    Default shape is UI-flavoured (core subscription fields only). Bitemporal
    columns are audit-only — present only when the caller passes ``?view=audit``.
    Route-level ``response_model_exclude_unset`` strips unset audit fields so
    they don't appear as null keys in default responses.
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

    # Audit-only fields — set by the handler when ?view=audit.
    # Field names drop the storage-side `t_` prefix; that's DB nomenclature,
    # not an API contract.
    valid_from: datetime.datetime | None = None
    valid_to: datetime.datetime | None = None
    ingested_at: datetime.datetime | None = None
    invalidated_at: datetime.datetime | None = None

    # HATEOAS-style navigation pointers: self + capability pointer.
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class InterfaceReadResponse(BaseModel):
    """GET /v1/capabilities/{id}/interface response.

    Default shape exposes the canonical surface, source, format, and time-travel
    ``as_of``. Bitemporal row metadata is audit-only — present only when the
    caller passes ``?view=audit``. Route-level ``response_model_exclude_unset``
    strips unset audit fields so they don't appear as null keys in default
    responses.
    """

    capability_id: str
    interface_canonical: Any | None
    interface_source: dict[str, Any] | None
    interface_format: str | None
    as_of: str | None

    # Audit-only fields — set by the handler when ?view=audit.
    # Field names drop the storage-side `t_` prefix; that's DB nomenclature,
    # not an API contract.
    valid_from: datetime.datetime | None = None
    valid_to: datetime.datetime | None = None
    ingested_at: datetime.datetime | None = None
    invalidated_at: datetime.datetime | None = None

    # HATEOAS-style navigation pointers: self + capability pointer.
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class EntityRefItem(BaseModel):
    entity_id: uuid.UUID
    tenant_id: uuid.UUID
    entity_type: str
    name: str
    external_id: str | None
    is_active: bool
    created_at: datetime.datetime


class CapabilityListResponse(BaseModel):
    items: list[EntityRefItem]
    next_cursor: str | None


class AdoptionListResponse(BaseModel):
    """Paginated list envelope for GET /v1/capabilities/{id}/adoptions.

    Cursor wiring: envelope-only. The adoption set for a single capability is
    small (one active adoption per consumer tenant), so ``next_cursor`` is
    always ``None`` in practice. The wrapper exists for shape consistency with
    every other list endpoint.
    """

    items: list[AdoptionResponse]
    next_cursor: str | None


class SubscriptionListResponse(BaseModel):
    """Paginated list envelope for GET /v1/capabilities/{id}/subscriptions.

    Cursor wiring: envelope-only. Subscriptions per capability per tenant are
    bounded (typically 1–5), so ``next_cursor`` is always ``None`` in practice.
    The wrapper exists for shape consistency.
    """

    items: list[SubscriptionResponse]
    next_cursor: str | None


class IntegrationListResponse(BaseModel):
    """Paginated list envelope for GET /v1/integrations.

    Cursor wiring: envelope-only. Integrations connecting two specific
    capabilities are bounded (typically 1–3), so ``next_cursor`` is always
    ``None`` in practice. The wrapper exists for shape consistency.
    """

    items: list[EntityRefItem]
    next_cursor: str | None


# ---------------------------------------------------------------------------
# Graph traversal
# ---------------------------------------------------------------------------


class TraversalResultResponse(BaseModel):
    """HTTP response shape for graph traversal endpoints.

    Maps one-to-one to TraversalResult; all UUID fields serialised as strings
    by Pydantic's default JSON encoder.
    """

    root_entity_id: uuid.UUID
    depth: int
    direction: str
    as_of: datetime.datetime | None
    nodes: list[EntityRefItem]
    edges: list[EdgeRefItem]
    version_satisfied: dict[str, bool]  # edge_id (str) → predicate result
    cache_hit: bool


# ---------------------------------------------------------------------------
# Provider/Consumer projections
# ---------------------------------------------------------------------------


class ProjectionResponse(BaseModel):
    """HTTP response shape for GET /v1/graph/provider and /v1/graph/consumer.

    Maps one-to-one to ``registry.service.projections.Projection``.
    ``next_cursor`` is None when no further pages exist; pass it as ``cursor=``
    on the next request to retrieve the following page.
    """

    nodes: list[EntityRefItem]
    edges: list[EdgeRefItem]
    next_cursor: str | None

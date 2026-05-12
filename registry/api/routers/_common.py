"""Shared helpers for entity-shaped routers (capability/concept/operation/artifact).

``get_service`` and ``to_response`` were originally defined as private
``_service`` and ``_to_response`` in ``capabilities.py`` and imported
across module boundaries by ``concepts.py``, ``operations.py``, and
``artifacts.py``. The underscore prefix claimed module-private; the
cross-module imports proved the symbols were de-facto shared. Promoted
here with an explicit ``__all__`` so the contract is intentional.

``edge_to_item`` is similarly shared: capabilities.py uses it for inline
``edges_out`` / ``edges_in`` lists and graph.py uses it for traversal
result edges. Both honour the same ``?view=audit`` contract.
"""

from __future__ import annotations

from fastapi import Request

from registry.api.schemas import CapabilityResponse, EdgeRefItem
from registry.service.catalog import CatalogService
from registry.types import CapabilityRecord, EdgeRef


def get_service(request: Request) -> CatalogService:
    """Return the ``CatalogService`` instance attached to the running app."""
    service: CatalogService = request.app.state.catalog
    return service


def to_response(record: CapabilityRecord) -> CapabilityResponse:
    """Convert a ``CapabilityRecord`` to the basic ``CapabilityResponse`` shape."""
    return CapabilityResponse(
        entity_id=record.entity.entity_id,
        tenant_id=record.entity.tenant_id,
        name=record.entity.name,
        external_id=record.entity.external_id,
        lifecycle=record.lifecycle,
        attributes=record.attributes,
        created_at=record.entity.created_at,
    )


def edge_to_item(edge: EdgeRef, *, audit: bool = False) -> EdgeRefItem:
    """Convert an EdgeRef to the response item.

    Default shape is UI-flavoured (no bitemporal cols, no tenant_id).
    Pass ``audit=True`` to populate the full audit shape — used by
    ``?view=audit`` on any endpoint that emits edges.
    """
    if audit:
        return EdgeRefItem(
            edge_id=edge.edge_id,
            tenant_id=edge.tenant_id,
            src_entity_id=edge.src_entity_id,
            rel=edge.rel,
            dst_entity_id=edge.dst_entity_id,
            properties=edge.properties,
            valid_from=edge.t_valid_from,
            valid_to=edge.t_valid_to,
            ingested_at=edge.t_ingested_at,
            invalidated_at=edge.t_invalidated_at,
        )
    return EdgeRefItem(
        edge_id=edge.edge_id,
        src_entity_id=edge.src_entity_id,
        rel=edge.rel,
        dst_entity_id=edge.dst_entity_id,
        properties=edge.properties,
    )


__all__ = ["get_service", "to_response", "edge_to_item"]

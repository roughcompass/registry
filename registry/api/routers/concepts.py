"""/v1/concepts — concepts attach to a parent capability via an atomic concept_of edge.

PATCH and DELETE are registered via HttpMethodRouter so REGISTRY_HTTP_METHODS_MODE
controls the exposed verb surface.
"""

from __future__ import annotations

from registry.api.routers._entity_crud import make_entity_router
from registry.api.schemas import CreateConceptRequest

router, mutation_router = make_entity_router(
    entity_type="concept",
    parent_edge_rel="concept_of",
    prefix="/v1/concepts",
    tag="concepts",
    create_request_model=CreateConceptRequest,
)

__all__ = ["router", "mutation_router"]

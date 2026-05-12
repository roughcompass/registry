"""/v1/operations — operations attach to a parent capability via an atomic operation_of edge.

PATCH and DELETE are registered via HttpMethodRouter so REGISTRY_HTTP_METHODS_MODE
controls the exposed verb surface.
"""

from __future__ import annotations

from registry.api.routers._entity_crud import make_entity_router
from registry.api.schemas import CreateOperationRequest

router, mutation_router = make_entity_router(
    entity_type="operation",
    parent_edge_rel="operation_of",
    prefix="/v1/operations",
    tag="operations",
    create_request_model=CreateOperationRequest,
)

__all__ = ["router", "mutation_router"]

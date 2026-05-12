"""Re-export shim for the admin router split.

The admin surface was split into focused per-domain modules:
  admin_tokens.py    — token mint/revoke
  admin_sync.py      — sync-source CRUD + sync-run history
  admin_vocab.py     — vocabulary + capability-type schemas
  admin_audit.py     — audit log query
  admin_rbac.py      — tenants, actors, roles
  admin_pii.py       — PII patterns + field policies
  admin_lifecycle.py — lifecycle state transitions

This module re-exports every name that main.py and existing tests use so
import sites do not need to change during the transition. Once all callers
have been updated to import directly from the sub-modules, this file can
be deleted.
"""

from __future__ import annotations

from fastapi import APIRouter

from registry.api.routers.admin_audit import AuditResponse, AuditRow, query_audit_log
from registry.api.routers.admin_audit import router as _audit_router
from registry.api.routers.admin_lifecycle import (
    LifecycleTransitionRequest,
    LifecycleTransitionResponse,
    lifecycle_router,
)
from registry.api.routers.admin_lifecycle import mutation_router as lifecycle_mutation_router
from registry.api.routers.admin_pii import (
    PiiFieldPolicyCreate,
    PiiFieldPolicyResponse,
    PiiPatternCreate,
    PiiPatternPatch,
    PiiPatternResponse,
    _delete_pii_field_policy,
    _delete_pii_pattern,
    _patch_pii_pattern,
    create_pii_field_policy,
    create_pii_pattern,
    list_pii_field_policies,
    list_pii_patterns,
    pii_field_policy_router,
    pii_pattern_router,
)
from registry.api.routers.admin_pii import router as _pii_router
from registry.api.routers.admin_rbac import (
    AssignRoleRequest,
    RoleResponse,
    assign_role,
    get_actor,
    get_tenant,
    list_actors,
    list_roles,
    remove_role,
)
from registry.api.routers.admin_rbac import router as _rbac_router
from registry.api.routers.admin_sync import (
    SupersededFactResponse,
    SyncRunResponse,
    SyncSourceCreate,
    SyncSourcePatch,
    SyncSourceResponse,
    TriggerResponse,
)
from registry.api.routers.admin_sync import mutation_router as _sync_mutation
from registry.api.routers.admin_sync import router as _sync_router
from registry.api.routers.admin_tokens import MintTokenRequest, MintTokenResponse
from registry.api.routers.admin_tokens import mutation_router as _tokens_mutation
from registry.api.routers.admin_tokens import router as _tokens_router
from registry.api.routers.admin_vocab import (
    CapabilityTypeSchemaCreate,
    CapabilityTypeSchemaPatch,
    CapabilityTypeSchemaResponse,
    VocabularyValueCreate,
    VocabularyValuePatch,
    VocabularyValueResponse,
    add_vocabulary_value,
    create_capability_type,
    delete_vocabulary_value,
    get_capability_type,
    list_capability_types,
    list_vocabulary_values,
    patch_capability_type,
    patch_vocabulary_value,
)
from registry.api.routers.admin_vocab import mutation_router as _vocab_mutation
from registry.api.routers.admin_vocab import router as _vocab_router

# LifecycleService is re-exported here so that unit tests that patch
# "catalog.api.routers.admin.LifecycleService" continue to work without
# change.  The name lives in admin_lifecycle.py; re-importing it here puts
# it in this module's namespace so mock.patch targets resolve correctly.
from registry.service.lifecycle import LifecycleService  # noqa: F401

# ---------------------------------------------------------------------------
# Aggregate "router" — the original admin.py had a single
#   router = APIRouter(prefix="/v1/admin")
# and main.py calls app.include_router(admin.router).
#
# Each sub-module router already carries prefix="/v1/admin".  We aggregate
# them under a no-prefix wrapper so the FastAPI route resolution produces
# /v1/admin/* unchanged.
# ---------------------------------------------------------------------------

router = APIRouter()
router.include_router(_tokens_router)
router.include_router(_sync_router)
router.include_router(_vocab_router)
router.include_router(_audit_router)
router.include_router(_rbac_router)
router.include_router(_pii_router)

# ---------------------------------------------------------------------------
# Aggregate "admin_mutation_router" — covers PATCH/DELETE for tokens, sync,
# vocab.  PII and lifecycle mutation routers keep their own names since
# main.py registers them individually.
# ---------------------------------------------------------------------------

admin_mutation_router = APIRouter()
admin_mutation_router.include_router(_tokens_mutation)
admin_mutation_router.include_router(_sync_mutation)
admin_mutation_router.include_router(_vocab_mutation)

__all__ = [
    # Routers
    "router",
    "admin_mutation_router",
    "lifecycle_mutation_router",
    "lifecycle_router",
    "pii_pattern_router",
    "pii_field_policy_router",
    # Service re-export for mock.patch compatibility
    "LifecycleService",
    # Token models
    "MintTokenRequest",
    "MintTokenResponse",
    # Sync models
    "SyncSourceCreate",
    "SyncSourcePatch",
    "SyncSourceResponse",
    "TriggerResponse",
    "SyncRunResponse",
    "SupersededFactResponse",
    # Vocab models + handlers
    "VocabularyValueResponse",
    "VocabularyValueCreate",
    "VocabularyValuePatch",
    "CapabilityTypeSchemaResponse",
    "CapabilityTypeSchemaCreate",
    "CapabilityTypeSchemaPatch",
    "list_vocabulary_values",
    "add_vocabulary_value",
    "patch_vocabulary_value",
    "delete_vocabulary_value",
    "list_capability_types",
    "create_capability_type",
    "get_capability_type",
    "patch_capability_type",
    # Audit models + handler
    "AuditRow",
    "AuditResponse",
    "query_audit_log",
    # RBAC models + handlers
    "RoleResponse",
    "AssignRoleRequest",
    "get_tenant",
    "get_actor",
    "list_actors",
    "list_roles",
    "assign_role",
    "remove_role",
    # PII models + handlers
    "PiiPatternCreate",
    "PiiPatternPatch",
    "PiiPatternResponse",
    "PiiFieldPolicyCreate",
    "PiiFieldPolicyResponse",
    "create_pii_pattern",
    "list_pii_patterns",
    "_patch_pii_pattern",
    "_delete_pii_pattern",
    "create_pii_field_policy",
    "list_pii_field_policies",
    "_delete_pii_field_policy",
    # Lifecycle models
    "LifecycleTransitionRequest",
    "LifecycleTransitionResponse",
]

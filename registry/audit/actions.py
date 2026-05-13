"""Canonical audit action name constants used across audit callsites."""

from typing import Final

__all__ = [
    "ANNOTATION_CREATED",
    "ANNOTATION_TRIAGED",
    "ANNOTATION_DELETED",
    "ENTITY_UPDATED",
    "ENTITY_DELETED",
    "ADOPTION_REVOKED",
    "ENTITY_VISIBILITY_SET",
    "EXTERNAL_ID_DELETED",
    "PROGRESSION_DEFINITION_PUBLISHED",
    "PROGRESSION_DEFINITION_SOFT_DELETED",
    "PROGRESSION_OVERRIDE_CREATED",
    "PROGRESSION_TRANSITION_ACCEPTED",
    "PROGRESSION_TRANSITION_REJECTED",
    "PROGRESSION_TRANSITION_WARNED",
    "PROGRESSION_TRANSITION_OVERRIDDEN",
    # Workspace actions
    "WORKSPACE_CREATED",
    "WORKSPACE_UPDATED",
    "WORKSPACE_DELETED",
    # Workspace entry actions
    "WORKSPACE_ENTRY_CREATED",
    "WORKSPACE_ENTRY_UPDATED",
    "WORKSPACE_ENTRY_DELETED",
    # Workspace share actions
    "WORKSPACE_SHARE_GRANTED",
    "WORKSPACE_SHARE_REVOKED",
    # Workspace expiry worker
    "WORKSPACE_ENTRY_EXPIRED",
    # Right-to-be-forgotten physical purge
    "RTBF_PURGE",
]

ANNOTATION_CREATED: Final[str]                  = "annotation.created"
ANNOTATION_TRIAGED: Final[str]                  = "annotation.triaged"
ANNOTATION_DELETED: Final[str]                  = "annotation.deleted"
ENTITY_UPDATED: Final[str]                      = "entity.updated"
ENTITY_DELETED: Final[str]                      = "entity.deleted"
ADOPTION_REVOKED: Final[str]                    = "adoption.revoked"
ENTITY_VISIBILITY_SET: Final[str]               = "entity.visibility_set"
EXTERNAL_ID_DELETED: Final[str]                 = "external_id.deleted"
PROGRESSION_DEFINITION_PUBLISHED: Final[str]    = "progression.definition.published"
PROGRESSION_DEFINITION_SOFT_DELETED: Final[str] = "progression.definition.soft_deleted"
PROGRESSION_OVERRIDE_CREATED: Final[str]        = "progression.override.created"
PROGRESSION_TRANSITION_ACCEPTED: Final[str]     = "progression.transition.accepted"
PROGRESSION_TRANSITION_REJECTED: Final[str]     = "progression.transition.rejected"
PROGRESSION_TRANSITION_WARNED: Final[str]       = "progression.transition.warned"
PROGRESSION_TRANSITION_OVERRIDDEN: Final[str]   = "progression.transition.overridden"

# Workspace lifecycle actions — used by WorkspaceService.
# Named noun.verb to match the registry-wide audit action convention.
WORKSPACE_CREATED: Final[str]                   = "workspace.created"
WORKSPACE_UPDATED: Final[str]                   = "workspace.updated"
WORKSPACE_DELETED: Final[str]                   = "workspace.deleted"

# Workspace entry lifecycle actions — used by WorkspaceService entry CRUD methods.
WORKSPACE_ENTRY_CREATED: Final[str]             = "workspace.entry.created"
WORKSPACE_ENTRY_UPDATED: Final[str]             = "workspace.entry.updated"
WORKSPACE_ENTRY_DELETED: Final[str]             = "workspace.entry.deleted"

# Workspace share lifecycle actions — used by WorkspaceService share methods.
WORKSPACE_SHARE_GRANTED: Final[str]             = "workspace.share.granted"
WORKSPACE_SHARE_REVOKED: Final[str]             = "workspace.share.revoked"

# Workspace expiry worker — emitted per batch of soft-invalidated entries.
# Tenant context is synthetic (system actor) because the worker spans all tenants.
WORKSPACE_ENTRY_EXPIRED: Final[str]             = "workspace.entry.expired"

# Right-to-be-forgotten physical purge. Cross-cutting concern; noun.verb taxonomy
# uses the operation's domain ("rtbf") rather than the service that executes it,
# because future phases may consolidate purge operations across content tables.
RTBF_PURGE: Final[str]                          = "rtbf.purge"

"""Shared dependencies and helpers for admin domain routers.

Every admin sub-module (tokens, sync, vocab, audit, rbac, pii, lifecycle)
imports from here rather than duplicating the role-guard one-liners.
Symbols promoted here must be referenced by at least two sub-modules;
single-use helpers live in their owning file.
"""

from __future__ import annotations

from registry.api.auth.context import ROLE_ADMIN, ROLE_AUDITOR, ROLE_PRODUCER, require_roles

# Dependency closures — assigned at module level so FastAPI's B008 lint rule
# (function calls in default arguments) is satisfied once, not per-file.
_admin_required = require_roles([ROLE_ADMIN])
_admin_or_producer_required = require_roles([ROLE_ADMIN, ROLE_PRODUCER])
_auditor_required = require_roles([ROLE_AUDITOR])

__all__ = [
    "_admin_required",
    "_admin_or_producer_required",
    "_auditor_required",
]

"""Base class, shared data types, and resolver factory for claim-source resolvers.

All resolver implementations (OIDC-derived, RSAM, and any future plug-in)
return the same `ResolvedIdentity` three-tuple. The `build_resolver` factory
selects the active resolver by inspecting `Settings.auth_mode` and delegates
to the matching `ClaimResolverBase` subclass.

Adding a new auth mode means:
1. Subclass `ClaimResolverBase` and implement `is_in_scope` + `resolve`.
2. Register the new subclass in `build_resolver` below.
3. Add the mode string to `Settings.auth_mode`'s documented allowed values.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from registry.config import Settings


# ---------------------------------------------------------------------------
# Data types shared across all resolver implementations


@dataclass(frozen=True)
class AuditIdentity:
    """Caller identity recorded in every audit log entry.

    `email` is optional: deployments backed by identity providers that do not
    surface email addresses (e.g. RSAM-only auth modes) record `None` explicitly
    so the gap is visible in the audit log rather than silently filled.

    `preferred_username` always falls back to `sub` when the identity provider
    does not supply a display name — it is therefore always non-None.
    """

    sub: str
    email: str | None
    preferred_username: str


@dataclass(frozen=True)
class TenantGrant:
    """One tenant-scoped role grant derived from a resolver's claims.

    `tenant_id` is the catalog-internal UUID (the row in the `tenants` table).
    `tenant_external_id` is the stable external identifier used by the
    originating identity system (e.g. the SEAL ID for RSAM deployments).
    `catalog_role` is one of the roles enumerated in `role_mappings`
    (admin | producer | auditor | viewer).
    """

    tenant_id: uuid.UUID
    tenant_external_id: str
    catalog_role: str


@dataclass
class ResolvedIdentity:
    """Output contract for every `ClaimResolverBase.resolve()` call.

    `user_id` is the stable opaque subject from the token (`sub` claim or
    equivalent). It is used as the key for per-subject caches and for
    actor-row lookups — it is NOT a UUID.

    `tenant_grants` is the full set of tenant-scoped roles the caller holds.
    An empty list means the caller is authenticated but holds no grants; the
    middleware translates this to HTTP 403 before any service code is reached.

    `audit_identity` carries the fields written to the audit log for every
    write operation. It is populated from the actors table where available
    and falls back to subject-only values when the actors table has not yet
    been seeded (actor JIT upsert is handled by a separate task).
    """

    user_id: str
    tenant_grants: list[TenantGrant] = field(default_factory=list)
    audit_identity: AuditIdentity | None = None


# ---------------------------------------------------------------------------
# Abstract base class


class ClaimResolverBase(ABC):
    """Abstract base for all claim-source resolver implementations.

    Concrete subclasses implement `is_in_scope` to declare which auth mode
    they handle, and `resolve` to convert a raw claims dict into a
    `ResolvedIdentity`. The factory calls `is_in_scope` on each registered
    resolver in priority order and delegates to the first match.

    Subclasses may be stateful (e.g. holding a session factory or a cache)
    but must be safe for concurrent async use — every request shares the same
    resolver instance.
    """

    @abstractmethod
    def is_in_scope(self, claims: dict[str, Any]) -> bool:
        """Return True if this resolver should handle the given claims dict.

        The factory calls this method to select the resolver; it must not
        perform I/O or raise. A resolver that depends on Settings should
        check `settings.auth_mode` here.
        """

    @abstractmethod
    async def resolve(self, claims: dict[str, Any]) -> ResolvedIdentity:
        """Convert raw token claims into a `ResolvedIdentity`.

        Implementations are responsible for:
        - Extracting the subject from `claims["sub"]` (or equivalent).
        - Fetching and parsing any external data needed to derive tenant grants.
        - Materialising JIT tenants and actors when required.
        - Returning a `ResolvedIdentity` with the caller's full grant set.

        On hard failures (upstream 5xx, missing required claim), raise rather
        than returning empty grants — the middleware translates exceptions to
        the appropriate HTTP error code.
        """


# ---------------------------------------------------------------------------
# Resolver factory
#
# `build_resolver` constructs the right `ClaimResolverBase` subclass for the
# current auth mode. Registration order matters when multiple resolvers could
# return `True` from `is_in_scope` for the same claims — the first match wins.
# Currently only one resolver is active at a time (mode is service-wide), so
# order is informational rather than a tie-breaker.


def build_resolver(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> ClaimResolverBase:
    """Return the claim-source resolver appropriate for the configured auth mode.

    Each resolver's `is_in_scope` is checked in registration order; the first
    resolver that claims scope is returned. Raises `ValueError` if no registered
    resolver matches — this indicates an unsupported `auth_mode` value that
    should have been caught at startup by `Settings.__post_init__`.
    """
    # Import here to avoid circular imports: claim_source imports from this
    # module, so top-level import would create a cycle.
    from registry.auth.entitlements.resolver import EntitlementResolver  # noqa: PLC0415

    registered: list[ClaimResolverBase] = [
        EntitlementResolver(settings=settings, session_factory=session_factory),
    ]

    dummy_claims: dict[str, Any] = {}
    for resolver in registered:
        if resolver.is_in_scope(dummy_claims):
            return resolver

    raise ValueError(
        f"No claim-source resolver registered for auth_mode={settings.auth_mode!r}. "
        "Add a ClaimResolverBase subclass and register it in build_resolver."
    )


__all__ = [
    "AuditIdentity",
    "ClaimResolverBase",
    "ResolvedIdentity",
    "TenantGrant",
    "build_resolver",
]

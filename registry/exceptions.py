"""Typed exception hierarchy for the registry service."""

from __future__ import annotations


class CatalogError(Exception):
    """Base for every catalog-domain error."""


class TenantIsolationError(CatalogError):
    """Raised when a request would read or write data outside its TenantContext."""


class VocabularyError(CatalogError):
    """Raised when a value violates a registered controlled vocabulary."""


class ValidationError(CatalogError):
    """Raised when input fails JSON Schema or capability-type validation."""


class LifecycleError(CatalogError):
    """Raised when a lifecycle-state transition violates the state machine."""


class NotFoundError(CatalogError):
    """Raised when a requested entity, fact, or edge does not exist for the tenant."""


class ConflictError(CatalogError):
    """Raised when an insert would violate a uniqueness constraint (e.g. duplicate external ID)."""

"""Connector ABC, DiscoveredArtifact, and ParsedFact.

Design invariants:
- ``parse()`` is a pure function — no I/O, no DB access.
- Credentials are resolved exclusively from ``os.environ``; they are never
  read from DB rows, config files, or any other source.
- ``resolve_credential`` raises ``CredentialError`` (a typed exception) when
  the referenced variable is absent, so callers get an actionable message.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from registry.storage.models import SyncSource


# ---------------------------------------------------------------------------
# Typed exception
# ---------------------------------------------------------------------------


class CredentialError(Exception):
    """Raised when a required credential environment variable is absent."""


# ---------------------------------------------------------------------------
# Credential helper — env-only, never DB or config
# ---------------------------------------------------------------------------


def resolve_credential(ref: str) -> str:
    """Return the value of ``os.environ[ref]``.

    Raises:
        CredentialError: if the variable is not set in the environment.
    """
    # Dynamic ref string per connector — see module header line 7.
    value = os.environ.get(ref)  # config: intentional
    if value is None:
        raise CredentialError(
            f"Credential environment variable '{ref}' is not set. "
            "Set it in the process environment before running the sync."
        )
    return value


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveredArtifact:
    """A single artifact discovered by a connector's ``discover()`` call.

    Immutable so it can be safely shared across async tasks.
    """

    artifact_id: str
    source_url: str
    artifact_type: str


@dataclass(frozen=True, slots=True)
class ParsedFact:
    """One fact extracted from a raw artifact by ``parse()``.

    ``valid_from`` and ``commit_sha`` are optional because not every
    artifact type carries temporal or VCS provenance.
    """

    entity_id: UUID
    category: str
    body: str
    valid_from: datetime | None
    source_url: str
    commit_sha: str | None


# ---------------------------------------------------------------------------
# Connector ABC
# ---------------------------------------------------------------------------


class Connector(ABC):
    """Abstract base class every connector implementation must satisfy.

    Lifecycle:
    1. ``validate(credentials_ref)`` — called once at source-activation time
       to confirm credentials exist and are accepted by the remote.
    2. ``discover(source)`` — enumerate all artifacts available for this
       source configuration.
    3. ``fetch(artifact, source)`` — download the raw bytes for one artifact.
    4. ``parse(artifact, raw)`` — extract ``ParsedFact`` objects from raw
       bytes.  MUST be pure: no network I/O, no DB access, no side effects.
    """

    @abstractmethod
    async def discover(
        self,
        source: SyncSource,
    ) -> list[DiscoveredArtifact]:
        """Return all artifacts available from *source*.

        Implementations may use ``source.config`` and, if required,
        ``resolve_credential(source.credentials_ref)`` for authentication.
        """

    @abstractmethod
    async def fetch(
        self,
        artifact: DiscoveredArtifact,
        source: SyncSource,
    ) -> bytes:
        """Download and return the raw content bytes for *artifact*."""

    @abstractmethod
    def parse(
        self,
        artifact: DiscoveredArtifact,
        raw: bytes,
    ) -> list[ParsedFact]:
        """Extract ``ParsedFact`` objects from *raw* bytes.

        This method MUST be pure:
        - No network calls.
        - No database access.
        - No file-system writes.
        - No global mutable state.

        Calling it twice with the same arguments must return equivalent
        results.
        """

    @abstractmethod
    async def validate(self, credentials_ref: str | None) -> None:
        """Confirm that credentials exist and are accepted by the remote.

        If *credentials_ref* is ``None`` the connector does not require
        credentials and should simply return.  Otherwise call
        ``resolve_credential(credentials_ref)`` to obtain the token and
        probe the remote endpoint.

        Raises:
            CredentialError: if the env variable is absent.
            Exception: any transport error from the remote.
        """

"""Connector registry — single authoritative mapping of source_type to Connector class.

Usage
-----
    from sync.registry import get_connector

    ConnectorClass = get_connector(source.source_type)
    connector = ConnectorClass()

The five keys defined here MUST match the ``source_type`` controlled-vocabulary
values exactly (vocabulary kind ``source_type``).  As each concrete connector
implementation lands, replace the corresponding ``_StubConnector`` entry with
the real class via an import.

Adding a new connector
----------------------
1. Implement ``MyConnector(Connector)`` in ``sync/connectors/my_type.py``.
2. Import it here.
3. Replace the ``_StubConnector`` entry (or add a new key) in ``CONNECTORS``.
4. Ship the connector task commit.

Do NOT import connector implementation modules that don't exist yet; use
``_StubConnector`` as the placeholder until each task completes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sync.connector import Connector, DiscoveredArtifact, ParsedFact
from sync.connectors.docs_corpus import DocsCorpusConnector
from sync.connectors.markdown_adr_rfc import MarkdownADRRFCConnector
from sync.connectors.openapi import OpenAPIConnector
from sync.connectors.package_json import PackageJsonConnector
from sync.connectors.release_notes import ReleaseNotesConnector

if TYPE_CHECKING:
    from registry.storage.models import SyncSource


# ---------------------------------------------------------------------------
# Typed exception
# ---------------------------------------------------------------------------


class UnknownConnectorError(Exception):
    """Raised when no connector is registered for a given source_type."""


# ---------------------------------------------------------------------------
# Stub — placeholder until each connector implementation lands
# ---------------------------------------------------------------------------


class _StubConnector(Connector):
    """Not-yet-implemented connector placeholder.

    Raises ``NotImplementedError`` for all methods so any accidental use
    in tests or the runner surfaces a clear, actionable error rather than
    silently doing nothing.
    """

    async def discover(self, source: SyncSource) -> list[DiscoveredArtifact]:
        raise NotImplementedError(
            f"{type(self).__name__}.discover() is not yet implemented. "
            "Replace this stub in sync/registry.py once the connector lands."
        )

    async def fetch(self, artifact: DiscoveredArtifact, source: SyncSource) -> bytes:
        raise NotImplementedError(f"{type(self).__name__}.fetch() is not yet implemented.")

    def parse(self, artifact: DiscoveredArtifact, raw: bytes) -> list[ParsedFact]:
        raise NotImplementedError(f"{type(self).__name__}.parse() is not yet implemented.")

    async def validate(self, credentials_ref: str | None) -> None:
        raise NotImplementedError(f"{type(self).__name__}.validate() is not yet implemented.")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CONNECTORS: dict[str, type[Connector]] = {
    "openapi": OpenAPIConnector,
    "release_notes": ReleaseNotesConnector,
    "markdown_adr_rfc": MarkdownADRRFCConnector,
    "package_json": PackageJsonConnector,
    "docs_corpus": DocsCorpusConnector,
}


def get_connector(source_type: str) -> type[Connector]:
    """Return the ``Connector`` subclass registered for *source_type*.

    Args:
        source_type: Must match one of the five controlled-vocabulary values.

    Returns:
        The ``Connector`` subclass (not an instance).

    Raises:
        UnknownConnectorError: if *source_type* is not in ``CONNECTORS``.
    """
    try:
        return CONNECTORS[source_type]
    except KeyError:
        known = ", ".join(sorted(CONNECTORS))
        raise UnknownConnectorError(
            f"No connector registered for source_type={source_type!r}. " f"Known types: {known}."
        ) from None

"""Docs corpus connector.

Discovers ``AGENTS.md`` and all ``docs/**/*.md`` files from a GitHub
repository (excluding ``docs/adr/`` and ``docs/rfc/`` which are handled
by the ADR/RFC connector), fetches their raw content, and parses each
into a single ``ParsedFact`` with ``category='dev_doc'``.

The ``source.config`` dict must contain:
    ``owner``  — GitHub organisation/user  (str)
    ``repo``   — repository name            (str)
    ``ref``    — branch/tag/SHA             (str)

``source.credentials_ref`` must name an env-var holding a GitHub PAT.
If ``None``, unauthenticated requests are made (rate-limited to 60/h).

Design decisions:
- ``discover()`` uses the GitHub Git Tree API with ``?recursive=1`` to
  enumerate all blobs.  Matching rules:
    * ``AGENTS.md`` (exact root-level file)
    * ``docs/**/*.md`` EXCEPT paths under ``docs/adr/`` or ``docs/rfc/``
      (those are owned by the MarkdownADRRFCConnector).
- ``fetch()`` GETs the raw blob via
  ``https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}``.
- ``parse()`` is pure: decodes UTF-8 markdown, extracts a heading hierarchy
  summary from ``#``-prefixed lines and stores the full content as the
  body.  ``entity_id`` is derived deterministically from
  ``{owner}/{repo}::{path}`` via ``uuid.uuid5``.
- ``validate()`` pings ``GET /user`` on the GitHub API.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from sync.connector import (
    Connector,
    DiscoveredArtifact,
    ParsedFact,
    resolve_credential,
)

if TYPE_CHECKING:
    from registry.storage.models import SyncSource


# ---------------------------------------------------------------------------
# Fixed namespace for deterministic entity_id derivation.
# ---------------------------------------------------------------------------

_DOCS_NS = uuid.UUID("5e6f7a8b-9c0d-1e2f-3a4b-5c6d7e8f9a0b")

_GITHUB_API = "https://api.github.com"
_GITHUB_RAW = "https://raw.githubusercontent.com"

# Paths excluded from this connector (handled by MarkdownADRRFCConnector).
_EXCLUDED_PREFIXES = ("docs/adr/", "docs/rfc/")

# Heading lines — one or more ``#`` chars followed by a space and text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def _is_docs_corpus_path(path: str) -> bool:
    """Return True if *path* should be ingested by this connector."""
    if path == "AGENTS.md":
        return True
    if path.startswith("docs/") and path.endswith(".md"):
        for excluded in _EXCLUDED_PREFIXES:
            if path.startswith(excluded):
                return False
        return True
    return False


class DocsCorpusConnector(Connector):
    """GitHub-backed docs corpus connector."""

    # ------------------------------------------------------------------
    # discover
    # ------------------------------------------------------------------

    async def discover(self, source: SyncSource) -> list[DiscoveredArtifact]:
        """Walk the GitHub tree for AGENTS.md and docs/**/*.md.

        Excludes ``docs/adr/`` and ``docs/rfc/`` paths.
        """
        config: dict[str, Any] = source.config
        owner: str = config["owner"]
        repo: str = config["repo"]
        ref: str = config["ref"]

        token: str | None = None
        if source.credentials_ref:
            token = resolve_credential(source.credentials_ref)

        headers = _github_headers(token)
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/git/trees/{ref}"
        params = {"recursive": "1"}

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()

        tree_data: dict[str, Any] = resp.json()
        tree: list[dict[str, Any]] = tree_data.get("tree", [])

        artifacts: list[DiscoveredArtifact] = []
        for entry in tree:
            if entry.get("type") != "blob":
                continue
            path: str = entry.get("path", "")
            if not _is_docs_corpus_path(path):
                continue

            raw_url = f"{_GITHUB_RAW}/{owner}/{repo}/{ref}/{path}"
            artifacts.append(
                DiscoveredArtifact(
                    artifact_id=path,
                    source_url=raw_url,
                    artifact_type="docs_corpus",
                )
            )

        return artifacts

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------

    async def fetch(
        self,
        artifact: DiscoveredArtifact,
        source: SyncSource,
    ) -> bytes:
        """Download raw markdown bytes from the GitHub raw URL."""
        token: str | None = None
        if source.credentials_ref:
            token = resolve_credential(source.credentials_ref)

        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient() as client:
            resp = await client.get(artifact.source_url, headers=headers)
            resp.raise_for_status()
            return resp.content

    # ------------------------------------------------------------------
    # parse (pure)
    # ------------------------------------------------------------------

    def parse(
        self,
        artifact: DiscoveredArtifact,
        raw: bytes,
    ) -> list[ParsedFact]:
        """Extract one ``ParsedFact`` (category='dev_doc') from a markdown file.

        The body is the full UTF-8 decoded content.  A heading-hierarchy
        summary is prepended so the text is more useful for embedding and
        search without lossy trimming of the original content.
        """
        path = artifact.artifact_id
        text = raw.decode("utf-8")

        # Build a heading hierarchy prefix for search quality.
        headings = _HEADING_RE.findall(text)
        prefix_parts: list[str] = []
        if headings:
            outline = ", ".join(
                f"{'  ' * (len(hashes) - 1)}{title.strip()}"
                for hashes, title in headings[:10]  # cap to avoid huge prefixes
            )
            prefix_parts.append(f"headings: {outline}")

        prefix_parts.append(f"path: {path}")
        body = "\n".join(prefix_parts) + "\n\n" + text

        # Derive owner/repo from source_url.
        # URL: https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}
        url_parts = artifact.source_url.split("/")
        owner_repo = ""
        if "raw.githubusercontent.com" in artifact.source_url and len(url_parts) >= 6:
            owner_repo = f"{url_parts[4]}/{url_parts[5]}"

        key = f"{owner_repo}::{path}" if owner_repo else path
        entity_id = uuid.uuid5(_DOCS_NS, key)

        return [
            ParsedFact(
                entity_id=entity_id,
                category="dev_doc",
                body=body.strip(),
                valid_from=datetime.now(UTC),
                source_url=artifact.source_url,
                commit_sha=None,
            )
        ]

    # ------------------------------------------------------------------
    # validate
    # ------------------------------------------------------------------

    async def validate(self, credentials_ref: str | None) -> None:
        """Probe ``GET /user`` to confirm credentials are accepted."""
        if credentials_ref is None:
            return
        token = resolve_credential(credentials_ref)
        headers = _github_headers(token)
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{_GITHUB_API}/user", headers=headers)
            resp.raise_for_status()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _github_headers(token: str | None) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

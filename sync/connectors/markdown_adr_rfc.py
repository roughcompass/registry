"""Markdown ADR / RFC connector.

Discovers ``docs/adr/*.md`` and ``docs/rfc/*.md`` in a GitHub repository
via the Git Tree API, fetches each blob's raw content, and parses it into
a single ``ParsedFact`` with ``category='adr'`` or ``category='rfc'``
inferred from the file path.

The ``source.config`` dict must contain:
    ``owner``  — GitHub organisation/user  (str)
    ``repo``   — repository name            (str)
    ``ref``    — branch/tag/SHA             (str)

``source.credentials_ref`` must name an env-var holding a GitHub PAT.
If ``None``, unauthenticated requests are made.

Design decisions:
- ``discover()`` uses ``GET /repos/:owner/:repo/git/trees/:ref?recursive=1``
  to find all tree blobs, then filters to ``docs/adr/*.md`` and
  ``docs/rfc/*.md``.
- ``fetch()`` GETs the raw blob via
  ``https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}``.
- ``parse()`` is pure: it reads optional YAML frontmatter delimited by
  ``---`` fences, extracts ``title`` and ``status`` from it (if present),
  uses the remaining text as the body.  ``entity_id`` is derived
  deterministically from ``{owner}/{repo}::{path}`` via ``uuid.uuid5``.
- ``validate()`` pings ``GET /user`` on the GitHub API.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import yaml

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

_MARKDOWN_NS = uuid.UUID("2b3c4d5e-6f7a-8b9c-0d1e-2f3a4b5c6d7e")

_GITHUB_API = "https://api.github.com"
_GITHUB_RAW = "https://raw.githubusercontent.com"

# Regex that matches the two target directory prefixes.
_ADR_PATTERN = re.compile(r"^docs/adr/[^/]+\.md$")
_RFC_PATTERN = re.compile(r"^docs/rfc/[^/]+\.md$")

# YAML frontmatter fence pattern.
_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\n---\r?\n", re.DOTALL)


class MarkdownADRRFCConnector(Connector):
    """GitHub-backed Markdown ADR / RFC connector."""

    # ------------------------------------------------------------------
    # discover
    # ------------------------------------------------------------------

    async def discover(self, source: SyncSource) -> list[DiscoveredArtifact]:
        """Walk the GitHub tree for ``docs/adr/*.md`` and ``docs/rfc/*.md``."""
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
            if not (_ADR_PATTERN.match(path) or _RFC_PATTERN.match(path)):
                continue

            raw_url = f"{_GITHUB_RAW}/{owner}/{repo}/{ref}/{path}"
            # Embed owner/repo/ref in source_url for use in parse().
            # Format: raw GitHub URL (parse() can extract path from URL).
            artifacts.append(
                DiscoveredArtifact(
                    artifact_id=path,
                    source_url=raw_url,
                    artifact_type="markdown_adr_rfc",
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
        """Fetch raw markdown bytes from the GitHub raw URL."""
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
        """Extract one ``ParsedFact`` from markdown content.

        Category is inferred from the file path: ``docs/adr/`` → ``'adr'``,
        ``docs/rfc/`` → ``'rfc'``.  YAML frontmatter is stripped before
        the body is stored.
        """
        path = artifact.artifact_id

        # Infer category.
        if path.startswith("docs/adr/"):
            category = "adr"
        elif path.startswith("docs/rfc/"):
            category = "rfc"
        else:
            category = "markdown"

        text = raw.decode("utf-8")

        # Strip YAML frontmatter if present, extracting title/status.
        fm_title: str | None = None
        fm_status: str | None = None
        match = _FRONTMATTER_RE.match(text)
        if match:
            fm_raw = match.group(1)
            try:
                fm: dict[str, Any] = yaml.safe_load(fm_raw) or {}
                fm_title = fm.get("title") or fm.get("Title")
                fm_status = fm.get("status") or fm.get("Status")
            except yaml.YAMLError:
                pass
            body = text[match.end() :]
        else:
            body = text

        # Prepend frontmatter summary to body for searchability.
        prefix_parts: list[str] = []
        if fm_title:
            prefix_parts.append(f"title: {fm_title}")
        if fm_status:
            prefix_parts.append(f"status: {fm_status}")
        if prefix_parts:
            body = "\n".join(prefix_parts) + "\n\n" + body

        # Derive owner/repo/ref from source_url for entity_id key.
        # URL: https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}
        url_parts = artifact.source_url.split("/")
        # raw.githubusercontent.com/{owner}/{repo}/{ref}/...
        # indices:                   0      1      2      3  4   5   6+
        owner_repo = ""
        if "raw.githubusercontent.com" in artifact.source_url and len(url_parts) >= 6:
            owner_repo = f"{url_parts[4]}/{url_parts[5]}"

        key = f"{owner_repo}::{path}" if owner_repo else path
        entity_id = uuid.uuid5(_MARKDOWN_NS, key)

        return [
            ParsedFact(
                entity_id=entity_id,
                category=category,
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

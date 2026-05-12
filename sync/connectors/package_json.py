"""package.json connector.

Discovers ``package.json`` files from a GitHub repository — the root and the
monorepo conventions ``apps/*/package.json`` and ``packages/*/package.json`` —
fetches their raw content, and parses each one into a single ``ParsedFact``
with ``category='package_manifest'`` summarising the package name, version,
and dependency lists.

The ``source.config`` dict must contain:
    ``owner``  — GitHub organisation/user  (str)
    ``repo``   — repository name            (str)
    ``ref``    — branch/tag/SHA             (str)

``source.credentials_ref`` must name an env-var holding a GitHub PAT.
If ``None``, unauthenticated requests are made (rate-limited to 60/h).

Design decisions:
- ``discover()`` queries the GitHub Git Tree API with ``?recursive=1`` to
  enumerate all blobs, then filters to the three target path patterns:
  ``package.json``, ``apps/*/package.json``, and
  ``packages/*/package.json``.  Using the tree API avoids multiple Contents
  API calls for each ``apps/`` and ``packages/`` subdirectory.
- ``fetch()`` GETs the raw blob via
  ``https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}``.
- ``parse()`` is pure: decodes JSON, extracts ``name``, ``version``,
  ``dependencies``, and ``devDependencies``, serialises a human-readable
  body string.  ``entity_id`` is derived deterministically from
  ``{owner}/{repo}::{path}::{name}`` via ``uuid.uuid5``.
- ``validate()`` pings ``GET /user`` on the GitHub API.
"""

from __future__ import annotations

import json
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

_PACKAGE_NS = uuid.UUID("4d5e6f7a-8b9c-0d1e-2f3a-4b5c6d7e8f9a")

_GITHUB_API = "https://api.github.com"
_GITHUB_RAW = "https://raw.githubusercontent.com"

# Pattern matching the three allowed package.json paths.
_PACKAGE_JSON_RE = re.compile(r"^(?:package\.json|apps/[^/]+/package\.json|packages/[^/]+/package\.json)$")


class PackageJsonConnector(Connector):
    """GitHub-backed package.json connector."""

    # ------------------------------------------------------------------
    # discover
    # ------------------------------------------------------------------

    async def discover(self, source: SyncSource) -> list[DiscoveredArtifact]:
        """Find ``package.json`` in root, ``apps/*/``, and ``packages/*/``.

        Uses the GitHub Git Tree API with ``?recursive=1``.
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
            if not _PACKAGE_JSON_RE.match(path):
                continue

            raw_url = f"{_GITHUB_RAW}/{owner}/{repo}/{ref}/{path}"
            artifacts.append(
                DiscoveredArtifact(
                    artifact_id=path,
                    source_url=raw_url,
                    artifact_type="package_json",
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
        """Download raw package.json bytes from the GitHub raw URL."""
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
        """Extract one ``ParsedFact`` (category='package_manifest') from a package.json.

        The body contains a human-readable summary of name, version, and
        both dependency maps.
        """
        data: dict[str, Any] = json.loads(raw)

        name: str = data.get("name", artifact.artifact_id)
        version: str = data.get("version", "")
        deps: dict[str, str] = data.get("dependencies", {})
        dev_deps: dict[str, str] = data.get("devDependencies", {})

        body_parts: list[str] = [f"name: {name}"]
        if version:
            body_parts.append(f"version: {version}")
        if deps:
            dep_lines = "\n".join(f"  {k}: {v}" for k, v in sorted(deps.items()))
            body_parts.append(f"dependencies:\n{dep_lines}")
        if dev_deps:
            dev_lines = "\n".join(f"  {k}: {v}" for k, v in sorted(dev_deps.items()))
            body_parts.append(f"devDependencies:\n{dev_lines}")

        body = "\n".join(body_parts)

        # Derive owner/repo from source_url.
        # URL: https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}
        url_parts = artifact.source_url.split("/")
        owner_repo = ""
        if "raw.githubusercontent.com" in artifact.source_url and len(url_parts) >= 6:
            owner_repo = f"{url_parts[4]}/{url_parts[5]}"

        key = f"{owner_repo}::{artifact.artifact_id}::{name}" if owner_repo else f"{artifact.artifact_id}::{name}"
        entity_id = uuid.uuid5(_PACKAGE_NS, key)

        return [
            ParsedFact(
                entity_id=entity_id,
                category="package_manifest",
                body=body,
                valid_from=datetime.now(UTC),
                source_url=artifact.source_url,
                commit_sha=None,
            )
        ]

    # ------------------------------------------------------------------
    # validate
    # ------------------------------------------------------------------

    async def validate(self, credentials_ref: str | None) -> None:
        """Probe ``GET /user`` to confirm credentials are valid."""
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

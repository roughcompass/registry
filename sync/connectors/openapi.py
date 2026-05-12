"""OpenAPI / AsyncAPI connector.

Discovers ``*.openapi.yaml``, ``*.asyncapi.yaml`` (and JSON equivalents)
from a GitHub repository's root and ``api/`` directory, fetches the raw
blob, and parses the spec into a single ``ParsedFact`` with
``category='api_doc'``.

Design decisions:
- ``discover()`` queries the GitHub Contents API for root + ``api/`` dir
  and filters by file name suffix.
- ``fetch()`` GETs the ``download_url`` from the discovered artifact's
  ``source_url``.
- ``parse()`` is pure: parses YAML/JSON, extracts ``title``,
  ``info.description``, and ``paths`` summaries, serialises to a body
  string.  ``entity_id`` is derived deterministically from
  ``{owner}/{repo}::{path}`` via ``uuid.uuid5``.
- ``validate()`` performs a ``GET /user`` against the GitHub API.

The ``source.config`` dict must contain:
    ``owner``  — GitHub organisation/user  (str)
    ``repo``   — repository name            (str)
    ``ref``    — branch/tag/SHA             (str)

``source.credentials_ref`` must name an env-var holding a GitHub PAT.
If ``None``, unauthenticated requests are made (rate-limited to 60/h).
"""

from __future__ import annotations

import json
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
# Namespace UUID — fixed so entity_id derivation is stable across runs.
# ---------------------------------------------------------------------------

_OPENAPI_NS = uuid.UUID("7c9d1e2f-3a4b-5c6d-7e8f-9a0b1c2d3e4f")

# Suffixes that identify OpenAPI / AsyncAPI spec files.
_SPEC_SUFFIXES = (
    ".openapi.yaml",
    ".openapi.json",
    ".asyncapi.yaml",
    ".asyncapi.json",
)

_GITHUB_API = "https://api.github.com"
_GITHUB_RAW = "https://raw.githubusercontent.com"


class OpenAPIConnector(Connector):
    """GitHub-backed OpenAPI / AsyncAPI connector."""

    # ------------------------------------------------------------------
    # discover
    # ------------------------------------------------------------------

    async def discover(self, source: SyncSource) -> list[DiscoveredArtifact]:
        """List ``*.openapi.yaml`` / ``*.asyncapi.yaml`` files in root and ``api/``.

        Uses the GitHub Contents API.
        """
        config: dict[str, Any] = source.config
        owner: str = config["owner"]
        repo: str = config["repo"]
        ref: str = config["ref"]

        token: str | None = None
        if source.credentials_ref:
            token = resolve_credential(source.credentials_ref)

        headers = _github_headers(token)

        artifacts: list[DiscoveredArtifact] = []

        async with httpx.AsyncClient() as client:
            for dir_path in ("", "api/"):
                url = f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{dir_path}"
                params = {"ref": ref}
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code == 404:
                    # Directory does not exist — skip silently.
                    continue
                resp.raise_for_status()
                entries: list[dict[str, Any]] = resp.json()
                for entry in entries:
                    if entry.get("type") != "file":
                        continue
                    name: str = entry.get("name", "")
                    if not any(name.endswith(s) for s in _SPEC_SUFFIXES):
                        continue
                    artifacts.append(
                        DiscoveredArtifact(
                            artifact_id=entry["path"],
                            source_url=entry["download_url"],
                            artifact_type="openapi",
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
        """Download raw spec bytes from the GitHub raw URL."""
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
        """Extract one ``ParsedFact`` (category='api_doc') from the spec bytes."""
        # Determine format from artifact_id extension.
        aid = artifact.artifact_id
        if aid.endswith(".json"):
            data: dict[str, Any] = json.loads(raw)
        else:
            data = yaml.safe_load(raw)

        info: dict[str, Any] = data.get("info", {})
        title: str = info.get("title", aid)
        description: str = info.get("description", "")
        version: str = info.get("version", "")

        # Summarise paths.
        paths: dict[str, Any] = data.get("paths", {})
        path_summaries: list[str] = []
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, op in methods.items():
                if method.startswith("x-") or not isinstance(op, dict):
                    continue
                summary = op.get("summary", "")
                op_id = op.get("operationId", "")
                path_summaries.append(
                    f"{method.upper()} {path}" + (f" — {summary}" if summary else "") + (f" [{op_id}]" if op_id else "")
                )

        body_parts = [f"title: {title}"]
        if version:
            body_parts.append(f"version: {version}")
        if description:
            body_parts.append(f"description: {description}")
        if path_summaries:
            body_parts.append("paths:\n" + "\n".join(f"  {p}" for p in path_summaries))

        body = "\n".join(body_parts)

        # Stable entity_id derived from source URL + path.
        key = f"{artifact.source_url}::{artifact.artifact_id}"
        entity_id = uuid.uuid5(_OPENAPI_NS, key)

        # Determine commit_sha from source_url components (raw GitHub URL encodes ref).
        commit_sha: str | None = None
        parts = artifact.source_url.split("/")
        # raw GitHub URL pattern: raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}
        if "raw.githubusercontent.com" in artifact.source_url and len(parts) >= 7:
            commit_sha = parts[6]

        return [
            ParsedFact(
                entity_id=entity_id,
                category="api_doc",
                body=body,
                valid_from=datetime.now(UTC),
                source_url=artifact.source_url,
                commit_sha=commit_sha,
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

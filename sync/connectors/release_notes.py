"""GitHub Releases connector.

Discovers releases via the GitHub Releases API, fetches each release's
body as bytes, and parses it into one ``ParsedFact`` with
``category='release_note'`` and ``valid_from`` set from ``published_at``.

The ``source.config`` dict must contain:
    ``owner``  — GitHub organisation/user  (str)
    ``repo``   — repository name            (str)

Optional config:
    ``per_page``  — releases per page (int, default 20, max 100)

``source.credentials_ref`` must name an env-var holding a GitHub PAT.
If ``None``, unauthenticated requests are made.

Design decisions:
- ``discover()`` calls ``GET /repos/:owner/:repo/releases?per_page=N&page=1``
  and returns one ``DiscoveredArtifact`` per release entry.  The full
  release JSON is serialised as the ``source_url`` payload so ``fetch()``
  can reconstruct it without another network call.  The ``source_url``
  field carries the GitHub API release URL for traceability; the raw bytes
  fetched in ``fetch()`` are the release body text encoded as UTF-8.
- ``entity_id`` is derived deterministically from
  ``{owner}/{repo}::{tag_name}`` via ``uuid.uuid5``.
- ``parse()`` is pure and decodes the UTF-8 body plus JSON metadata
  embedded in ``artifact.source_url`` as a ``data:`` URI.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote

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

_RELEASE_NS = uuid.UUID("1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d")

_GITHUB_API = "https://api.github.com"

# Separator used to embed release metadata in the source_url field.
# Format: RELEASE_META_PREFIX + percent-encoded JSON
_RELEASE_META_PREFIX = "data:release-meta;"


class ReleaseNotesConnector(Connector):
    """GitHub-backed release notes connector."""

    # ------------------------------------------------------------------
    # discover
    # ------------------------------------------------------------------

    async def discover(self, source: SyncSource) -> list[DiscoveredArtifact]:
        """List releases via the GitHub Releases API.

        Returns one ``DiscoveredArtifact`` per release.  The ``source_url``
        embeds the release JSON metadata so ``fetch()`` has no extra
        network round-trip to decode it.
        """
        config: dict[str, Any] = source.config
        owner: str = config["owner"]
        repo: str = config["repo"]
        per_page: int = int(config.get("per_page", 20))

        token: str | None = None
        if source.credentials_ref:
            token = resolve_credential(source.credentials_ref)

        headers = _github_headers(token)

        url = f"{_GITHUB_API}/repos/{owner}/{repo}/releases"
        params = {"per_page": per_page, "page": 1}

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()

        releases: list[dict[str, Any]] = resp.json()

        artifacts: list[DiscoveredArtifact] = []
        for release in releases:
            tag: str = release["tag_name"]
            # Embed metadata so fetch() can reconstruct the body without I/O.
            meta = {
                "tag_name": tag,
                "name": release.get("name", tag),
                "published_at": release.get("published_at"),
                "body": release.get("body") or "",
                "owner": owner,
                "repo": repo,
            }
            encoded_meta = quote(json.dumps(meta))
            source_url = f"{_RELEASE_META_PREFIX}{encoded_meta}"
            artifacts.append(
                DiscoveredArtifact(
                    artifact_id=tag,
                    source_url=source_url,
                    artifact_type="release_note",
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
        """Return the release body text as UTF-8 bytes.

        The metadata embedded in ``artifact.source_url`` is sufficient —
        no additional HTTP request is made.
        """
        meta = _decode_meta(artifact.source_url)
        body_text: str = meta.get("body", "")
        return body_text.encode("utf-8")

    # ------------------------------------------------------------------
    # parse (pure)
    # ------------------------------------------------------------------

    def parse(
        self,
        artifact: DiscoveredArtifact,
        raw: bytes,
    ) -> list[ParsedFact]:
        """Produce one ``ParsedFact`` (category='release_note') per release."""
        meta = _decode_meta(artifact.source_url)

        owner: str = meta["owner"]
        repo: str = meta["repo"]
        tag: str = meta["tag_name"]

        # Stable entity_id.
        key = f"{owner}/{repo}::{tag}"
        entity_id = uuid.uuid5(_RELEASE_NS, key)

        # Parse publication timestamp.
        valid_from: datetime | None = None
        published_at: str | None = meta.get("published_at")
        if published_at:
            try:
                valid_from = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            except ValueError:
                valid_from = None

        body = raw.decode("utf-8")
        release_name: str = meta.get("name", tag)
        if release_name and release_name != body:
            body = f"# {release_name}\n\n{body}"

        return [
            ParsedFact(
                entity_id=entity_id,
                category="release_note",
                body=body,
                valid_from=valid_from,
                source_url=artifact.source_url,
                commit_sha=None,
            )
        ]

    # ------------------------------------------------------------------
    # validate
    # ------------------------------------------------------------------

    async def validate(self, credentials_ref: str | None) -> None:
        """Probe the GitHub API to confirm credentials are accepted."""
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


def _decode_meta(source_url: str) -> dict[str, Any]:
    """Decode the release metadata embedded in ``source_url``."""
    if not source_url.startswith(_RELEASE_META_PREFIX):
        raise ValueError(f"source_url does not contain embedded release metadata: {source_url!r}")
    encoded = source_url[len(_RELEASE_META_PREFIX) :]
    return json.loads(unquote(encoded))  # type: ignore[no-any-return]

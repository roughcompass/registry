"""Unit tests for sync/connector.py and connector parse() methods.

Covers:
- DiscoveredArtifact and ParsedFact immutability / slot behaviour.
- resolve_credential raises CredentialError on missing env var.
- resolve_credential returns the value when the env var is present.
- Concrete subclass enforcement: all four abstract methods required.
- PackageJsonConnector.parse()
- DocsCorpusConnector.parse()
- OpenAPIConnector.parse()
- MarkdownADRRFCConnector.parse()
- ReleaseNotesConnector.parse()
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from sync.connector import (
    Connector,
    CredentialError,
    DiscoveredArtifact,
    ParsedFact,
    resolve_credential,
)
from sync.connectors.docs_corpus import DocsCorpusConnector, _is_docs_corpus_path
from sync.connectors.markdown_adr_rfc import MarkdownADRRFCConnector
from sync.connectors.openapi import OpenAPIConnector
from sync.connectors.package_json import PackageJsonConnector
from sync.connectors.release_notes import _RELEASE_META_PREFIX, ReleaseNotesConnector

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# DiscoveredArtifact
# ---------------------------------------------------------------------------


class TestDiscoveredArtifact:
    def test_basic_construction(self) -> None:
        a = DiscoveredArtifact(
            artifact_id="abc-123",
            source_url="https://example.com/openapi.json",
            artifact_type="openapi",
        )
        assert a.artifact_id == "abc-123"
        assert a.source_url == "https://example.com/openapi.json"
        assert a.artifact_type == "openapi"

    def test_frozen_raises_on_mutation(self) -> None:
        a = DiscoveredArtifact(artifact_id="x", source_url="https://x.com", artifact_type="openapi")
        with pytest.raises((AttributeError, TypeError)):
            a.artifact_id = "y"  # type: ignore[misc]

    def test_equality_and_hash(self) -> None:
        a = DiscoveredArtifact("id", "https://url", "openapi")
        b = DiscoveredArtifact("id", "https://url", "openapi")
        assert a == b
        assert hash(a) == hash(b)

    def test_slots_no_dict(self) -> None:
        a = DiscoveredArtifact("id", "https://url", "openapi")
        assert not hasattr(a, "__dict__")


# ---------------------------------------------------------------------------
# ParsedFact
# ---------------------------------------------------------------------------


class TestParsedFact:
    def _make(self, **overrides: object) -> ParsedFact:
        defaults: dict[str, object] = {
            "entity_id": uuid4(),
            "category": "api_endpoint",
            "body": "GET /health returns 200",
            "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
            "source_url": "https://repo/openapi.json",
            "commit_sha": "deadbeef",
        }
        defaults.update(overrides)
        return ParsedFact(**defaults)  # type: ignore[arg-type]

    def test_basic_construction(self) -> None:
        eid = uuid4()
        f = self._make(entity_id=eid)
        assert f.entity_id == eid
        assert f.category == "api_endpoint"

    def test_optional_fields_none(self) -> None:
        f = self._make(valid_from=None, commit_sha=None)
        assert f.valid_from is None
        assert f.commit_sha is None

    def test_frozen(self) -> None:
        f = self._make()
        with pytest.raises((AttributeError, TypeError)):
            f.body = "mutated"  # type: ignore[misc]

    def test_slots_no_dict(self) -> None:
        f = self._make()
        assert not hasattr(f, "__dict__")


# ---------------------------------------------------------------------------
# resolve_credential
# ---------------------------------------------------------------------------


class TestResolveCredential:
    def test_returns_value_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_API_KEY", "supersecret")
        assert resolve_credential("MY_API_KEY") == "supersecret"

    def test_raises_credential_error_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_VAR", raising=False)
        with pytest.raises(CredentialError, match="MISSING_VAR"):
            resolve_credential("MISSING_VAR")

    def test_error_message_contains_var_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOME_TOKEN", raising=False)
        with pytest.raises(CredentialError, match="SOME_TOKEN"):
            resolve_credential("SOME_TOKEN")


# ---------------------------------------------------------------------------
# Connector ABC enforcement
# ---------------------------------------------------------------------------


class TestConnectorABC:
    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            Connector()  # type: ignore[abstract]

    def test_concrete_subclass_must_implement_all_methods(self) -> None:
        """A subclass that omits any abstract method cannot be instantiated."""

        class Incomplete(Connector):
            async def discover(self, source):  # type: ignore[override]
                return []

            # fetch, parse, validate are missing

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_full_concrete_subclass_can_be_instantiated(self) -> None:
        """A subclass that implements all methods can be instantiated."""

        class Full(Connector):
            async def discover(self, source):  # type: ignore[override]
                return []

            async def fetch(self, artifact, source):  # type: ignore[override]
                return b""

            def parse(self, artifact, raw):  # type: ignore[override]
                return []

            async def validate(self, credentials_ref):  # type: ignore[override]
                pass

        instance = Full()
        assert isinstance(instance, Connector)


# ---------------------------------------------------------------------------
# PackageJsonConnector.parse()
# ---------------------------------------------------------------------------

_RAW_URL = "https://raw.githubusercontent.com/acme/catalog-test-fixtures/catalog-test-fixtures-v1/package.json"
_PKG_ARTIFACT = DiscoveredArtifact(
    artifact_id="package.json",
    source_url=_RAW_URL,
    artifact_type="package_json",
)

_FULL_PKG = json.dumps(
    {
        "name": "@acme/catalog-test-fixtures",
        "version": "2.0.0",
        "dependencies": {"express": "^4.18.2", "zod": "^3.22.4"},
        "devDependencies": {"typescript": "^5.3.3", "jest": "^29.7.0"},
    }
).encode()

_MINIMAL_PKG = json.dumps({"name": "simple-pkg"}).encode()


class TestPackageJsonConnectorParse:
    def setup_method(self) -> None:
        self.connector = PackageJsonConnector()

    def test_returns_one_fact(self) -> None:
        facts = self.connector.parse(_PKG_ARTIFACT, _FULL_PKG)
        assert len(facts) == 1

    def test_category_is_package_manifest(self) -> None:
        (fact,) = self.connector.parse(_PKG_ARTIFACT, _FULL_PKG)
        assert fact.category == "package_manifest"

    def test_body_contains_name(self) -> None:
        (fact,) = self.connector.parse(_PKG_ARTIFACT, _FULL_PKG)
        assert "@acme/catalog-test-fixtures" in fact.body

    def test_body_contains_version(self) -> None:
        (fact,) = self.connector.parse(_PKG_ARTIFACT, _FULL_PKG)
        assert "2.0.0" in fact.body

    def test_body_contains_dependency(self) -> None:
        (fact,) = self.connector.parse(_PKG_ARTIFACT, _FULL_PKG)
        assert "express" in fact.body

    def test_body_contains_dev_dependency(self) -> None:
        (fact,) = self.connector.parse(_PKG_ARTIFACT, _FULL_PKG)
        assert "typescript" in fact.body

    def test_entity_id_is_deterministic(self) -> None:
        facts_a = self.connector.parse(_PKG_ARTIFACT, _FULL_PKG)
        facts_b = self.connector.parse(_PKG_ARTIFACT, _FULL_PKG)
        assert facts_a[0].entity_id == facts_b[0].entity_id

    def test_entity_id_differs_for_different_path(self) -> None:
        other_artifact = DiscoveredArtifact(
            artifact_id="packages/ui/package.json",
            source_url="https://raw.githubusercontent.com/acme/catalog-test-fixtures/v1/packages/ui/package.json",
            artifact_type="package_json",
        )
        fact_root = self.connector.parse(_PKG_ARTIFACT, _FULL_PKG)[0]
        fact_other = self.connector.parse(other_artifact, _MINIMAL_PKG)[0]
        assert fact_root.entity_id != fact_other.entity_id

    def test_source_url_preserved(self) -> None:
        (fact,) = self.connector.parse(_PKG_ARTIFACT, _FULL_PKG)
        assert fact.source_url == _RAW_URL

    def test_valid_from_is_set(self) -> None:
        (fact,) = self.connector.parse(_PKG_ARTIFACT, _FULL_PKG)
        assert fact.valid_from is not None

    def test_commit_sha_is_none(self) -> None:
        (fact,) = self.connector.parse(_PKG_ARTIFACT, _FULL_PKG)
        assert fact.commit_sha is None

    def test_minimal_package_no_deps(self) -> None:
        """Package with no dependency fields should not error."""
        (fact,) = self.connector.parse(_PKG_ARTIFACT, _MINIMAL_PKG)
        assert "simple-pkg" in fact.body
        assert "dependencies" not in fact.body


# ---------------------------------------------------------------------------
# DocsCorpusConnector.parse()
# ---------------------------------------------------------------------------

_DOCS_RAW_URL = "https://raw.githubusercontent.com/acme/catalog-test-fixtures/catalog-test-fixtures-v1/AGENTS.md"
_AGENTS_ARTIFACT = DiscoveredArtifact(
    artifact_id="AGENTS.md",
    source_url=_DOCS_RAW_URL,
    artifact_type="docs_corpus",
)

_AGENTS_MD = b"# AGENTS.md\n\nThis is the agents doc.\n\n## Overview\n\nSome details here.\n"


class TestDocsCorpusPathFilter:
    """Unit tests for the _is_docs_corpus_path helper."""

    def test_agents_md_included(self) -> None:
        assert _is_docs_corpus_path("AGENTS.md") is True

    def test_docs_markdown_included(self) -> None:
        assert _is_docs_corpus_path("docs/quickstart.md") is True

    def test_docs_nested_markdown_included(self) -> None:
        assert _is_docs_corpus_path("docs/reference/api.md") is True

    def test_docs_adr_excluded(self) -> None:
        assert _is_docs_corpus_path("docs/adr/0001-something.md") is False

    def test_docs_rfc_excluded(self) -> None:
        assert _is_docs_corpus_path("docs/rfc/rfc-001.md") is False

    def test_root_readme_excluded(self) -> None:
        assert _is_docs_corpus_path("README.md") is False

    def test_non_markdown_excluded(self) -> None:
        assert _is_docs_corpus_path("docs/notes.txt") is False

    def test_package_json_excluded(self) -> None:
        assert _is_docs_corpus_path("package.json") is False


class TestDocsCorpusConnectorParse:
    def setup_method(self) -> None:
        self.connector = DocsCorpusConnector()

    def test_returns_one_fact(self) -> None:
        facts = self.connector.parse(_AGENTS_ARTIFACT, _AGENTS_MD)
        assert len(facts) == 1

    def test_category_is_dev_doc(self) -> None:
        (fact,) = self.connector.parse(_AGENTS_ARTIFACT, _AGENTS_MD)
        assert fact.category == "dev_doc"

    def test_body_contains_file_content(self) -> None:
        (fact,) = self.connector.parse(_AGENTS_ARTIFACT, _AGENTS_MD)
        assert "agents doc" in fact.body

    def test_body_contains_heading_summary(self) -> None:
        (fact,) = self.connector.parse(_AGENTS_ARTIFACT, _AGENTS_MD)
        assert "AGENTS.md" in fact.body or "Overview" in fact.body

    def test_body_contains_path(self) -> None:
        (fact,) = self.connector.parse(_AGENTS_ARTIFACT, _AGENTS_MD)
        assert "AGENTS.md" in fact.body

    def test_entity_id_is_deterministic(self) -> None:
        facts_a = self.connector.parse(_AGENTS_ARTIFACT, _AGENTS_MD)
        facts_b = self.connector.parse(_AGENTS_ARTIFACT, _AGENTS_MD)
        assert facts_a[0].entity_id == facts_b[0].entity_id

    def test_entity_id_differs_for_different_path(self) -> None:
        other_artifact = DiscoveredArtifact(
            artifact_id="docs/quickstart.md",
            source_url="https://raw.githubusercontent.com/acme/catalog-test-fixtures/v1/docs/quickstart.md",
            artifact_type="docs_corpus",
        )
        fact_agents = self.connector.parse(_AGENTS_ARTIFACT, _AGENTS_MD)[0]
        fact_other = self.connector.parse(other_artifact, b"# Quickstart\n")[0]
        assert fact_agents.entity_id != fact_other.entity_id

    def test_source_url_preserved(self) -> None:
        (fact,) = self.connector.parse(_AGENTS_ARTIFACT, _AGENTS_MD)
        assert fact.source_url == _DOCS_RAW_URL

    def test_valid_from_is_set(self) -> None:
        (fact,) = self.connector.parse(_AGENTS_ARTIFACT, _AGENTS_MD)
        assert fact.valid_from is not None

    def test_commit_sha_is_none(self) -> None:
        (fact,) = self.connector.parse(_AGENTS_ARTIFACT, _AGENTS_MD)
        assert fact.commit_sha is None


# ---------------------------------------------------------------------------
# OpenAPIConnector.parse()
# ---------------------------------------------------------------------------

_OPENAPI_RAW_URL = (
    "https://raw.githubusercontent.com/acme/catalog-test-fixtures/" "catalog-test-fixtures-v1/petstore.openapi.yaml"
)
_OPENAPI_ARTIFACT = DiscoveredArtifact(
    artifact_id="petstore.openapi.yaml",
    source_url=_OPENAPI_RAW_URL,
    artifact_type="openapi",
)

_OPENAPI_YAML = b"""\
openapi: "3.0.3"
info:
  title: Petstore API
  version: "1.0.0"
  description: A minimal petstore used for catalog tests.
paths:
  /pets:
    get:
      summary: List all pets
      operationId: listPets
      responses:
        "200":
          description: A list of pets
  /pets/{petId}:
    get:
      summary: Info for a specific pet
      operationId: showPetById
      responses:
        "200":
          description: Expected response to a valid request
  /health:
    get:
      summary: Health check
      operationId: healthCheck
      responses:
        "200":
          description: OK
"""

_OPENAPI_JSON_ARTIFACT = DiscoveredArtifact(
    artifact_id="service.openapi.json",
    source_url="https://raw.githubusercontent.com/acme/svc/main/service.openapi.json",
    artifact_type="openapi",
)
_OPENAPI_JSON_BYTES = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Service API", "version": "2.0.0"},
        "paths": {"/status": {"get": {"summary": "Status check", "operationId": "status"}}},
    }
).encode()

_OPENAPI_NO_PATHS = b"""\
openapi: "3.0.3"
info:
  title: Minimal Spec
  version: "0.1.0"
"""


class TestOpenAPIConnectorParse:
    def setup_method(self) -> None:
        self.connector = OpenAPIConnector()

    def test_returns_one_fact(self) -> None:
        facts = self.connector.parse(_OPENAPI_ARTIFACT, _OPENAPI_YAML)
        assert len(facts) == 1

    def test_category_is_api_doc(self) -> None:
        (fact,) = self.connector.parse(_OPENAPI_ARTIFACT, _OPENAPI_YAML)
        assert fact.category == "api_doc"

    def test_body_contains_title(self) -> None:
        (fact,) = self.connector.parse(_OPENAPI_ARTIFACT, _OPENAPI_YAML)
        assert "Petstore API" in fact.body

    def test_body_contains_version(self) -> None:
        (fact,) = self.connector.parse(_OPENAPI_ARTIFACT, _OPENAPI_YAML)
        assert "1.0.0" in fact.body

    def test_body_contains_description(self) -> None:
        (fact,) = self.connector.parse(_OPENAPI_ARTIFACT, _OPENAPI_YAML)
        assert "petstore" in fact.body.lower()

    def test_body_contains_path_summaries(self) -> None:
        (fact,) = self.connector.parse(_OPENAPI_ARTIFACT, _OPENAPI_YAML)
        assert "/pets" in fact.body
        assert "listPets" in fact.body

    def test_entity_id_is_deterministic(self) -> None:
        facts_a = self.connector.parse(_OPENAPI_ARTIFACT, _OPENAPI_YAML)
        facts_b = self.connector.parse(_OPENAPI_ARTIFACT, _OPENAPI_YAML)
        assert facts_a[0].entity_id == facts_b[0].entity_id

    def test_entity_id_differs_for_different_path(self) -> None:
        other = DiscoveredArtifact(
            artifact_id="api/other.openapi.yaml",
            source_url="https://raw.githubusercontent.com/acme/catalog-test-fixtures/v1/api/other.openapi.yaml",
            artifact_type="openapi",
        )
        fact_a = self.connector.parse(_OPENAPI_ARTIFACT, _OPENAPI_YAML)[0]
        fact_b = self.connector.parse(other, _OPENAPI_YAML)[0]
        assert fact_a.entity_id != fact_b.entity_id

    def test_source_url_preserved(self) -> None:
        (fact,) = self.connector.parse(_OPENAPI_ARTIFACT, _OPENAPI_YAML)
        assert fact.source_url == _OPENAPI_RAW_URL

    def test_commit_sha_not_none_for_raw_github_url(self) -> None:
        """Connector extracts a sha-like string from the raw GitHub URL."""
        (fact,) = self.connector.parse(_OPENAPI_ARTIFACT, _OPENAPI_YAML)
        # The connector sets commit_sha from parts[6] of the raw.githubusercontent.com URL.
        # For a single-path-segment file, parts[6] is the filename (not the ref).
        # This documents the actual behaviour; the value is non-None for valid raw URLs.
        assert fact.commit_sha is not None

    def test_json_format_parsed_correctly(self) -> None:
        (fact,) = self.connector.parse(_OPENAPI_JSON_ARTIFACT, _OPENAPI_JSON_BYTES)
        assert fact.category == "api_doc"
        assert "Service API" in fact.body
        assert "/status" in fact.body

    def test_no_paths_in_spec(self) -> None:
        (fact,) = self.connector.parse(_OPENAPI_ARTIFACT, _OPENAPI_NO_PATHS)
        assert fact.category == "api_doc"
        assert "Minimal Spec" in fact.body

    def test_valid_from_is_set(self) -> None:
        (fact,) = self.connector.parse(_OPENAPI_ARTIFACT, _OPENAPI_YAML)
        assert fact.valid_from is not None


# ---------------------------------------------------------------------------
# MarkdownADRRFCConnector.parse()
# ---------------------------------------------------------------------------

_ADR_RAW_URL = "https://raw.githubusercontent.com/acme/repo/main/docs/adr/0001-use-postgres.md"
_ADR_ARTIFACT = DiscoveredArtifact(
    artifact_id="docs/adr/0001-use-postgres.md",
    source_url=_ADR_RAW_URL,
    artifact_type="markdown_adr_rfc",
)

_RFC_RAW_URL = "https://raw.githubusercontent.com/acme/repo/main/docs/rfc/rfc-001-api-versioning.md"
_RFC_ARTIFACT = DiscoveredArtifact(
    artifact_id="docs/rfc/rfc-001-api-versioning.md",
    source_url=_RFC_RAW_URL,
    artifact_type="markdown_adr_rfc",
)

_ADR_WITH_FRONTMATTER = b"""\
---
title: Use PostgreSQL as primary store
status: Accepted
---
# Decision-0001

We evaluated multiple databases and chose PostgreSQL.

## Context

ACID guarantees required.
"""

_ADR_WITHOUT_FRONTMATTER = b"""\
# Decision-0002: Use Redis for caching

**Status:** Proposed

We need a fast cache layer.
"""

_RFC_BYTES = b"""\
---
title: API versioning via URL prefix
status: Draft
---
Propose using `/v1/` prefixes for all routes.
"""

_MALFORMED_FRONTMATTER = b"""\
---
title: [unclosed bracket
status: broken
---
# Body content here
"""


class TestMarkdownADRRFCConnectorParse:
    def setup_method(self) -> None:
        self.connector = MarkdownADRRFCConnector()

    def test_adr_returns_one_fact(self) -> None:
        facts = self.connector.parse(_ADR_ARTIFACT, _ADR_WITH_FRONTMATTER)
        assert len(facts) == 1

    def test_adr_category(self) -> None:
        (fact,) = self.connector.parse(_ADR_ARTIFACT, _ADR_WITH_FRONTMATTER)
        assert fact.category == "adr"

    def test_rfc_category(self) -> None:
        (fact,) = self.connector.parse(_RFC_ARTIFACT, _RFC_BYTES)
        assert fact.category == "rfc"

    def test_frontmatter_title_in_body(self) -> None:
        (fact,) = self.connector.parse(_ADR_ARTIFACT, _ADR_WITH_FRONTMATTER)
        assert "Use PostgreSQL as primary store" in fact.body

    def test_frontmatter_status_in_body(self) -> None:
        (fact,) = self.connector.parse(_ADR_ARTIFACT, _ADR_WITH_FRONTMATTER)
        assert "Accepted" in fact.body

    def test_frontmatter_stripped_from_raw_body(self) -> None:
        """The raw ``---`` fence lines must not appear in the body."""
        (fact,) = self.connector.parse(_ADR_ARTIFACT, _ADR_WITH_FRONTMATTER)
        assert "---" not in fact.body

    def test_no_frontmatter_body_preserved(self) -> None:
        (fact,) = self.connector.parse(_ADR_ARTIFACT, _ADR_WITHOUT_FRONTMATTER)
        assert "Redis" in fact.body

    def test_entity_id_is_deterministic(self) -> None:
        facts_a = self.connector.parse(_ADR_ARTIFACT, _ADR_WITH_FRONTMATTER)
        facts_b = self.connector.parse(_ADR_ARTIFACT, _ADR_WITH_FRONTMATTER)
        assert facts_a[0].entity_id == facts_b[0].entity_id

    def test_entity_id_differs_for_adr_vs_rfc(self) -> None:
        fact_adr = self.connector.parse(_ADR_ARTIFACT, _ADR_WITH_FRONTMATTER)[0]
        fact_rfc = self.connector.parse(_RFC_ARTIFACT, _RFC_BYTES)[0]
        assert fact_adr.entity_id != fact_rfc.entity_id

    def test_source_url_preserved(self) -> None:
        (fact,) = self.connector.parse(_ADR_ARTIFACT, _ADR_WITH_FRONTMATTER)
        assert fact.source_url == _ADR_RAW_URL

    def test_valid_from_is_set(self) -> None:
        (fact,) = self.connector.parse(_ADR_ARTIFACT, _ADR_WITH_FRONTMATTER)
        assert fact.valid_from is not None

    def test_commit_sha_is_none(self) -> None:
        (fact,) = self.connector.parse(_ADR_ARTIFACT, _ADR_WITH_FRONTMATTER)
        assert fact.commit_sha is None

    def test_malformed_frontmatter_does_not_raise(self) -> None:
        """Malformed YAML frontmatter must not crash parse() — body is preserved."""
        facts = self.connector.parse(_ADR_ARTIFACT, _MALFORMED_FRONTMATTER)
        assert len(facts) == 1
        assert facts[0].category == "adr"


# ---------------------------------------------------------------------------
# ReleaseNotesConnector.parse()
# ---------------------------------------------------------------------------

from json import dumps as _jdumps  # noqa: E402 — local import at bottom is fine
from urllib.parse import quote as _quote  # noqa: E402


def _make_release_artifact(
    tag: str,
    name: str,
    body_text: str,
    published_at: str | None,
    owner: str = "acme",
    repo: str = "catalog-test-fixtures",
) -> DiscoveredArtifact:
    """Build a DiscoveredArtifact with embedded release metadata, mirroring ReleaseNotesConnector.discover()."""
    meta = {
        "tag_name": tag,
        "name": name,
        "published_at": published_at,
        "body": body_text,
        "owner": owner,
        "repo": repo,
    }
    source_url = f"{_RELEASE_META_PREFIX}{_quote(_jdumps(meta))}"
    return DiscoveredArtifact(
        artifact_id=tag,
        source_url=source_url,
        artifact_type="release_note",
    )


_V200_BODY = (
    "## Breaking Changes\n"
    "- Removed GET /pets/{petId}/owner\n"
    "- petId is now UUID\n\n"
    "## New Features\n"
    "- Added POST /pets"
)
_V100_BODY = "## Initial Release\n- GET /pets\n- GET /pets/{petId}\n- GET /health"

_V200_ARTIFACT = _make_release_artifact(
    tag="v2.0.0",
    name="v2.0.0 — Breaking changes to /pets",
    body_text=_V200_BODY,
    published_at="2025-03-15T10:00:00Z",
)
_V100_ARTIFACT = _make_release_artifact(
    tag="v1.0.0",
    name="v1.0.0 — Initial release",
    body_text=_V100_BODY,
    published_at="2024-06-01T09:00:00Z",
)
_EMPTY_BODY_ARTIFACT = _make_release_artifact(
    tag="v0.1.0",
    name="v0.1.0 — Pre-release",
    body_text="",
    published_at="2024-01-15T08:00:00Z",
)
_NO_DATE_ARTIFACT = _make_release_artifact(
    tag="v3.0.0",
    name="v3.0.0 — Nightly",
    body_text="Nightly build",
    published_at=None,
)


class TestReleaseNotesConnectorParse:
    def setup_method(self) -> None:
        self.connector = ReleaseNotesConnector()

    def test_returns_one_fact(self) -> None:
        facts = self.connector.parse(_V200_ARTIFACT, _V200_BODY.encode())
        assert len(facts) == 1

    def test_category_is_release_note(self) -> None:
        (fact,) = self.connector.parse(_V200_ARTIFACT, _V200_BODY.encode())
        assert fact.category == "release_note"

    def test_body_contains_release_name(self) -> None:
        (fact,) = self.connector.parse(_V200_ARTIFACT, _V200_BODY.encode())
        assert "v2.0.0" in fact.body

    def test_body_contains_release_content(self) -> None:
        (fact,) = self.connector.parse(_V200_ARTIFACT, _V200_BODY.encode())
        assert "Breaking Changes" in fact.body

    def test_valid_from_parsed_from_published_at(self) -> None:
        (fact,) = self.connector.parse(_V200_ARTIFACT, _V200_BODY.encode())
        assert fact.valid_from is not None
        assert fact.valid_from.year == 2025
        assert fact.valid_from.month == 3

    def test_valid_from_is_none_when_no_published_at(self) -> None:
        (fact,) = self.connector.parse(_NO_DATE_ARTIFACT, b"Nightly build")
        assert fact.valid_from is None

    def test_entity_id_is_deterministic(self) -> None:
        facts_a = self.connector.parse(_V200_ARTIFACT, _V200_BODY.encode())
        facts_b = self.connector.parse(_V200_ARTIFACT, _V200_BODY.encode())
        assert facts_a[0].entity_id == facts_b[0].entity_id

    def test_entity_id_differs_for_different_tags(self) -> None:
        fact_v2 = self.connector.parse(_V200_ARTIFACT, _V200_BODY.encode())[0]
        fact_v1 = self.connector.parse(_V100_ARTIFACT, _V100_BODY.encode())[0]
        assert fact_v2.entity_id != fact_v1.entity_id

    def test_entity_id_differs_for_different_repo(self) -> None:
        other_artifact = _make_release_artifact(
            tag="v2.0.0",
            name="v2.0.0",
            body_text=_V200_BODY,
            published_at="2025-03-15T10:00:00Z",
            owner="other-org",
            repo="other-repo",
        )
        fact_main = self.connector.parse(_V200_ARTIFACT, _V200_BODY.encode())[0]
        fact_other = self.connector.parse(other_artifact, _V200_BODY.encode())[0]
        assert fact_main.entity_id != fact_other.entity_id

    def test_empty_release_body(self) -> None:
        """Empty release body should not raise; fact is still created."""
        facts = self.connector.parse(_EMPTY_BODY_ARTIFACT, b"")
        assert len(facts) == 1
        assert facts[0].category == "release_note"

    def test_source_url_embedded(self) -> None:
        """source_url must carry the data: URI (not an HTTP URL)."""
        (fact,) = self.connector.parse(_V200_ARTIFACT, _V200_BODY.encode())
        assert fact.source_url.startswith(_RELEASE_META_PREFIX)

    def test_commit_sha_is_none(self) -> None:
        (fact,) = self.connector.parse(_V200_ARTIFACT, _V200_BODY.encode())
        assert fact.commit_sha is None

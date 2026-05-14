"""Unit tests for the seed-bundle loader.

The loader (``scripts/seed_loader.py``) is the only thing standing
between the JSON files under ``seeds/`` and the database, so its parsing
contract and deterministic-UUID rule deserve explicit pins. The
integration tests in ``tests/integration/test_dev_seed.py`` cover the
end-to-end DB behaviour; this file covers shape parsing, error paths,
and the identity-stability rule.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from scripts.seed_loader import (
    bundles_for_usecase,
    default_bundles,
    deterministic_edge_id,
    deterministic_entity_id,
    discover_usecases,
    load_bundle,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEEDS = _REPO_ROOT / "seeds"


class TestLoadBundle:
    def test_parses_vocabulary_file(self) -> None:
        bundle = load_bundle(_SEEDS / "_vocabulary.json")
        assert bundle.name == "_vocabulary"
        assert bundle.vocabulary, "vocabulary bundle should contain vocab rows"
        assert all("kind" in row and "value" in row for row in bundle.vocabulary)
        assert bundle.entities == []
        assert bundle.facts == []

    def test_parses_demo_minimal(self) -> None:
        bundle = load_bundle(_SEEDS / "demo-minimal" / "v1.json")
        names = {e["name"] for e in bundle.entities}
        assert names == {"salt-design-system", "user-preferences"}, names
        # Salt has external_id mappings; user-preferences doesn't.
        salt = next(e for e in bundle.entities if e["name"] == "salt-design-system")
        assert salt.get("external_ids"), "salt-design-system should have external_ids"
        up = next(e for e in bundle.entities if e["name"] == "user-preferences")
        assert not up.get("external_ids"), "user-preferences should stay thin"

    def test_parses_salt_ds_v143(self) -> None:
        bundle = load_bundle(_SEEDS / "salt-ds" / "v1.43.json")
        # Test contract: integration tests assert >= 16 composes edges from
        # Salt. v1.43 must contribute at least 16 child component entities.
        children = [e for e in bundle.entities if e.get("parent") == "salt-design-system"]
        assert len(children) >= 16, f"v1.43 should ship >=16 components, got {len(children)}"

    def test_parses_salt_ds_v145_bitemporal(self) -> None:
        bundle = load_bundle(_SEEDS / "salt-ds" / "v1.45.json")
        # The 3-row current_version history is what powers time-travel demos.
        # Pin it: changes here are a real semantic change.
        history = next(
            (b for b in bundle.bitemporal_attributes if b["key"] == "current_version"),
            None,
        )
        assert history is not None, "v1.45 must carry the current_version bitemporal history"
        assert history["replace_existing"] is True
        versions = [row["value"] for row in history["rows"]]
        assert versions == ["1.43.0", "1.44.0", "1.45.0"], versions

    def test_rejects_unknown_top_level_key(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"schema_version": 1, "secret_field": []}))
        with pytest.raises(ValueError, match="unknown top-level key"):
            load_bundle(bad)

    def test_rejects_wrong_schema_version(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"schema_version": 99}))
        with pytest.raises(ValueError, match="schema_version 99 not supported"):
            load_bundle(bad)

    def test_rejects_non_object_root(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps([1, 2, 3]))
        with pytest.raises(ValueError, match="top-level must be an object"):
            load_bundle(bad)

    def test_parses_iso_with_z_suffix(self, tmp_path: Path) -> None:
        # Pydantic doesn't accept "Z" in datetime strings; the loader has
        # to normalise it. Pin that behaviour — Salt's release dates are
        # all stored with "Z" and integration tests depend on it.
        f = tmp_path / "iso.json"
        f.write_text(json.dumps({"schema_version": 1, "released_at": "2026-01-15T00:00:00Z"}))
        bundle = load_bundle(f)
        assert bundle.released_at is not None
        assert bundle.released_at.year == 2026


class TestDeterministicIds:
    """Identity stability — these UUIDs are the contract between the
    refactored loader and pre-existing seeded data. Changing the rule
    here orphans every row keyed by the old UUIDs.
    """

    def test_entity_id_uuidv5_over_tenant_and_name(self) -> None:
        tid = uuid.UUID("00000000-0000-0000-0000-000000000001")
        eid = deterministic_entity_id(tid, "salt-button")
        assert eid == uuid.uuid5(uuid.NAMESPACE_OID, f"{tid}:salt-button")

    def test_entity_id_matches_running_stack_dev_tenant(self) -> None:
        # The dev tenant on the docker-compose stack has a stable UUID
        # because `make dev-token` is idempotent for slug='dev'. The
        # salt-design-system UUID below is what `GET /v1/capabilities`
        # has returned across many seed runs.
        dev_tid = uuid.UUID("5c777a41-b413-4c3a-9425-9a568da4c1b3")
        assert deterministic_entity_id(dev_tid, "salt-design-system") == uuid.UUID(
            "1f5a0b64-ce83-53a8-829d-482b2cc16222"
        )
        assert deterministic_entity_id(dev_tid, "salt-button") == uuid.UUID(
            "876ce655-b3e8-51d3-9318-c8a4b187bc23"
        )
        assert deterministic_entity_id(dev_tid, "user-preferences") == uuid.UUID(
            "2f8597cf-e641-532b-b924-98a79cc64bdd"
        )

    def test_edge_id_uuidv5_over_tenant_src_rel_dst(self) -> None:
        tid = uuid.UUID("00000000-0000-0000-0000-000000000001")
        src = uuid.UUID("00000000-0000-0000-0000-000000000002")
        dst = uuid.UUID("00000000-0000-0000-0000-000000000003")
        eid = deterministic_edge_id(tid, src, "composes", dst)
        assert eid == uuid.uuid5(uuid.NAMESPACE_OID, f"{tid}:{src}:composes:{dst}")


class TestUsecaseDiscovery:
    def test_discover_skips_hidden_and_underscore(self) -> None:
        usecases = discover_usecases(_SEEDS)
        for uc in usecases:
            assert not uc.startswith("_")
            assert not uc.startswith(".")

    def test_known_usecases_present(self) -> None:
        usecases = discover_usecases(_SEEDS)
        assert "salt-ds" in usecases
        assert "demo-minimal" in usecases

    def test_default_bundles_load_order(self) -> None:
        # _vocabulary first (prerequisite), then demo-minimal, then salt-ds.
        # Within salt-ds, files sort lexically — which is chronological
        # because they're named v1.43, v1.44, v1.45.
        paths = default_bundles(_SEEDS)
        names = [p.name for p in paths]
        assert names[0] == "_vocabulary.json"
        assert "v1.43.json" in names
        assert names.index("v1.43.json") < names.index("v1.44.json") < names.index("v1.45.json")

    def test_bundles_for_usecase_returns_chronological(self) -> None:
        paths = bundles_for_usecase(_SEEDS, "salt-ds")
        names = [p.name for p in paths]
        # Files must load in chronological order — the v1.45 bitemporal
        # block depends on the v1.43 / v1.44 components already existing.
        assert names == sorted(names), f"salt-ds files should load lexically, got {names}"

    def test_bundles_for_usecase_raises_on_unknown(self) -> None:
        with pytest.raises(ValueError, match="not found"):
            bundles_for_usecase(_SEEDS, "no-such-usecase")

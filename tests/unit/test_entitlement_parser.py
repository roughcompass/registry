"""Unit tests for the new entitlement string parser.

Behavior coverage: discriminator-driven splitting, configured role
mapping, multi-alias mappings, multi-tenant inputs, malformed inputs,
unknown-role suffix drops, discriminator mismatches, edge cases (empty
input, tenant slug containing the discriminator substring, discriminator
containing underscores), Prometheus counter increments per drop reason.

The legacy SEAL/verb parser shim in the same module is not exercised
here — it is on the deletion list and its tests will be removed when
the resolver no longer calls into it.
"""

from __future__ import annotations

import logging

import pytest

from registry.auth.entitlements.parser import ParsedEntitlement, parse_entitlements
from registry.config import Settings


def _settings(
    *,
    discriminator: str = "REGISTRY",
    mapping: dict[str, str] | None = None,
) -> Settings:
    """Construct a Settings configured for entitlement parsing.

    Defaults: discriminator=REGISTRY, mapping covers the canonical four
    internal roles via their natural-name external suffixes.
    """
    role_mapping = mapping if mapping is not None else {
        "ADMIN": "admin",
        "PRODUCER": "producer",
        "CONSUMER": "consumer",
        "AUDITOR": "auditor",
    }
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost/r",
        pgbouncer_url="postgresql+asyncpg://u:p@localhost/r",
        scheduler_jobstore_url="postgresql+asyncpg://u:p@localhost/r",
        entitlement_service_url="https://entitlement.example.com",
        entitlement_service_env="DEV",
        entitlement_service_discriminator=discriminator,
        entitlement_role_mapping=dict(role_mapping),
    )


class TestSuccessfulParse:
    def test_well_formed_single_entry(self):
        result = parse_entitlements(["111205_REGISTRY_ADMIN"], _settings())
        assert result == [ParsedEntitlement(tenant_slug="111205", role="admin")]

    def test_multi_tenant_entries(self):
        result = parse_entitlements(
            ["111205_REGISTRY_ADMIN", "999999_REGISTRY_CONSUMER"],
            _settings(),
        )
        assert ParsedEntitlement("111205", "admin") in result
        assert ParsedEntitlement("999999", "consumer") in result
        assert len(result) == 2

    def test_each_canonical_role_round_trips(self):
        result = parse_entitlements(
            [
                "111_REGISTRY_ADMIN",
                "222_REGISTRY_PRODUCER",
                "333_REGISTRY_CONSUMER",
                "444_REGISTRY_AUDITOR",
            ],
            _settings(),
        )
        roles = {(e.tenant_slug, e.role) for e in result}
        assert roles == {
            ("111", "admin"),
            ("222", "producer"),
            ("333", "consumer"),
            ("444", "auditor"),
        }

    def test_alphanumeric_tenant_slug(self):
        result = parse_entitlements(["TENANT_A_REGISTRY_ADMIN"], _settings())
        # Splits on FIRST occurrence of "_REGISTRY_": tenant_slug becomes
        # "TENANT_A". Tenant slugs containing underscores are preserved.
        assert result == [ParsedEntitlement(tenant_slug="TENANT_A", role="admin")]


class TestMultiAliasMapping:
    def test_two_external_aliases_map_to_same_internal_role(self):
        """LDAP rename rollouts can have both old and new strings live."""
        settings = _settings(
            mapping={
                "ADMIN": "admin",
                "ADMINISTRATOR": "admin",
                "PRODUCER": "producer",
                "CONSUMER": "consumer",
                "AUDITOR": "auditor",
            }
        )
        result = parse_entitlements(
            ["111_REGISTRY_ADMIN", "222_REGISTRY_ADMINISTRATOR"],
            settings,
        )
        assert ParsedEntitlement("111", "admin") in result
        assert ParsedEntitlement("222", "admin") in result


class TestDiscriminatorBehavior:
    def test_discriminator_mismatch_silently_dropped(self, caplog):
        """An entitlement for a different service shares the upstream API
        but is not for this deployment — silent drop, no log noise."""
        with caplog.at_level(logging.WARNING):
            result = parse_entitlements(["111_GRAPHREGISTRY_SUPERADMIN"], _settings())
        assert result == []
        # Silent drop — must NOT have logged at WARNING for this case.
        assert not any("entitlement_parse_dropped" in rec.message for rec in caplog.records)

    def test_discriminator_containing_underscore(self):
        """A discriminator like DATA_CATALOG must split correctly."""
        settings = _settings(discriminator="DATA_CATALOG", mapping={"ADMIN": "admin"})
        result = parse_entitlements(["111_DATA_CATALOG_ADMIN"], settings)
        assert result == [ParsedEntitlement(tenant_slug="111", role="admin")]

    def test_alternate_discriminator_works_in_isolation(self):
        settings = _settings(discriminator="GRAPHREGISTRY", mapping={"SUPERADMIN": "admin"})
        result = parse_entitlements(
            ["111_GRAPHREGISTRY_SUPERADMIN", "222_REGISTRY_ADMIN"],
            settings,
        )
        # First entry matches; second has REGISTRY (not GRAPHREGISTRY) so it is dropped.
        assert result == [ParsedEntitlement(tenant_slug="111", role="admin")]


class TestMalformedAndUnknown:
    def test_unknown_role_suffix_logged_and_dropped(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = parse_entitlements(
                ["111205_REGISTRY_GHOST", "222_REGISTRY_ADMIN"],
                _settings(),
            )
        # The unknown-role entry is dropped; the valid one still parses.
        assert result == [ParsedEntitlement(tenant_slug="222", role="admin")]
        assert any("unknown_role" in rec.message for rec in caplog.records)

    def test_malformed_delimiter_at_position_zero(self, caplog):
        """`_REGISTRY_ADMIN` has the delimiter at the start: tenant slug is empty."""
        with caplog.at_level(logging.WARNING):
            result = parse_entitlements(["_REGISTRY_ADMIN"], _settings())
        assert result == []
        assert any("malformed" in rec.message for rec in caplog.records)

    def test_no_delimiter_at_all_silently_dropped(self, caplog):
        """A string without `_REGISTRY_` cannot be addressed to this service."""
        with caplog.at_level(logging.WARNING):
            result = parse_entitlements(["plain-text-no-delim"], _settings())
        assert result == []
        # Silent — different-namespace entitlements are not loggable noise.
        assert not any("entitlement_parse_dropped" in rec.message for rec in caplog.records)

    def test_continues_after_drop(self):
        """One bad entitlement in a list must not fail the rest."""
        result = parse_entitlements(
            [
                "111_REGISTRY_ADMIN",
                "_REGISTRY_ADMIN",  # malformed
                "222_REGISTRY_GHOST",  # unknown role
                "333_GRAPHREGISTRY_ADMIN",  # different discriminator
                "444_REGISTRY_CONSUMER",
            ],
            _settings(),
        )
        # Two valid entries survive; three are dropped/ignored.
        assert ParsedEntitlement("111", "admin") in result
        assert ParsedEntitlement("444", "consumer") in result
        assert len(result) == 2


class TestEdgeCases:
    def test_empty_list_returns_empty(self):
        assert parse_entitlements([], _settings()) == []

    def test_empty_string_silently_dropped(self):
        # An empty string contains no delimiter → other_namespace, silent drop.
        assert parse_entitlements([""], _settings()) == []

    def test_tenant_slug_containing_discriminator_substring(self):
        """A tenant slug that contains the discriminator name as a substring —
        but not as the bracketed delimiter `_<DISCRIMINATOR>_` — is preserved.
        `MY_REGISTRYISH_REGISTRY_ADMIN` splits on the FIRST literal `_REGISTRY_`,
        which is at position 14, producing `tenant_slug="MY_REGISTRYISH"` and
        `role_suffix="ADMIN"`. The `REGISTRY` inside `REGISTRYISH` is not a
        delimiter because it lacks the trailing `_`."""
        result = parse_entitlements(["MY_REGISTRYISH_REGISTRY_ADMIN"], _settings())
        assert result == [ParsedEntitlement(tenant_slug="MY_REGISTRYISH", role="admin")]

    def test_role_suffix_case_sensitivity(self):
        """Mapping lookup is case-sensitive — `admin` (lowercase) does not
        match a mapping key of `ADMIN` and is dropped as unknown_role."""
        result = parse_entitlements(["111_REGISTRY_admin"], _settings())
        assert result == []


class TestPrometheusInstrumentation:
    """Smoke tests for counter increments — confirms each drop reason
    increments the corresponding counter without asserting absolute values
    (other tests in the suite share the global registry and may concurrently
    increment them)."""

    def test_other_namespace_increments_ignored_counter(self):
        from registry.auth.entitlements.parser import _PARSE_IGNORED

        before = _PARSE_IGNORED.labels(reason="other_namespace")._value.get()
        parse_entitlements(["111_OTHER_ADMIN"], _settings())
        after = _PARSE_IGNORED.labels(reason="other_namespace")._value.get()
        assert after - before == 1

    def test_malformed_increments_dropped_counter(self):
        from registry.auth.entitlements.parser import _PARSE_DROPPED

        before = _PARSE_DROPPED.labels(reason="malformed")._value.get()
        parse_entitlements(["_REGISTRY_ADMIN"], _settings())
        after = _PARSE_DROPPED.labels(reason="malformed")._value.get()
        assert after - before == 1

    def test_unknown_role_increments_dropped_counter(self):
        from registry.auth.entitlements.parser import _PARSE_DROPPED

        before = _PARSE_DROPPED.labels(reason="unknown_role")._value.get()
        parse_entitlements(["111_REGISTRY_GHOST"], _settings())
        after = _PARSE_DROPPED.labels(reason="unknown_role")._value.get()
        assert after - before == 1


class TestParsedEntitlementShape:
    def test_is_frozen_dataclass(self):
        e = ParsedEntitlement(tenant_slug="111", role="admin")
        with pytest.raises((AttributeError, Exception)):
            e.tenant_slug = "999"  # type: ignore[misc]

    def test_equality(self):
        a = ParsedEntitlement("111", "admin")
        b = ParsedEntitlement("111", "admin")
        assert a == b
        assert hash(a) == hash(b)

"""Unit tests for Settings — covers defaults and the cross-field validation
that rejects a non-oidc auth_mode when no claim source URL is supplied."""

import pytest

from registry.config import Settings, _parse_csv_list, _parse_role_mapping


def _base_kwargs() -> dict:
    """Minimal kwargs to construct a valid Settings without env vars."""
    return dict(
        database_url="postgresql+asyncpg://user:pass@localhost/registry",
        pgbouncer_url="postgresql+asyncpg://user:pass@localhost/registry",
        scheduler_jobstore_url="postgresql+asyncpg://user:pass@localhost/registry",
    )


class TestSettingsDefaults:
    def test_auth_defaults(self):
        s = Settings(**_base_kwargs())
        assert s.auth_mode == "oidc"
        assert s.auth_claim_source_url is None
        assert s.auth_claim_cache_ttl_seconds == 300
        assert s.auth_stale_ceiling_seconds == 86400
        assert s.auth_serve_stale_on_failure is False
        assert s.auth_tenant_id_header == "X-Tenant-ID"
        assert s.auth_seal_id_header_alias == "X-SEAL-ID"

    def test_progression_default(self):
        s = Settings(**_base_kwargs())
        assert s.progression_definition_cache_ttl_seconds == 60

    def test_has_all_new_attributes(self):
        s = Settings(**_base_kwargs())
        for attr in (
            "auth_mode",
            "auth_claim_source_url",
            "auth_claim_cache_ttl_seconds",
            "auth_stale_ceiling_seconds",
            "auth_serve_stale_on_failure",
            "auth_tenant_id_header",
            "auth_seal_id_header_alias",
            "progression_definition_cache_ttl_seconds",
        ):
            assert hasattr(s, attr), f"Settings missing attribute: {attr}"


class TestAuthModeValidation:
    def test_rsam_without_claim_source_url_raises(self):
        with pytest.raises(ValueError, match="AUTH_CLAIM_SOURCE_URL"):
            Settings(**_base_kwargs(), auth_mode="rsam", auth_claim_source_url=None)

    def test_rsam_with_claim_source_url_accepted(self):
        s = Settings(
            **_base_kwargs(),
            auth_mode="rsam",
            auth_claim_source_url="https://entitlements.example.com",
        )
        assert s.auth_mode == "rsam"
        assert s.auth_claim_source_url == "https://entitlements.example.com"

    def test_oidc_without_claim_source_url_accepted(self):
        # Default path — no URL needed for oidc mode.
        s = Settings(**_base_kwargs(), auth_mode="oidc", auth_claim_source_url=None)
        assert s.auth_mode == "oidc"


# Default canonical role mapping used in entitlement-related tests below.
_CANONICAL_MAPPING = {
    "ADMIN": "admin",
    "PRODUCER": "producer",
    "CONSUMER": "consumer",
    "AUDITOR": "auditor",
}


def _entitlement_kwargs(**overrides) -> dict:
    """Minimal kwargs to enable the entitlement code path without overrides."""
    base = dict(
        entitlement_service_url="https://entitlement.example.com",
        entitlement_service_env="DEV",
        entitlement_service_discriminator="REGISTRY",
        entitlement_role_mapping=dict(_CANONICAL_MAPPING),
    )
    base.update(overrides)
    return base


class TestEntitlementSettingsDefaults:
    """When the entitlement path is disabled (URL empty), all entitlement
    fields default to safe empty values and __post_init__ does not fire."""

    def test_defaults_when_disabled(self):
        s = Settings(**_base_kwargs())
        assert s.entitlement_service_url == ""
        assert s.entitlement_service_env == ""
        assert s.entitlement_service_discriminator == ""
        assert s.entitlement_role_mapping == {}
        assert s.entitlement_connect_timeout_ms == 250
        assert s.entitlement_read_timeout_ms == 1500
        assert s.entitlement_max_retries == 1
        assert s.entitlement_cache_max_entries == 10000

    def test_oidc_validation_field_defaults(self):
        s = Settings(**_base_kwargs())
        assert s.oidc_issuer_allowlist == []
        assert s.oidc_client_id_allowlist == []
        assert s.oidc_max_token_ttl_seconds == 900
        assert s.resource_uri_allowlist == []


class TestEntitlementSettingsValidation:
    """When entitlement_service_url is set, the rest of the entitlement
    config is required-together. __post_init__ enforces this."""

    def test_complete_config_accepted(self):
        s = Settings(**_base_kwargs(), **_entitlement_kwargs())
        assert s.entitlement_service_discriminator == "REGISTRY"
        assert s.entitlement_role_mapping == _CANONICAL_MAPPING

    def test_missing_env_when_url_set_raises(self):
        with pytest.raises(ValueError, match="ENTITLEMENT_SERVICE_ENV"):
            Settings(**_base_kwargs(), **_entitlement_kwargs(entitlement_service_env=""))

    def test_missing_discriminator_when_url_set_raises(self):
        with pytest.raises(ValueError, match="ENTITLEMENT_SERVICE_DISCRIMINATOR"):
            Settings(**_base_kwargs(), **_entitlement_kwargs(entitlement_service_discriminator=""))

    def test_discriminator_with_whitespace_raises(self):
        with pytest.raises(ValueError, match="whitespace"):
            Settings(
                **_base_kwargs(),
                **_entitlement_kwargs(entitlement_service_discriminator="REGISTRY SERVICE"),
            )

    def test_empty_role_mapping_when_url_set_raises(self):
        with pytest.raises(ValueError, match="non-empty mapping"):
            Settings(**_base_kwargs(), **_entitlement_kwargs(entitlement_role_mapping={}))

    def test_role_mapping_empty_external_key_raises(self):
        with pytest.raises(ValueError, match="empty external"):
            Settings(
                **_base_kwargs(),
                **_entitlement_kwargs(entitlement_role_mapping={"": "admin"}),
            )

    def test_role_mapping_empty_internal_value_raises(self):
        with pytest.raises(ValueError, match="empty internal"):
            Settings(
                **_base_kwargs(),
                **_entitlement_kwargs(entitlement_role_mapping={"ADMIN": ""}),
            )

    def test_role_mapping_unknown_internal_role_raises(self):
        with pytest.raises(ValueError, match="unknown internal role"):
            Settings(
                **_base_kwargs(),
                **_entitlement_kwargs(entitlement_role_mapping={"ADMIN": "superuser"}),
            )

    def test_partial_internal_coverage_warns_but_succeeds(self, caplog):
        """A deployment may legitimately omit some internal roles
        (e.g., auditor). __post_init__ logs a WARNING but does not fail."""
        import logging

        with caplog.at_level(logging.WARNING, logger="registry.config"):
            s = Settings(
                **_base_kwargs(),
                **_entitlement_kwargs(entitlement_role_mapping={"ADMIN": "admin"}),
            )
        assert s.entitlement_role_mapping == {"ADMIN": "admin"}
        assert any(
            "does not map any external suffix" in record.message
            and "auditor" in record.message
            for record in caplog.records
        )

    def test_multiple_external_aliases_to_same_internal_accepted(self):
        """LDAP rename rollouts may emit both old and new strings concurrently."""
        s = Settings(
            **_base_kwargs(),
            **_entitlement_kwargs(
                entitlement_role_mapping={
                    "ADMIN": "admin",
                    "ADMINISTRATOR": "admin",
                    "PRODUCER": "producer",
                    "CONSUMER": "consumer",
                    "AUDITOR": "auditor",
                }
            ),
        )
        assert s.entitlement_role_mapping["ADMIN"] == "admin"
        assert s.entitlement_role_mapping["ADMINISTRATOR"] == "admin"


class TestParseRoleMapping:
    """Covers the env-var string → dict parsing helper."""

    def test_canonical_form(self):
        result = _parse_role_mapping("ADMIN:admin,PRODUCER:producer,CONSUMER:consumer,AUDITOR:auditor")
        assert result == _CANONICAL_MAPPING

    def test_strips_whitespace(self):
        result = _parse_role_mapping(" ADMIN : admin , PRODUCER : producer ")
        assert result == {"ADMIN": "admin", "PRODUCER": "producer"}

    def test_empty_string_returns_empty_dict(self):
        assert _parse_role_mapping("") == {}

    def test_none_returns_empty_dict(self):
        assert _parse_role_mapping(None) == {}

    def test_missing_colon_raises(self):
        with pytest.raises(ValueError, match="missing the ':' delimiter"):
            _parse_role_mapping("ADMIN:admin,NOTAVALIDPAIR")

    def test_duplicate_external_key_last_wins(self):
        # LDAP rename rollouts can ship duplicates briefly.
        result = _parse_role_mapping("ADMIN:admin,ADMIN:producer")
        assert result == {"ADMIN": "producer"}

    def test_skips_empty_pairs(self):
        # Trailing or doubled commas should not create empty entries.
        result = _parse_role_mapping("ADMIN:admin,,PRODUCER:producer,")
        assert result == {"ADMIN": "admin", "PRODUCER": "producer"}


class TestParseCsvList:
    def test_parses(self):
        assert _parse_csv_list("a,b,c") == ["a", "b", "c"]

    def test_strips_whitespace(self):
        assert _parse_csv_list(" a , b ,c ") == ["a", "b", "c"]

    def test_skips_empty(self):
        assert _parse_csv_list("a,,b,") == ["a", "b"]

    def test_empty_string_returns_empty(self):
        assert _parse_csv_list("") == []

    def test_none_returns_empty(self):
        assert _parse_csv_list(None) == []

"""Unit tests for Settings — covers defaults and the cross-field validation
that rejects a non-oidc auth_mode when no claim source URL is supplied."""

import pytest

from registry.config import Settings


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

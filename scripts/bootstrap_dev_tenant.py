"""Stub — bootstrap is being rewritten against the entitlement-resolved auth path.

The legacy script seeded a tenant + actor + role grants and minted an
api_token. With the api_token table, Role and ActorRole models, and
registry-side role storage all removed, this script's previous job no
longer exists in the same shape.

The replacement (still pending) seeds a tenant + actor in the catalog
DB and registers a corresponding client + canned entitlements in the
mock OIDC + mock entitlement service in `tests/mocks/` so a fresh
checkout can `docker compose up && python scripts/bootstrap_dev_tenant.py`
and start hitting the API with a real-shaped JWT.

Until that rewrite ships, this stub exits cleanly with an explanatory
message rather than crashing on the deleted imports.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "bootstrap_dev_tenant.py: stubbed pending the entitlement-auth rewrite. "
        "See OAR follow-up. For now, seed your dev tenant manually via the "
        "tenants/actors tables and configure ENTITLEMENT_SERVICE_URL "
        "to point at the mock entitlement service.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

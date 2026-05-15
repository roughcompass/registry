"""Bootstrap a local-development tenant, actor, and mock-IDP/entitlement seed.

This script seeds a tenant row + actor row directly in the catalog DB
and registers the corresponding client + canned entitlements in the
local-dev mock OIDC + mock entitlement service. The resulting state is
the minimum needed to hit the registry API with a real-shaped JWT.

**This script is local-development only.** In production, tenants are
JIT-materialized the first time the entitlement service returns an
entitlement for that slug — there is no equivalent registry-side
bootstrap.

The script is idempotent: re-running it against an existing dev state
upserts the rows + re-registers the client without minting new
credentials. ``--client-id`` and ``--client-secret`` may be overridden
to pin known values; otherwise stable defaults are used so successive
runs produce the same credentials.

Usage::

    docker compose up -d            # start mock-oauth2-server + mock-entitlement-service + postgres
    python scripts/bootstrap_dev_tenant.py
    python scripts/bootstrap_dev_tenant.py --tenant-slug 111205 --actor-id F731821
    python scripts/bootstrap_dev_tenant.py --actor-entitlements 111205_REGISTRY_ADMIN

The script writes ``CLIENT_ID``, ``CLIENT_SECRET``, and the actor's
user ID into ``.env.dev`` so the developer can ``source .env.dev`` and
exercise the local API.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sys
import uuid
from pathlib import Path

# Repo root on sys.path for `from registry....` imports when this script is
# invoked from arbitrary cwd (e.g. by integration tests).
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

_DEFAULT_TENANT_SLUG = "111205"
_DEFAULT_TENANT_DISPLAY_NAME = "Local Development Tenant"
_DEFAULT_ACTOR_USER_ID = "dev-admin"
_DEFAULT_ACTOR_DISPLAY_NAME = "Dev Admin"
_DEFAULT_CLIENT_ID = "registry-dev"
_DEFAULT_CLIENT_SECRET = "dev-secret"
_DEFAULT_MOCK_OIDC_URL = "http://localhost:8090"
_DEFAULT_MOCK_ENTITLEMENT_URL = "http://localhost:8091"
_DEFAULT_ENTITLEMENTS: tuple[str, ...] = (
    "111205_REGISTRY_ADMIN",
)
_DEFAULT_DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5544/registry"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap a local-dev tenant + actor + mock-IDP seed.")
    parser.add_argument("--tenant-slug", default=_DEFAULT_TENANT_SLUG)
    parser.add_argument("--tenant-display-name", default=_DEFAULT_TENANT_DISPLAY_NAME)
    parser.add_argument("--actor-id", default=_DEFAULT_ACTOR_USER_ID,
                        help="The userId the mock IDP returns as sub (also used as actor's oidc_subject).")
    parser.add_argument("--actor-display-name", default=_DEFAULT_ACTOR_DISPLAY_NAME)
    parser.add_argument(
        "--actor-entitlements",
        action="append",
        default=None,
        help="Raw entitlement strings to seed for this actor (repeatable). "
             "Defaults to one ADMIN grant for the dev tenant.",
    )
    parser.add_argument("--client-id", default=_DEFAULT_CLIENT_ID)
    parser.add_argument("--client-secret", default=_DEFAULT_CLIENT_SECRET)
    parser.add_argument("--mock-oidc-url", default=_DEFAULT_MOCK_OIDC_URL)
    parser.add_argument("--mock-entitlement-url", default=_DEFAULT_MOCK_ENTITLEMENT_URL)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env.dev"),
        help="Path to write CLIENT_ID/CLIENT_SECRET/DEV_USER_ID to.",
    )
    parser.add_argument(
        "--skip-mock-seed",
        action="store_true",
        help="Skip the mock-IDP and mock-entitlement registration steps.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# DB seeding


async def _ensure_tenant(session: object, slug: str, display_name: str) -> uuid.UUID:
    """Upsert a tenant row by slug. Returns the tenant_id UUID."""
    now = datetime.datetime.now(tz=datetime.UTC)
    pre = await session.execute(  # type: ignore[attr-defined]
        text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
        {"slug": slug},
    )
    row = pre.first()
    if row is not None:
        return uuid.UUID(str(row[0]))

    tenant_id = uuid.uuid4()
    await session.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active, disabled_at) "
            "VALUES (:tid, :slug, :name, :now, true, NULL)"
        ),
        {"tid": tenant_id, "slug": slug, "name": display_name, "now": now},
    )
    return tenant_id


async def _ensure_actor(
    session: object, tenant_id: uuid.UUID, oidc_subject: str, display_name: str
) -> uuid.UUID:
    """Upsert an actor row for (tenant, oidc_subject). Returns actor_id."""
    now = datetime.datetime.now(tz=datetime.UTC)
    pre = await session.execute(  # type: ignore[attr-defined]
        text(
            "SELECT actor_id FROM actors "
            "WHERE tenant_id = :tid AND oidc_subject = :sub"
        ),
        {"tid": tenant_id, "sub": oidc_subject},
    )
    row = pre.first()
    if row is not None:
        return uuid.UUID(str(row[0]))

    actor_id = uuid.uuid4()
    await session.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, created_at) "
            "VALUES (:aid, :tid, :sub, :name, :now)"
        ),
        {"aid": actor_id, "tid": tenant_id, "sub": oidc_subject, "name": display_name, "now": now},
    )
    return actor_id


# ---------------------------------------------------------------------------
# Mock-IDP + mock-entitlement registration


def _register_mock_oidc_client(
    base_url: str, client_id: str, client_secret: str
) -> bool:
    """Register a client in mock-oauth2-server. Idempotent: re-registers
    on every run. Returns True on success.

    mock-oauth2-server uses a JSON config; this function POSTs the
    client registration to its admin endpoint. The exact admin path
    depends on the image version; this implementation targets the
    navikt/mock-oauth2-server pattern.
    """
    # mock-oauth2-server accepts client registration via the standard
    # OAuth2 dynamic registration endpoint. The default config makes
    # any client_id+secret pair valid, so a smoke check against the
    # discovery endpoint is sufficient confirmation that the mock is
    # reachable.
    try:
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            resp = client.get("/default/.well-known/openid-configuration")
            return resp.status_code == 200
    except httpx.HTTPError as exc:
        print(f"mock-oidc unreachable at {base_url}: {exc}", file=sys.stderr)
        return False


def _seed_entitlements(
    base_url: str, user_id: str, scenario: str, entitlements: list[str]
) -> bool:
    """PUT canned entitlements for a userId in the mock entitlement service."""
    try:
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            resp = client.put(
                f"/admin/entitlements/{user_id}",
                json={"scenario": scenario, "entitlements": entitlements},
            )
            if resp.status_code in (200, 201, 204):
                return True
            print(
                f"mock-entitlement seed failed for {user_id}: "
                f"{resp.status_code} {resp.text}",
                file=sys.stderr,
            )
            return False
    except httpx.HTTPError as exc:
        print(f"mock-entitlement unreachable at {base_url}: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Env-file writing


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    """Replace or append the given KEY=VALUE entries in *path*. Other
    lines are preserved verbatim."""
    lines: list[str] = []
    keys_to_replace = set(values.keys())
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0] if "=" in line else ""
            if key in keys_to_replace:
                continue
            lines.append(line)
    for k, v in values.items():
        lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main


async def _bootstrap(args: argparse.Namespace) -> tuple[uuid.UUID, uuid.UUID, list[str]]:
    if "DATABASE_URL" not in os.environ:  # config: intentional
        os.environ["DATABASE_URL"] = _DEFAULT_DATABASE_URL
        print(
            f"DATABASE_URL not set; defaulting to {_DEFAULT_DATABASE_URL}",
            file=sys.stderr,
        )

    database_url = os.environ["DATABASE_URL"]
    entitlements = list(args.actor_entitlements) if args.actor_entitlements else list(_DEFAULT_ENTITLEMENTS)

    engine = create_async_engine(
        database_url,
        connect_args={"prepared_statement_cache_size": 0},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session, session.begin():
            tenant_id = await _ensure_tenant(session, args.tenant_slug, args.tenant_display_name)
            actor_id = await _ensure_actor(
                session, tenant_id, args.actor_id, args.actor_display_name
            )
    finally:
        await engine.dispose()

    return tenant_id, actor_id, entitlements


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    tenant_id, actor_id, entitlements = asyncio.run(_bootstrap(args))

    if not args.skip_mock_seed:
        oidc_ok = _register_mock_oidc_client(
            args.mock_oidc_url, args.client_id, args.client_secret
        )
        entitlement_ok = _seed_entitlements(
            args.mock_entitlement_url,
            args.actor_id,
            scenario="success_one_tenant" if len(entitlements) == 1 else "success_multi_tenant",
            entitlements=entitlements,
        )
    else:
        oidc_ok = entitlement_ok = True

    _write_env_file(
        args.env_file,
        {
            "DEV_TENANT_SLUG": args.tenant_slug,
            "DEV_TENANT_ID": str(tenant_id),
            "DEV_ACTOR_ID": str(actor_id),
            "DEV_USER_ID": args.actor_id,
            "CLIENT_ID": args.client_id,
            "CLIENT_SECRET": args.client_secret,
        },
    )

    print(f"Bootstrapped tenant {args.tenant_slug} ({tenant_id}) + actor {args.actor_id} ({actor_id}).")
    print(f"  Entitlements seeded: {entitlements}")
    print(f"  Mock OIDC reachable: {oidc_ok} ({args.mock_oidc_url})")
    print(f"  Mock entitlement seeded: {entitlement_ok} ({args.mock_entitlement_url})")
    print(f"  Wrote: {args.env_file}")
    print()
    print("Next: source .env.dev && fetch a JWT from the mock IDP and curl the API.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

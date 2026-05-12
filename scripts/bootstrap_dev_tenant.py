"""Bootstrap a local-development tenant + actor + API token.

Idempotent: re-running against the same database reuses the existing
tenant, actor, and role grants. Each run mints a *new* API token for the
target actor (so the operator can rotate by re-running).

This script is a developer-experience affordance. Production token
minting goes through ``scripts/mint_token.py`` (which expects the tenant
and actor to already exist). Production identity goes through OIDC when
``OIDC_DISCOVERY_URL`` is set.

Usage::

    python scripts/bootstrap_dev_tenant.py
    python scripts/bootstrap_dev_tenant.py --tenant-slug dev --actor-name dev-admin
    python scripts/bootstrap_dev_tenant.py --roles admin --roles producer
    python scripts/bootstrap_dev_tenant.py --write-env-file .env.dev
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import secrets
import sys
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token

_DEFAULT_TENANT_SLUG = "dev"
_DEFAULT_TENANT_DISPLAY_NAME = "Local Development Tenant"
_DEFAULT_ACTOR_NAME = "dev-admin"
_DEFAULT_ROLES: tuple[str, ...] = ("admin", "producer", "consumer", "auditor")
_TOKEN_ENV_VAR = "REGISTRY_DEV_TOKEN"

# Matches the docker-compose default in registry/docker-compose.yml.
# Used only when DATABASE_URL is not already set — pointing this script at a
# non-default database is a single `export DATABASE_URL=...` away.
_DOCKER_COMPOSE_DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5544/catalog"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap a dev tenant + actor + token.")
    parser.add_argument("--tenant-slug", default=_DEFAULT_TENANT_SLUG)
    parser.add_argument("--actor-name", default=_DEFAULT_ACTOR_NAME)
    parser.add_argument(
        "--roles",
        action="append",
        default=None,
        help="Role to grant (repeatable). Defaults to all four named roles.",
    )
    parser.add_argument(
        "--write-env-file",
        type=Path,
        default=None,
        help=f"Append/replace `{_TOKEN_ENV_VAR}=...` in the given file.",
    )
    return parser.parse_args(argv)


def _ansi(text: str, code: str, tty: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if tty else text


async def _ensure_tenant(session: object, slug: str) -> uuid.UUID:
    """Return the tenant UUID for *slug*, inserting a new row if missing."""
    now = datetime.datetime.now(tz=datetime.UTC)
    result = await session.execute(  # type: ignore[attr-defined]
        text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
        {"slug": slug},
    )
    row = result.first()
    if row is not None:
        return uuid.UUID(str(row[0]))

    tenant_id = uuid.uuid4()
    await session.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
            "VALUES (:tid, :slug, :name, :now, true)"
        ),
        {"tid": tenant_id, "slug": slug, "name": _DEFAULT_TENANT_DISPLAY_NAME, "now": now},
    )
    return tenant_id


async def _ensure_default_roles(session: object, tenant_id: uuid.UUID) -> None:
    """Seed the four named roles + the default rate_limits row. Idempotent."""
    now = datetime.datetime.now(tz=datetime.UTC)
    for name in _DEFAULT_ROLES:
        await session.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO roles (role_id, tenant_id, name, permissions, created_at) "
                "VALUES (gen_random_uuid(), :tid, :name, '{}', :now) "
                "ON CONFLICT (tenant_id, name) DO NOTHING"
            ),
            {"tid": tenant_id, "name": name, "now": now},
        )
    await session.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO rate_limits "
            "(limit_id, tenant_id, actor_id, reads_per_second, writes_per_second, created_at) "
            "VALUES (gen_random_uuid(), :tid, NULL, 100, 10, :now) "
            "ON CONFLICT DO NOTHING"
        ),
        {"tid": tenant_id, "now": now},
    )


async def _ensure_actor(session: object, tenant_id: uuid.UUID, display_name: str) -> uuid.UUID:
    """Return the actor UUID for (tenant, display_name), inserting a new row if missing."""
    now = datetime.datetime.now(tz=datetime.UTC)
    result = await session.execute(  # type: ignore[attr-defined]
        text("SELECT actor_id FROM actors " "WHERE tenant_id = :tid AND display_name = :name AND actor_kind = 'human'"),
        {"tid": tenant_id, "name": display_name},
    )
    row = result.first()
    if row is not None:
        return uuid.UUID(str(row[0]))

    actor_id = uuid.uuid4()
    await session.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO actors (actor_id, tenant_id, display_name, created_at, actor_kind) "
            "VALUES (:aid, :tid, :name, :now, 'human')"
        ),
        {"aid": actor_id, "tid": tenant_id, "name": display_name, "now": now},
    )
    return actor_id


async def _grant_roles(
    session: object,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    role_names: list[str],
) -> None:
    """Grant each named role to the actor. Idempotent — ON CONFLICT DO NOTHING."""
    now = datetime.datetime.now(tz=datetime.UTC)
    for name in role_names:
        await session.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO actor_roles (tenant_id, actor_id, role_id, granted_at, granted_by) "
                "SELECT :tid, :aid, role_id, :now, NULL "
                "FROM roles WHERE tenant_id = :tid AND name = :name "
                "ON CONFLICT DO NOTHING"
            ),
            {"tid": tenant_id, "aid": actor_id, "name": name, "now": now},
        )


async def _mint_token(
    session: object,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    role_names: list[str],
) -> str:
    """Mint and persist a fresh bearer token; return the plaintext."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_token(raw_token)
    now = datetime.datetime.now(tz=datetime.UTC)
    description = f"dev-bootstrap {now.date().isoformat()}"
    await session.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO api_tokens "
            "(token_id, tenant_id, actor_id, token_hash, roles, description, expires_at, created_at, revoked_at) "
            "VALUES (gen_random_uuid(), :tid, :aid, :hash, :roles, :desc, NULL, :now, NULL)"
        ),
        {
            "tid": tenant_id,
            "aid": actor_id,
            "hash": token_hash,
            "roles": role_names,
            "desc": description,
            "now": now,
        },
    )
    return raw_token


async def _bootstrap(args: argparse.Namespace) -> tuple[uuid.UUID, uuid.UUID, list[str], str]:
    from registry.config import get_settings  # noqa: PLC0415

    if "DATABASE_URL" not in os.environ:  # config: intentional
        os.environ["DATABASE_URL"] = _DOCKER_COMPOSE_DATABASE_URL
        print(
            f"DATABASE_URL not set; defaulting to docker-compose Postgres "
            f"({_DOCKER_COMPOSE_DATABASE_URL}). Export DATABASE_URL to override.",
            file=sys.stderr,
        )
    database_url = get_settings().database_url
    role_names = list(args.roles) if args.roles else list(_DEFAULT_ROLES)

    engine = create_async_engine(
        database_url,
        connect_args={"prepared_statement_cache_size": 0},  # PgBouncer transaction mode
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session, session.begin():
            tenant_id = await _ensure_tenant(session, args.tenant_slug)
            await _ensure_default_roles(session, tenant_id)
            actor_id = await _ensure_actor(session, tenant_id, args.actor_name)
            await _grant_roles(session, tenant_id, actor_id, role_names)
            raw_token = await _mint_token(session, tenant_id, actor_id, role_names)
    finally:
        await engine.dispose()

    return tenant_id, actor_id, role_names, raw_token


def _write_env_file(path: Path, token: str) -> None:
    """Replace or append a `REGISTRY_DEV_TOKEN=<token>` line in *path*."""
    lines: list[str] = []
    if path.exists():
        lines = [
            line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith(f"{_TOKEN_ENV_VAR}=")
        ]
    lines.append(f"{_TOKEN_ENV_VAR}={token}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _emit_summary(
    tenant_slug: str,
    tenant_id: uuid.UUID,
    actor_name: str,
    actor_id: uuid.UUID,
    role_names: list[str],
    token: str,
    env_file: Path | None,
) -> None:
    tty = sys.stdout.isatty()
    bold = lambda s: _ansi(s, "1", tty)  # noqa: E731
    cyan = lambda s: _ansi(s, "36", tty)  # noqa: E731
    dim = lambda s: _ansi(s, "2", tty)  # noqa: E731

    print(bold("Bootstrapped local-dev auth."))
    print(f"  {dim('Tenant ')}: {tenant_slug}  {dim('(' + str(tenant_id) + ')')}")
    print(f"  {dim('Actor  ')}: {actor_name}  {dim('(' + str(actor_id) + ')')}")
    print(f"  {dim('Roles  ')}: {', '.join(role_names)}")
    print(f"  {dim('Token  ')}: {cyan(token)}")
    print()
    print(bold("Use it:"))
    print(f"  curl -H 'Authorization: Bearer {token}' http://localhost:8000/v1/capabilities")
    print(f"  {dim('# or paste into /docs → Authorize → bearerAuth')}")
    if env_file is not None:
        print()
        print(f"{dim('Wrote')} {env_file}  {dim('(' + _TOKEN_ENV_VAR + '=...)')}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    tenant_id, actor_id, role_names, token = asyncio.run(_bootstrap(args))
    if args.write_env_file is not None:
        _write_env_file(args.write_env_file, token)
    _emit_summary(
        tenant_slug=args.tenant_slug,
        tenant_id=tenant_id,
        actor_name=args.actor_name,
        actor_id=actor_id,
        role_names=role_names,
        token=token,
        env_file=args.write_env_file,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

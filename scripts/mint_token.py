"""Mint a scoped API token (admin / break-glass CLI).

Generates a 32-byte random token, SHA-256 hashes it, inserts the hash into
`api_tokens`, and prints the plaintext token to stdout exactly once. The
plaintext is never persisted.

**This is the production / break-glass path** — it requires an existing
tenant UUID and actor UUID. For local development with no pre-existing
tenant, run `make dev-token` instead; it seeds the tenant + actor + roles
in one shot.

Usage:
    DATABASE_URL=postgresql+asyncpg://... \\
        python scripts/mint_token.py \\
            --tenant-id <uuid> --actor-id <uuid> \\
            --roles producer --roles auditor \\
            --description 'CI deploy token'
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import secrets
import sys
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.storage.models import ApiToken


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mint a scoped API token.")
    parser.add_argument("--tenant-id", required=True, help="Tenant UUID")
    parser.add_argument("--actor-id", required=True, help="Actor UUID")
    parser.add_argument(
        "--roles",
        action="append",
        default=[],
        help="Role to grant (repeatable). Common: consumer, producer, admin, auditor.",
    )
    parser.add_argument("--description", default=None, help="Human-readable description")
    parser.add_argument(
        "--expires-days",
        type=int,
        default=None,
        metavar="N",
        help="Number of days until token expiry (minimum 1); omit for non-expiring",
    )
    args = parser.parse_args(argv)
    if args.expires_days is not None and args.expires_days < 1:
        # 0 or negative would mint an already-expired token: the CLI exits 0
        # and prints the token, then every API call using it returns 401 —
        # a confusing double-failure in time-pressured incidents. Fail at
        # argument-parse time instead.
        parser.error("--expires-days must be a positive integer (minimum 1)")
    return args


async def _mint(args: argparse.Namespace) -> str:
    # Settings is the single env-var reader; raises a clear error
    # when DATABASE_URL is unset.
    from registry.config import get_settings  # noqa: PLC0415

    database_url = get_settings().database_url

    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_token(raw_token)
    expires_at = (
        datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=args.expires_days)
        if args.expires_days is not None
        else None
    )

    engine = create_async_engine(
        database_url,
        connect_args={"prepared_statement_cache_size": 0},  # required for PgBouncer transaction mode
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session, session.begin():
            session.add(
                ApiToken(
                    token_id=uuid.uuid4(),
                    tenant_id=uuid.UUID(args.tenant_id),
                    actor_id=uuid.UUID(args.actor_id),
                    token_hash=token_hash,
                    roles=args.roles,
                    description=args.description,
                    expires_at=expires_at,
                    created_at=datetime.datetime.now(tz=datetime.UTC),
                    revoked_at=None,
                )
            )
    finally:
        await engine.dispose()

    return raw_token


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        raw_token = asyncio.run(_mint(args))
    except KeyError as exc:
        if exc.args == ("DATABASE_URL",):
            print(
                "error: DATABASE_URL is not set.\n"
                "  This script mints tokens for an existing tenant + actor; it "
                "needs the database URL of the catalog instance to update.\n"
                "  For local development with no pre-existing tenant, run "
                "`make dev-token` instead.",
                file=sys.stderr,
            )
            return 2
        raise
    sys.stdout.write(raw_token + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

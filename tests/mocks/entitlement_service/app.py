"""Mock entitlement service — single-file FastAPI app with seedable scenarios.

Stands in for the enterprise LDAP-entitlements API in dev compose and
integration tests. Every scenario the registry needs to handle has a
named code path here, dispatched by the seeded scenario string for a
given userId.

Run standalone for compose: `uvicorn tests.mocks.entitlement_service.app:app --port 8081`.

Scenarios (matched against the seeded `scenario` field):

- `success_one_tenant`     — 200 + a single entitlement string
- `success_multi_tenant`   — 200 + multiple entitlement strings
- `empty`                  — 200 with `{"entitlements": []}`
- `disabled_tenant`        — 200 with an entitlement whose tenant slug is
                             expected to be operator-disabled in the
                             registry; the registry must drop the tuple
                             without re-creating
- `unknown_role`           — 200 with an entitlement whose role suffix is
                             not in the registry's role mapping
- `malformed`              — 200 with a non-JSON body
- `auth_rejected_401`      — 401 (upstream JWT rejection)
- `5xx`                    — 500
- `timeout`                — sleep longer than the registry's read timeout
                             (default 1500ms) before responding

Seed data is in-memory and resets on process restart — no persistence,
no external dependencies beyond FastAPI and uvicorn.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Sleep duration for the `timeout` scenario. Set well above the
# registry's default ENTITLEMENT_READ_TIMEOUT_MS (1500) so tests reliably
# observe the timeout failure mode. Ten seconds is enough for any
# reasonable test budget while keeping the mock responsive for other
# scenarios on a different userId.
TIMEOUT_SLEEP_SECONDS = 10.0

# Catalog of supported scenario names. Anything not in this set is
# rejected at seed time so typos surface immediately rather than as a
# silent mismatch at request time.
_KNOWN_SCENARIOS: frozenset[str] = frozenset(
    {
        "success_one_tenant",
        "success_multi_tenant",
        "empty",
        "disabled_tenant",
        "unknown_role",
        "malformed",
        "auth_rejected_401",
        "5xx",
        "timeout",
    }
)


class SeedRequest(BaseModel):
    scenario: str
    entitlements: list[str] = []


# In-memory seed store — userId → (scenario, entitlements). Reset on
# process restart. No locking: dev/integration use only, no concurrent
# writers expected within a single test.
_seed: dict[str, tuple[str, list[str]]] = {}


app = FastAPI(title="mock-entitlement-service")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.put("/admin/entitlements/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def seed_user(user_id: str, body: SeedRequest) -> Response:
    if body.scenario not in _KNOWN_SCENARIOS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown scenario {body.scenario!r}; valid: {sorted(_KNOWN_SCENARIOS)}",
        )
    _seed[user_id] = (body.scenario, list(body.entitlements))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.delete("/admin/entitlements/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def unseed_user(user_id: str) -> Response:
    _seed.pop(user_id, None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/v1/ldap-entitlements")
async def ldap_entitlements(userId: str, env: str) -> Any:  # noqa: N803 — match real API contract
    """Return entitlements for `userId` according to the seeded scenario.

    The `env` query param is accepted but not validated — real production
    services scope responses by environment; tests can vary it freely.
    Unseeded users get a 404 so test failures surface as missing-seed
    bugs rather than silent empty-entitlements behavior.
    """
    seeded = _seed.get(userId)
    if seeded is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no scenario seeded for userId {userId!r}; PUT /admin/entitlements/{{userId}} first",
        )
    scenario, entitlements = seeded

    if scenario == "success_one_tenant":
        return {"entitlements": entitlements}
    if scenario == "success_multi_tenant":
        return {"entitlements": entitlements}
    if scenario == "empty":
        return {"entitlements": []}
    if scenario == "disabled_tenant":
        # Same shape as success — the registry-side test seeds the tenants
        # row with disabled_at set so the dropping behavior is exercised.
        return {"entitlements": entitlements}
    if scenario == "unknown_role":
        return {"entitlements": entitlements}
    if scenario == "malformed":
        # Non-JSON body, but content-type still application/json so the
        # registry's parser is forced to handle the mismatch.
        return Response(content="not-json", media_type="application/json", status_code=200)
    if scenario == "auth_rejected_401":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="upstream rejected JWT")
    if scenario == "5xx":
        return JSONResponse(status_code=500, content={"detail": "simulated upstream error"})
    if scenario == "timeout":
        await asyncio.sleep(TIMEOUT_SLEEP_SECONDS)
        return {"entitlements": entitlements}
    # Unreachable: scenarios are validated at seed time, but defensive.
    raise HTTPException(  # pragma: no cover
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"unhandled scenario {scenario!r}",
    )


def reset_seed() -> None:
    """Test-only helper: clear all seeded users. Useful between tests
    that share the in-process app (no compose stack)."""
    _seed.clear()

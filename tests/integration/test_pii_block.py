"""Integration tests for PII scanner block policy + detection log.

Contract under test
----------------------------------------------------
Scenario: artifact body write with a credit card number in two configurations.

1. policy=block (via pii_patterns.policy_override='block' on credit_card pattern):
   POST /v1/capabilities/{id}/artifacts with a Luhn-valid credit card number in
   body → HTTP 422 with error.code == 'pii_blocked'; detection log row written.

2. policy=advisory (default, no override):
   POST with same body → HTTP 201 (write succeeds); matched_patterns present in
   the detection log row (advisory does not block).

3. No PII in body → HTTP 201; no detection log rows.

Uses a real Postgres container via the session-scoped ``pg_container`` fixture.
Each test creates its own tenant + token to avoid state leakage between tests.
"""

from __future__ import annotations

import datetime
import secrets
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.config import Settings
from registry.main import create_app
from registry.storage.models import Actor, ApiToken, Tenant

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

# Visa test number — passes Luhn, well-known in PCI test suites.
_VISA_TEST_CC = "4111111111111111"
_BODY_WITH_CC = f"Contact billing@example.com. Card on file: {_VISA_TEST_CC}."
_CLEAN_BODY = "This is a clean description with no sensitive data."

# Seeded for each test class so isolation is guaranteed.
_FACT_CATEGORY = "overview"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed(
    pg_url: str,
    *,
    slug: str,
    roles: list[str],
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Insert tenant + actor + API token. Returns (tenant_id, actor_id, raw_token)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw = secrets.token_urlsafe(24)
    try:
        async with factory() as session, session.begin():
            session.add(
                Tenant(
                    tenant_id=tenant_id,
                    slug=slug,
                    display_name=slug,
                    created_at=_NOW,
                    is_active=True,
                )
            )
            await session.flush()
            session.add(
                Actor(
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    display_name=f"actor-{slug}",
                    email=None,
                    oidc_subject=None,
                    created_at=_NOW,
                )
            )
            await session.flush()
            session.add(
                ApiToken(
                    token_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    token_hash=hash_token(raw),
                    roles=roles,
                    description=None,
                    expires_at=None,
                    created_at=_NOW,
                    revoked_at=None,
                )
            )
            # Seed required vocabulary values for the test tenant.
            for kind, value in [
                ("entity_type", "capability"),
                ("fact_category", "overview"),
                ("edge_rel", "depends_on"),
            ]:
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
                        "VALUES (:tid, :kind, :value, FALSE) ON CONFLICT DO NOTHING"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw


async def _seed_credit_card_block_policy(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> uuid.UUID:
    """Insert a pii_patterns row for credit_card with policy_override='block'.

    Returns the pattern_id of the inserted row.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    pattern_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO pii_patterns "
                    "(pattern_id, tenant_id, name, category, regex, is_system, "
                    " policy_override, is_enabled, created_at, created_by) "
                    "VALUES (:pid, :tid, 'credit_card', 'FINANCIAL', '__sentinel__', "
                    "        FALSE, 'block', TRUE, :now, :aid)"
                ),
                {
                    "pid": pattern_id,
                    "tid": tenant_id,
                    "now": _NOW,
                    "aid": actor_id,
                },
            )
    finally:
        await engine.dispose()
    return pattern_id


async def _count_detection_log(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    pattern_name: str,
) -> int:
    """Return number of pii_detection_log rows for this tenant + pattern_name."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM pii_detection_log " "WHERE tenant_id = :tid AND pattern_name = :pname"),
                {"tid": tenant_id, "pname": pattern_name},
            )
            row = result.one()
            return int(row[0])
    finally:
        await engine.dispose()


def _build_app(pg_url: str) -> TestClient:
    settings = Settings(
        database_url=pg_url,
        pgbouncer_url=pg_url,
        scheduler_jobstore_url=pg_url,
        embedding_model="stub",
        scheduler_use_memory_jobstore=True,
    )
    return create_app(settings)


# ---------------------------------------------------------------------------
# Tests — policy=block: credit card body → 422 + detection log row
# ---------------------------------------------------------------------------


class TestPiiBlockPolicy:
    @pytest.mark.asyncio
    async def test_credit_card_body_returns_422_when_block_policy(self, pg_container: str) -> None:
        """Artifact body containing a credit card + block policy → HTTP 422."""
        tenant_id, actor_id, raw = await _seed(
            pg_container,
            slug=f"pii-block-{uuid.uuid4().hex[:6]}",
            roles=["producer", "admin"],
        )
        await _seed_credit_card_block_policy(pg_container, tenant_id=tenant_id, actor_id=actor_id)

        app = _build_app(pg_container)
        with TestClient(app) as client:
            auth = {"Authorization": f"Bearer {raw}"}
            # Create parent capability.
            cap_r = client.post("/v1/capabilities", json={"name": "pii-cap"}, headers=auth)
            assert cap_r.status_code == 201, cap_r.text
            entity_id = cap_r.json()["entity_id"]

            # POST artifact with CC in body → must be rejected.
            art_r = client.post(
                f"/v1/capabilities/{entity_id}/artifacts",
                json={"category": _FACT_CATEGORY, "title": "PII test artifact", "body": _BODY_WITH_CC},
                headers=auth,
            )
            assert (
                art_r.status_code == 422
            ), f"Expected 422 from PII block policy, got {art_r.status_code}: {art_r.text}"
            body = art_r.json()
            # The global StarletteHTTPException handler coerces dict details
            # into the canonical envelope `{"errors": [{path, code, message}]}`.
            # The raised HTTPException carries a dict {error, message,
            # matched_patterns}; after coercion the message stringifies the
            # original dict, so the credit_card / pii_blocked markers should
            # appear inside the message body.
            errors = body.get("errors", [])
            assert errors, f"Expected `errors` in envelope; got {body}"
            message = str(errors[0].get("message", ""))
            assert "pii_blocked" in message, f"Expected 'pii_blocked' marker in message; got {message}"
            assert "credit_card" in message, f"Expected 'credit_card' marker in message; got {message}"

    @pytest.mark.asyncio
    async def test_credit_card_body_writes_detection_log_row(self, pg_container: str) -> None:
        """Blocked artifact write must insert a pii_detection_log row."""
        tenant_id, actor_id, raw = await _seed(
            pg_container,
            slug=f"pii-log-{uuid.uuid4().hex[:6]}",
            roles=["producer", "admin"],
        )
        await _seed_credit_card_block_policy(pg_container, tenant_id=tenant_id, actor_id=actor_id)

        app = _build_app(pg_container)
        with TestClient(app) as client:
            auth = {"Authorization": f"Bearer {raw}"}
            cap_r = client.post("/v1/capabilities", json={"name": "pii-log-cap"}, headers=auth)
            assert cap_r.status_code == 201, cap_r.text
            entity_id = cap_r.json()["entity_id"]

            # Trigger the block (422 expected).
            client.post(
                f"/v1/capabilities/{entity_id}/artifacts",
                json={"category": _FACT_CATEGORY, "title": "PII test artifact", "body": _BODY_WITH_CC},
                headers=auth,
            )

        # Detection log row must be present even though the write was blocked.
        count = await _count_detection_log(pg_container, tenant_id=tenant_id, pattern_name="credit_card")
        assert count >= 1, f"Expected at least 1 pii_detection_log row for credit_card, got {count}"


# ---------------------------------------------------------------------------
# Tests — advisory policy (default): CC body → 201 + detection log written
# ---------------------------------------------------------------------------


class TestPiiAdvisoryPolicy:
    @pytest.mark.asyncio
    async def test_credit_card_body_allowed_when_advisory(self, pg_container: str) -> None:
        """Artifact body with credit card and advisory policy (default) → HTTP 201."""
        tenant_id, actor_id, raw = await _seed(
            pg_container,
            slug=f"pii-advisory-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        # No block policy seeded — scanner defaults to advisory.

        app = _build_app(pg_container)
        with TestClient(app) as client:
            auth = {"Authorization": f"Bearer {raw}"}
            cap_r = client.post("/v1/capabilities", json={"name": "pii-advisory-cap"}, headers=auth)
            assert cap_r.status_code == 201, cap_r.text
            entity_id = cap_r.json()["entity_id"]

            art_r = client.post(
                f"/v1/capabilities/{entity_id}/artifacts",
                json={"category": _FACT_CATEGORY, "title": "PII test artifact", "body": _BODY_WITH_CC},
                headers=auth,
            )
            assert art_r.status_code == 201, f"Advisory policy must allow write, got {art_r.status_code}: {art_r.text}"
            assert art_r.json()["body"] == _BODY_WITH_CC

    @pytest.mark.asyncio
    async def test_advisory_write_still_logs_detection(self, pg_container: str) -> None:
        """Advisory write must still produce a detection log row (always-on logging)."""
        tenant_id, actor_id, raw = await _seed(
            pg_container,
            slug=f"pii-adv-log-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )

        app = _build_app(pg_container)
        with TestClient(app) as client:
            auth = {"Authorization": f"Bearer {raw}"}
            cap_r = client.post("/v1/capabilities", json={"name": "pii-adv-log-cap"}, headers=auth)
            assert cap_r.status_code == 201, cap_r.text
            entity_id = cap_r.json()["entity_id"]

            art_r = client.post(
                f"/v1/capabilities/{entity_id}/artifacts",
                json={"category": _FACT_CATEGORY, "title": "PII test artifact", "body": _BODY_WITH_CC},
                headers=auth,
            )
            assert art_r.status_code == 201, art_r.text

        count = await _count_detection_log(pg_container, tenant_id=tenant_id, pattern_name="credit_card")
        assert count >= 1, f"Advisory write must still log detection; got {count} rows for credit_card"


# ---------------------------------------------------------------------------
# Tests — clean body: no PII → no detection log rows
# ---------------------------------------------------------------------------


class TestPiiCleanBody:
    @pytest.mark.asyncio
    async def test_clean_body_returns_201_no_detection_log(self, pg_container: str) -> None:
        """Artifact body with no PII → 201 and zero detection log rows."""
        tenant_id, actor_id, raw = await _seed(
            pg_container,
            slug=f"pii-clean-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )

        app = _build_app(pg_container)
        with TestClient(app) as client:
            auth = {"Authorization": f"Bearer {raw}"}
            cap_r = client.post("/v1/capabilities", json={"name": "pii-clean-cap"}, headers=auth)
            assert cap_r.status_code == 201, cap_r.text
            entity_id = cap_r.json()["entity_id"]

            art_r = client.post(
                f"/v1/capabilities/{entity_id}/artifacts",
                json={"category": _FACT_CATEGORY, "title": "PII clean artifact", "body": _CLEAN_BODY},
                headers=auth,
            )
            assert art_r.status_code == 201, art_r.text

        count = await _count_detection_log(pg_container, tenant_id=tenant_id, pattern_name="credit_card")
        assert count == 0, f"Clean body must produce no detection log rows, got {count}"


# ---------------------------------------------------------------------------
# Tests — PRD eval metric 21: precision ≥ 90%, recall ≥ 80%
# on 100-string curated fixture (50 PII + 50 negatives)
# ---------------------------------------------------------------------------


class TestPiiPrecisionRecall:
    """Hand-curated 100-string fixture: 50 strings containing PII, 50 negatives.

    Tests both precision (of detected positives, how many are true positives)
    and recall (of all positives, how many were detected).

    Uses the build_builtin_scanner() factory directly — no DB required.
    SLO: precision ≥ 90%, recall ≥ 80%.
    """

    # 50 positive samples — each contains at least one PII element from a built-in category.
    _POSITIVES = [
        # Email
        "Contact alice@example.com for support.",
        "Send reports to billing@company.co.uk",
        "user+tag@subdomain.org is my address.",
        "My backup: admin@localhost",
        "Reach me at test.user@mail.example",
        # Phone (E.164 / US format)
        "Call us at +1-800-555-0100",
        "Phone: (555) 867-5309",
        "Fax: 415.555.2671",
        "Emergency: +44 20 7946 0958",
        "Mobile: +1 (212) 555-0147",
        # SSN
        "SSN on file: 123-45-6789",
        "Tax ID: 987-65-4321",
        "Social: 456-78-9012",
        "Employee SSN: 234-56-7890",
        "Verification SSN: 345-67-8901",
        # AWS access key
        "Key: AKIAIOSFODNN7EXAMPLE",
        "AWS_ACCESS_KEY_ID=AKIAJOE26PNHE7ZFXNBQ",
        "export AWS_ACCESS_KEY=AKIAI3MOAZQNB7AQJYQ7",
        "Credentials: access_key=AKIAZ5BSYM5WDKW23XNQ",
        "boto3 key: AKIATMZMCUWJKRK3JHAZ",
        # AWS secret key (high-entropy 40-char strings)
        "Secret: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "AWS_SECRET_ACCESS_KEY=8T4hYuEQ2OHCvZ5w3K7aLpMnBxJdFgNsR1iUoP",
        "secret_key: v3+Hk/9TnPqW2mZgXbCyEaFdLrJuOsNMIYVi1h0",
        "export SECRET=Q8wEaRtYuIoPaSdFgHjKlZxCvBnMq2W4e6T1y3",
        "key_secret='AbCdEfGhIjKlMnOpQrStUvWx12345678YzABCD12'",
        # JWT
        "token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        "Bearer eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJodHRwczovL2V4YW1wbGUuY29tIn0.RSASHA256sig",
        "JWT: eyJhbGciOiJub25lIn0.eyJ1c2VyIjoiYWRtaW4ifQ.",
        "auth_token=eyJhbGciOiJIUzM4NCJ9.eyJkYXRhIjoidGVzdCJ9.HMACSHA384signature",
        "Set-Cookie: session=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6MX0.abc123",
        # Credit card (Luhn-valid)
        "Card: 4111111111111111",
        "Visa: 4532015112830366",
        "Mastercard: 5500005555555559",
        "Amex: 378282246310005",
        "Discover: 6011111111111117",
        "Card number: 4111 1111 1111 1111",
        "Payment: 4000056655665556",
        "CC: 5425233430109903",
        "Billing card: 4012888888881881",
        "Charge to: 371449635398431",
        # Mixed / multi-pattern
        "User alice@example.com, SSN: 123-45-6789, card 4111111111111111",
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE, secret=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "Contact +1-800-555-0100 or email info@company.com",
        "Deploy key AKIAJOE26PNHE7ZFXNBQ, billing card 5500005555555559",
        "SSN 987-65-4321, JWT eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        "Emergency contact: (415) 555-0101, email: emergency@example.org",
        "Invoice #1234, card: 4111111111111111, billed to alice@example.com",
        "CI secret AKIAZ5BSYM5WDKW23XNQ for pipeline runner user@corp.io",
        "OAuth: eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJodHRwczovL2V4YW1wbGUuY29tIn0.RSASHA256sig",
        "Refund for card 378282246310005 to customer@email.net",
        "Service account secret: Q8wEaRtYuIoPaSdFgHjKlZxCvBnMq2W4e6T1y3",
    ]

    # 50 negative samples — realistic developer-pasted content with NO valid PII.
    _NEGATIVES = [
        # Version strings / semver
        "Requires package >= 2.0.0, < 3.0.0",
        "Bump dependency from 1.4.2 to 1.5.0",
        "Semantic version: 12.0.0-alpha.1+20130313144700",
        "npm install --save package@^1.4.0",
        "chart version: 3.14.159265",
        # Order / invoice / SKU IDs
        "Order ID: 12345678901234",
        "Invoice #INV-2026-001234",
        "SKU: AB-1234567-XL",
        "Tracking: 1Z999AA10123456784",
        "Reference: REF-00987654321",
        # Code / config snippets (no PII)
        "DATABASE_URL=postgresql://localhost:5432/mydb",
        "REDIS_URL=redis://127.0.0.1:6379/0",
        "export NODE_ENV=production",
        "const MAX_RETRY = 5;",
        "SELECT count(*) FROM entities WHERE tenant_id = :tid;",
        # UUIDs / random hex (not PII)
        "entity_id: 550e8400-e29b-41d4-a716-446655440000",
        "run_id = deadbeef-1234-5678-abcd-ef0123456789",
        "sha256: 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "commit: a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        "md5: d41d8cd98f00b204e9800998ecf8427e",
        # Technical documentation
        "The CTE traversal uses depth-first search up to max depth 5.",
        "Edge types: depends_on, requires, composes, provides_to, conflicts_with",
        "Bi-temporal columns: t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at",
        "Run `pytest tests/ -q --timeout=120` to execute the test suite.",
        "HTTP method mode is configurable via REGISTRY_HTTP_METHODS_MODE env var.",
        # Dates / timestamps
        "Created at: 2026-01-01T00:00:00Z",
        "Report date: 2025-12-31 23:59:59+00:00",
        "Retention window: 90 days from 2026-05-10",
        "Next review: Q3 2027",
        "Scheduled for 2026-06-01",
        # Numbers that look like card numbers but fail Luhn
        "Transaction: 4111111111111110",  # Luhn-invalid (off by 1)
        "Reference: 5500005555555550",  # Luhn-invalid
        "Ticket #: 3782822463100050",  # wrong length
        "Batch: 6011111111111110",  # Luhn-invalid
        "Serial: 4000-0000-0000-0001",  # Luhn-invalid
        # Placeholder / example values
        "Name: John Doe",
        "Address: 123 Main Street, Springfield",
        "Company: Acme Corp.",
        "Username: developer42",
        "Role: capability-fabric-admin",
        # Lorem ipsum / generic prose
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
        "Sed ut perspiciatis unde omnis iste natus error sit voluptatem.",
        "Nemo enim ipsam voluptatem quia voluptas sit aspernatur.",
        "At vero eos et accusamus et iusto odio dignissimos ducimus.",
        "Temporibus autem quibusdam et aut officiis debitis et rerum necessitatibus.",
        # Error messages / log lines
        "ERROR: relation 'old_table' does not exist",
        "WARN: cache miss for entity_id=abc123, falling back to CTE",
        "INFO: worker processed 47 closure_outbox rows in 0.32s",
        "DEBUG: PII scanner: 0 matches found in 128 chars",
        "CRITICAL: alembic downgrade failed; rollback required",
    ]

    def test_pii_precision_and_recall(self) -> None:
        """Built-in scanner achieves ≥ 90% precision and ≥ 80% recall on 100-string fixture.

        Precision = TP / (TP + FP)  — of detected positives, how many are true positives.
        Recall    = TP / (TP + FN)  — of all positives, how many were detected.
        """
        from registry.security.pii_scanner import build_builtin_scanner  # noqa: PLC0415

        scanner = build_builtin_scanner(tenant_policy="advisory")

        tp = 0  # positive string detected (at least one match)
        fn = 0  # positive string NOT detected (false negative)
        fp = 0  # negative string incorrectly detected (false positive)
        tn = 0  # negative string correctly passed through

        for positive in self._POSITIVES:
            resp = scanner.scan(
                positive,
                field_type="test.body",
                pattern_overrides={},
                field_policies={},
            )
            if resp.matched_patterns:
                tp += 1
            else:
                fn += 1

        for negative in self._NEGATIVES:
            resp = scanner.scan(
                negative,
                field_type="test.body",
                pattern_overrides={},
                field_policies={},
            )
            if resp.matched_patterns:
                fp += 1
            else:
                tn += 1

        total_positives = len(self._POSITIVES)
        len(self._NEGATIVES)

        recall = tp / total_positives if total_positives > 0 else 0.0
        precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0

        assert recall >= 0.80, (
            f"PII recall {recall:.1%} below 80% threshold. "
            f"TP={tp}, FN={fn} (missed positives follow):\n"
            + "\n".join(
                f"  [{i}] {s!r}"
                for i, s in enumerate(self._POSITIVES)
                if not scanner.scan(s, field_type="test.body", pattern_overrides={}, field_policies={}).matched_patterns
            )
        )

        assert precision >= 0.90, (
            f"PII precision {precision:.1%} below 90% threshold. "
            f"TP={tp}, FP={fp} (false positives follow):\n"
            + "\n".join(
                f"  [{i}] {s!r}"
                for i, s in enumerate(self._NEGATIVES)
                if scanner.scan(s, field_type="test.body", pattern_overrides={}, field_policies={}).matched_patterns
            )
        )

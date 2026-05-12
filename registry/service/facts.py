"""FactService — bi-temporal fact writes plus full-capability aggregate read.

Owns: `create_fact`, `update_fact`, `delete_fact`, `create_fact_from_sync`,
`upsert_synced_facts`, `get_full_capability`.

`get_full_capability` lives here because assembling the capability record
requires fetching both attribute rows (entity-layer) and fact rows. The
EntityService is injected so the entity lookup goes through the canonical
path (tenant assertion included).

The embedding outbox insert is wrapped in a SAVEPOINT: if the migration
that creates `embedding_outbox` hasn't been applied yet the SAVEPOINT rolls
back cleanly without poisoning the outer transaction.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import sqlalchemy.exc
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.exceptions import NotFoundError
from registry.service.embedding_drain import make_chunk_plan
from registry.service.entity import EntityService, _entity_to_ref
from registry.service.temporal import build_as_of_filter_sql, build_current_filter_sql, normalize_utc
from registry.service.vocabulary import VocabularyService
from registry.storage.models import Attribute, Edge, Entity, Fact
from registry.types import (
    CapabilityRecord,
    Clock,
    EdgeRef,
    FactRef,
    SyncWriteResult,
    TenantContext,
)

_log = logging.getLogger(__name__)


def _fact_to_ref(f: Fact) -> FactRef:
    return FactRef(
        fact_id=f.fact_id,
        tenant_id=f.tenant_id,
        entity_id=f.entity_id,
        category=f.category,
        body=f.body,
        is_authoritative=f.is_authoritative,
        is_authoritative_superseded=f.is_authoritative_superseded,
        sync_run_id=f.sync_run_id,
        t_valid_from=f.t_valid_from,
        t_valid_to=f.t_valid_to,
        t_ingested_at=f.t_ingested_at,
        t_invalidated_at=f.t_invalidated_at,
        title=f.title,
        body_format=f.body_format,
        created_by=f.created_by,
    )


def _edge_to_ref(e: Edge) -> EdgeRef:
    return EdgeRef(
        edge_id=e.edge_id,
        tenant_id=e.tenant_id,
        src_entity_id=e.src_entity_id,
        rel=e.rel,
        dst_entity_id=e.dst_entity_id,
        properties=e.properties,
        t_valid_from=e.t_valid_from,
        t_valid_to=e.t_valid_to,
        t_ingested_at=e.t_ingested_at,
        t_invalidated_at=e.t_invalidated_at,
    )


class FactService:
    """Focused service for fact writes and capability-record assembly.

    Owns: `create_fact`, `update_fact`, `delete_fact`, `create_fact_from_sync`,
    `upsert_synced_facts`, `get_full_capability`.

    Requires an `EntityService` for entity lookups used inside
    `get_full_capability` (DI, not inheritance, to keep the dependency
    direction explicit and avoid circular imports).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        vocabulary: VocabularyService,
        entity_service: EntityService,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._vocabulary = vocabulary
        self._entity = entity_service

    # ---- fact CRUD --------------------------------------------------------

    async def create_fact(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        category: str,
        body: str,
        valid_from: Any = None,
        is_authoritative: bool = True,
        sync_run_id: uuid.UUID | None = None,
        title: str | None = None,
        body_format: str = "markdown",
    ) -> FactRef:
        from registry.service.slugs import validate_artifact_title, validate_body_format  # noqa: PLC0415

        if title is not None:
            validate_artifact_title(title)
        validate_body_format(body_format)
        await self._vocabulary.validate_value(ctx, "fact_category", category)

        now = self._clock.now()
        valid_from = normalize_utc(valid_from) if valid_from is not None else now
        fact = Fact(
            fact_id=uuid.uuid4(),
            tenant_id=ctx.tenant_id,
            entity_id=entity_id,
            category=category,
            title=title,
            body=body,
            body_format=body_format,
            is_authoritative=is_authoritative,
            is_authoritative_superseded=False,
            sync_run_id=sync_run_id,
            t_valid_from=valid_from,
            t_valid_to=None,
            t_ingested_at=now,
            t_invalidated_at=None,
            created_by=ctx.actor_id,
        )

        async with self._session_factory() as session, session.begin():
            entity = await session.get(Entity, entity_id)
            if entity is None or entity.tenant_id != ctx.tenant_id:
                msg = f"entity {entity_id} not found for tenant"
                raise NotFoundError(msg)
            session.add(fact)
            await session.flush()
            await self._enqueue_embedding(session, ctx, fact.fact_id, body)

        return _fact_to_ref(fact)

    async def update_fact(
        self,
        ctx: TenantContext,
        fact_id: uuid.UUID,
        new_body: str,
        valid_from: Any = None,
    ) -> FactRef:
        now = self._clock.now()
        valid_from = normalize_utc(valid_from) if valid_from is not None else now

        async with self._session_factory() as session, session.begin():
            old = await session.get(Fact, fact_id)
            if old is None:
                msg = f"fact {fact_id} not found"
                raise NotFoundError(msg)
            self._entity._assert_tenant(ctx, old.tenant_id)
            old.t_valid_to = now
            new = Fact(
                fact_id=uuid.uuid4(),
                tenant_id=ctx.tenant_id,
                entity_id=old.entity_id,
                category=old.category,
                body=new_body,
                is_authoritative=old.is_authoritative,
                is_authoritative_superseded=False,
                sync_run_id=old.sync_run_id,
                t_valid_from=valid_from,
                t_valid_to=None,
                t_ingested_at=now,
                t_invalidated_at=None,
                created_by=ctx.actor_id,
            )
            session.add(new)
            await session.flush()
            await self._enqueue_embedding(session, ctx, new.fact_id, new_body)

        return _fact_to_ref(new)

    async def delete_fact(self, ctx: TenantContext, fact_id: uuid.UUID) -> None:
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            fact = await session.get(Fact, fact_id)
            if fact is None:
                msg = f"fact {fact_id} not found"
                raise NotFoundError(msg)
            self._entity._assert_tenant(ctx, fact.tenant_id)
            fact.t_valid_to = now
            fact.t_invalidated_at = now

    async def create_fact_from_sync(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        category: str,
        body: str,
        sync_run_id: uuid.UUID,
        source_id: uuid.UUID,
        valid_from: Any = None,
    ) -> FactRef:
        """Write one synced fact applying the authoritative-wins conflict policy.

        Before inserting, checks whether an active authoritative fact already exists
        for ``(tenant_id, entity_id, category)``:

        - If an authoritative fact exists → insert the new row with
          ``is_authoritative=False, is_authoritative_superseded=True``.
          The existing authoritative row is left intact; the synced fact records
          that it was superseded at write time.
        - Otherwise → insert as a non-authoritative synced fact
          (``is_authoritative=False, is_authoritative_superseded=False``)
          and bi-temporally supersede any prior open non-authoritative row for
          the same ``(tenant_id, entity_id, category)`` by closing its
          ``t_invalidated_at``.

        The ``embedding_outbox`` insert happens in the same transaction
        (outbox pattern: atomic with the fact row).
        """
        await self._vocabulary.validate_value(ctx, "fact_category", category)

        now = self._clock.now()
        valid_from = normalize_utc(valid_from) if valid_from is not None else now

        async with self._session_factory() as session, session.begin():
            entity = await session.get(Entity, entity_id)
            if entity is None or entity.tenant_id != ctx.tenant_id:
                msg = f"entity {entity_id} not found for tenant"
                raise NotFoundError(msg)

            # --- conflict check: authoritative-wins policy ------------------
            # Is there a live authoritative fact for this (tenant, entity, category)?
            auth_result = await session.execute(
                select(Fact).where(
                    Fact.tenant_id == ctx.tenant_id,
                    Fact.entity_id == entity_id,
                    Fact.category == category,
                    Fact.is_authoritative.is_(True),
                    Fact.t_invalidated_at.is_(None),
                )
            )
            existing_auth = auth_result.scalar_one_or_none()

            superseded_by_authoritative = existing_auth is not None

            if not superseded_by_authoritative:
                # Bi-temporal supersession: close any open non-authoritative row
                # for the same (tenant, entity, category) so only one open row
                # exists per dimension.
                prev_result = await session.execute(
                    select(Fact).where(
                        Fact.tenant_id == ctx.tenant_id,
                        Fact.entity_id == entity_id,
                        Fact.category == category,
                        Fact.is_authoritative.is_(False),
                        Fact.t_invalidated_at.is_(None),
                        Fact.t_valid_to.is_(None),
                    )
                )
                prev = prev_result.scalar_one_or_none()
                if prev is not None:
                    prev.t_valid_to = now
                    prev.t_invalidated_at = now

            fact = Fact(
                fact_id=uuid.uuid4(),
                tenant_id=ctx.tenant_id,
                entity_id=entity_id,
                category=category,
                body=body,
                is_authoritative=False,
                is_authoritative_superseded=superseded_by_authoritative,
                sync_run_id=sync_run_id,
                t_valid_from=valid_from,
                t_valid_to=None,
                t_ingested_at=now,
                t_invalidated_at=None,
                created_by=ctx.actor_id,
            )
            session.add(fact)
            await session.flush()
            await self._enqueue_embedding(session, ctx, fact.fact_id, body)

        _log.info(
            "sync fact written fact_id=%s entity=%s category=%s superseded_by_auth=%s source=%s",
            fact.fact_id,
            entity_id,
            category,
            superseded_by_authoritative,
            source_id,
        )
        return _fact_to_ref(fact)

    async def upsert_synced_facts(
        self,
        ctx: TenantContext,
        facts: list[Any],
        sync_run_id: uuid.UUID,
        source: Any,
    ) -> SyncWriteResult:
        """Apply the authoritative-wins conflict policy for a batch of ``ParsedFact`` objects.

        Issues O(1) transactions per call regardless of batch size:
          1. One vocabulary validation call per unique category (CPU, no extra DB round-trip
             beyond what the VocabularyService already issues).
          2. One SELECT to confirm all entity_ids belong to this tenant.
          3. One SELECT to find which (entity_id, category) pairs already have a live
             authoritative fact.
          4. One SELECT to find open non-authoritative rows that must be closed.
          5. One bulk UPDATE to close those prior rows.
          6. One bulk INSERT for the new fact rows.
          7. One bulk INSERT into embedding_outbox (wrapped in a SAVEPOINT so a
             missing table does not abort the outer transaction).

        All seven steps share a single transaction and a single commit.

        Conflict semantics (authoritative-wins policy):
          - Pair already has a live authoritative fact → new row written with
            ``is_authoritative_superseded=True``; the prior non-auth row (if any) is
            left open.
          - No authoritative blocker → new row written with
            ``is_authoritative_superseded=False``; the prior open non-auth row (if any)
            is closed (bi-temporal supersession).

        Unknown entity_id: fails the whole batch with ``NotFoundError`` before any
        writes, matching the per-row behavior.

        ``source`` is the ``SyncSource`` ORM row (or any object with a
        ``.source_id`` UUID attribute).
        """
        if not facts:
            return SyncWriteResult(created=0, skipped=0, superseded=0)

        # --- 1. Validate categories (deduplicated — one DB round-trip per unique value) -
        unique_categories: set[str] = {pf.category for pf in facts}
        for category in unique_categories:
            await self._vocabulary.validate_value(ctx, "fact_category", category)

        now = self._clock.now()

        # --- Collect distinct entity_ids from the batch -----------------------
        distinct_entity_ids: list[uuid.UUID] = list({pf.entity_id for pf in facts})

        async with self._session_factory() as session, session.begin():
            # --- 2. Bulk entity existence check --------------------------------
            entity_result = await session.execute(
                select(Entity.entity_id).where(
                    Entity.entity_id.in_(distinct_entity_ids),
                    Entity.tenant_id == ctx.tenant_id,
                )
            )
            found_ids: set[uuid.UUID] = {row[0] for row in entity_result}
            missing = set(distinct_entity_ids) - found_ids
            if missing:
                first_missing = next(iter(missing))
                msg = f"entity {first_missing} not found for tenant"
                raise NotFoundError(msg)

            # --- 3. Bulk authoritative-conflict check --------------------------
            # Build a set of (entity_id, category) pairs that have a live
            # authoritative fact. A live authoritative fact means: is_authoritative=True
            # AND t_invalidated_at IS NULL.
            # Using a VALUES-list subquery avoids N individual predicates.
            #
            # PostgreSQL supports unnest with array literals for multi-column
            # membership checks efficiently. We use a text CTE for portability
            # with the SQLAlchemy text() layer already used in _enqueue_embedding.
            entity_ids_arr = [str(pf.entity_id) for pf in facts]
            categories_arr = [pf.category for pf in facts]

            auth_check_result = await session.execute(
                text(
                    "SELECT DISTINCT entity_id, category FROM facts "
                    "WHERE tenant_id = :tid "
                    "  AND is_authoritative = TRUE "
                    "  AND t_invalidated_at IS NULL "
                    "  AND (entity_id, category) IN "
                    "      (SELECT unnest(CAST(:eids AS uuid[])), unnest(CAST(:cats AS text[])))"
                ),
                {
                    "tid": ctx.tenant_id,
                    "eids": entity_ids_arr,
                    "cats": categories_arr,
                },
            )
            # Set of (entity_id_str, category) pairs blocked by an authoritative fact.
            auth_blocked: set[tuple[str, str]] = {(str(row[0]), row[1]) for row in auth_check_result}

            # --- 4. Bulk prior-open non-auth row check -------------------------
            # Only need to find prior rows for pairs NOT blocked by an authoritative fact.
            unblocked_eids = [str(pf.entity_id) for pf in facts if (str(pf.entity_id), pf.category) not in auth_blocked]
            unblocked_cats = [pf.category for pf in facts if (str(pf.entity_id), pf.category) not in auth_blocked]

            prior_fact_ids: list[uuid.UUID] = []
            if unblocked_eids:
                prior_result = await session.execute(
                    text(
                        "SELECT fact_id FROM facts "
                        "WHERE tenant_id = :tid "
                        "  AND is_authoritative = FALSE "
                        "  AND t_invalidated_at IS NULL "
                        "  AND t_valid_to IS NULL "
                        "  AND (entity_id, category) IN "
                        "      (SELECT unnest(CAST(:eids AS uuid[])), unnest(CAST(:cats AS text[])))"
                    ),
                    {
                        "tid": ctx.tenant_id,
                        "eids": unblocked_eids,
                        "cats": unblocked_cats,
                    },
                )
                prior_fact_ids = [row[0] for row in prior_result]

            # --- 5. Bulk UPDATE: close prior open non-auth rows ----------------
            if prior_fact_ids:
                await session.execute(
                    update(Fact).where(Fact.fact_id.in_(prior_fact_ids)).values(t_valid_to=now, t_invalidated_at=now)
                )

            # --- 6. Bulk INSERT new fact rows -----------------------------------
            created = 0
            superseded = 0
            new_facts: list[dict[str, Any]] = []
            for pf in facts:
                key = (str(pf.entity_id), pf.category)
                is_superseded = key in auth_blocked
                valid_from = normalize_utc(pf.valid_from) if pf.valid_from is not None else now
                new_facts.append(
                    {
                        "fact_id": uuid.uuid4(),
                        "tenant_id": ctx.tenant_id,
                        "entity_id": pf.entity_id,
                        "category": pf.category,
                        "body": pf.body,
                        "is_authoritative": False,
                        "is_authoritative_superseded": is_superseded,
                        "sync_run_id": sync_run_id,
                        "t_valid_from": valid_from,
                        "t_valid_to": None,
                        "t_ingested_at": now,
                        "t_invalidated_at": None,
                        "created_by": ctx.actor_id,
                    }
                )
                if is_superseded:
                    superseded += 1
                else:
                    created += 1

            if new_facts:
                await session.execute(
                    text(
                        "INSERT INTO facts "
                        "(fact_id, tenant_id, entity_id, category, body, "
                        " is_authoritative, is_authoritative_superseded, sync_run_id, "
                        " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at, created_by) "
                        "SELECT "
                        "  unnest(CAST(:fact_ids AS uuid[])), "
                        "  unnest(CAST(:tenant_ids AS uuid[])), "
                        "  unnest(CAST(:entity_ids AS uuid[])), "
                        "  unnest(CAST(:categories AS text[])), "
                        "  unnest(CAST(:bodies AS text[])), "
                        "  unnest(CAST(:is_authoritatives AS bool[])), "
                        "  unnest(CAST(:is_auth_superseded AS bool[])), "
                        "  unnest(CAST(:sync_run_ids AS uuid[])), "
                        "  unnest(CAST(:t_valid_froms AS timestamptz[])), "
                        "  NULL::timestamptz, "
                        "  unnest(CAST(:t_ingested_ats AS timestamptz[])), "
                        "  NULL::timestamptz, "
                        "  unnest(CAST(:created_bys AS uuid[]))"
                    ),
                    {
                        "fact_ids": [str(r["fact_id"]) for r in new_facts],
                        "tenant_ids": [str(r["tenant_id"]) for r in new_facts],
                        "entity_ids": [str(r["entity_id"]) for r in new_facts],
                        "categories": [r["category"] for r in new_facts],
                        "bodies": [r["body"] for r in new_facts],
                        "is_authoritatives": [r["is_authoritative"] for r in new_facts],
                        "is_auth_superseded": [r["is_authoritative_superseded"] for r in new_facts],
                        "sync_run_ids": [str(r["sync_run_id"]) for r in new_facts],
                        "t_valid_froms": [r["t_valid_from"].isoformat() for r in new_facts],
                        "t_ingested_ats": [r["t_ingested_at"].isoformat() for r in new_facts],
                        "created_bys": [str(r["created_by"]) if r["created_by"] else None for r in new_facts],
                    },
                )

            # --- 7. Bulk INSERT embedding_outbox -------------------------------
            # Wrapped in a SAVEPOINT: if the table doesn't exist yet (migration
            # hasn't been applied), the SAVEPOINT rolls back without poisoning
            # the outer transaction.
            if new_facts:
                outbox_rows = [
                    {
                        "tid": str(r["tenant_id"]),
                        "fid": str(r["fact_id"]),
                        "body": r["body"],
                        "chunk_plan": json.dumps(make_chunk_plan(r["body"])),
                        "now": now.isoformat(),
                    }
                    for r in new_facts
                ]
                try:
                    async with session.begin_nested():
                        await session.execute(
                            text(
                                "INSERT INTO embedding_outbox "
                                "(outbox_id, tenant_id, claim_type, fact_id, "
                                " text_to_embed, chunk_plan, enqueued_at, attempts) "
                                "SELECT gen_random_uuid(), "
                                "  unnest(CAST(:tids AS uuid[])), "
                                "  'fact', "
                                "  unnest(CAST(:fids AS uuid[])), "
                                "  unnest(CAST(:bodies AS text[])), "
                                "  unnest(CAST(:chunk_plans AS text[]))::jsonb, "
                                "  unnest(CAST(:nows AS timestamptz[])), "
                                "  0"
                            ),
                            {
                                "tids": [r["tid"] for r in outbox_rows],
                                "fids": [r["fid"] for r in outbox_rows],
                                "bodies": [r["body"] for r in outbox_rows],
                                "chunk_plans": [r["chunk_plan"] for r in outbox_rows],
                                "nows": [r["now"] for r in outbox_rows],
                            },
                        )
                except sqlalchemy.exc.ProgrammingError:
                    _log.debug("embedding_outbox not present yet (migration creates it); skipping bulk enqueue")

        _log.info(
            "bulk sync facts written count=%d created=%d superseded=%d sync_run=%s",
            len(new_facts),
            created,
            superseded,
            sync_run_id,
        )
        return SyncWriteResult(created=created, skipped=0, superseded=superseded)

    # ---- capability aggregate read ----------------------------------------

    async def get_full_capability(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        as_of: Any = None,
    ) -> CapabilityRecord:
        async with self._session_factory() as session:
            entity = await session.get(Entity, entity_id)
            if entity is None:
                msg = f"entity {entity_id} not found"
                raise NotFoundError(msg)
            self._entity._assert_tenant(ctx, entity.tenant_id)

            if as_of is not None:
                # Bi-temporal time-travel via shared helpers — all three predicates
                # (t_valid_from, t_valid_to, t_invalidated_at) are enforced.
                # Edges omit the t_valid_to bound (include_valid_to=False) because
                # edge validity is tracked solely by retraction timestamp for these
                # queries; the helper still emits t_valid_from and t_invalidated_at.
                attr_rows = await session.execute(
                    select(Attribute).where(
                        Attribute.tenant_id == ctx.tenant_id,
                        Attribute.entity_id == entity_id,
                        *build_as_of_filter_sql(Attribute, as_of),
                    )
                )
                fact_rows = await session.execute(
                    select(Fact).where(
                        Fact.tenant_id == ctx.tenant_id,
                        Fact.entity_id == entity_id,
                        *build_as_of_filter_sql(Fact, as_of),
                        Fact.is_authoritative_superseded.is_(False),
                    )
                )
                edges_out_rows = await session.execute(
                    select(Edge).where(
                        Edge.tenant_id == ctx.tenant_id,
                        Edge.src_entity_id == entity_id,
                        *build_as_of_filter_sql(Edge, as_of, include_valid_to=False),
                    )
                )
                edges_in_rows = await session.execute(
                    select(Edge).where(
                        Edge.tenant_id == ctx.tenant_id,
                        Edge.dst_entity_id == entity_id,
                        *build_as_of_filter_sql(Edge, as_of, include_valid_to=False),
                    )
                )
            else:
                # Current truth: open-interval rows only.
                # Edges are filtered by retraction only (no t_valid_to bound).
                attr_rows = await session.execute(
                    select(Attribute).where(
                        Attribute.tenant_id == ctx.tenant_id,
                        Attribute.entity_id == entity_id,
                        *build_current_filter_sql(Attribute),
                    )
                )
                fact_rows = await session.execute(
                    select(Fact).where(
                        Fact.tenant_id == ctx.tenant_id,
                        Fact.entity_id == entity_id,
                        *build_current_filter_sql(Fact),
                        Fact.is_authoritative_superseded.is_(False),
                    )
                )
                edges_out_rows = await session.execute(
                    select(Edge).where(
                        Edge.tenant_id == ctx.tenant_id,
                        Edge.src_entity_id == entity_id,
                        *build_current_filter_sql(Edge, include_valid_to=False),
                    )
                )
                edges_in_rows = await session.execute(
                    select(Edge).where(
                        Edge.tenant_id == ctx.tenant_id,
                        Edge.dst_entity_id == entity_id,
                        *build_current_filter_sql(Edge, include_valid_to=False),
                    )
                )

            attributes: dict[str, Any] = {row.key: row.value for row in attr_rows.scalars()}
            facts = [_fact_to_ref(f) for f in fact_rows.scalars()]
            edges_out = [_edge_to_ref(e) for e in edges_out_rows.scalars()]
            edges_in = [_edge_to_ref(e) for e in edges_in_rows.scalars()]

        lifecycle = str(attributes.get("lifecycle", {}).get("state", "unknown"))
        return CapabilityRecord(
            entity=_entity_to_ref(entity),
            attributes=attributes,
            lifecycle=lifecycle,
            facts=facts,
            edges_out=edges_out,
            edges_in=edges_in,
        )

    # ---- internals --------------------------------------------------------

    async def _enqueue_embedding(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        fact_id: uuid.UUID,
        body: str,
    ) -> None:
        """Insert into `embedding_outbox` in the same transaction as the parent fact.

        Wrapped in a SAVEPOINT so that if the table does not yet exist (before
        the migration that creates it has been applied), this is a silent no-op
        that doesn't poison the outer transaction. Once the migration is applied
        the insert succeeds and is committed atomically with the fact row.

        `chunk_plan` is materialized here so the drain job can use it directly
        without re-parsing.
        """
        import json  # noqa: PLC0415

        chunk_plan = make_chunk_plan(body)
        chunk_plan_json = json.dumps(chunk_plan)
        now = self._clock.now()
        try:
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO embedding_outbox "
                        "(outbox_id, tenant_id, claim_type, fact_id, "
                        " text_to_embed, chunk_plan, enqueued_at, attempts) "
                        "VALUES (gen_random_uuid(), :tid, 'fact', :fid, "
                        "        :body, CAST(:chunk_plan AS jsonb), :now, 0)"
                    ),
                    {
                        "tid": ctx.tenant_id,
                        "fid": fact_id,
                        "body": body,
                        "chunk_plan": chunk_plan_json,
                        "now": now,
                    },
                )
        except sqlalchemy.exc.ProgrammingError:
            # The embedding_outbox table does not yet exist — the migration that
            # creates it has not been applied. The SAVEPOINT rolls back cleanly
            # so the outer transaction is unaffected. Any other failure
            # (serialization error, constraint violation, etc.) propagates.
            _log.debug("embedding_outbox not present yet (migration creates it); skipping enqueue")


__all__ = ["FactService", "_fact_to_ref", "_edge_to_ref"]

"""Bi-temporal filter builders. No I/O, no clock reads.

Two tiers live here:

1. **Predicate-DSL tier** — pure Python dicts keyed by column name.
   Service callers that hand-build SQL strings use these.  No external deps.

   Dict value shapes:
   * ``None`` — ``IS NULL``
   * ``"NULL_OR_FUTURE"`` — ``IS NULL OR > caller_clock_now()`` (used by
     ``build_current_filter``; the caller supplies ``now``).
   * ``("LE", dt)`` — ``<= dt``
   * ``("NULL_OR_GT", dt)`` — ``IS NULL OR > dt``

2. **SQLAlchemy tier** — ``build_as_of_filter_sql`` / ``build_current_filter_sql``
   emit SQLAlchemy clause lists ready for ``select(...).where(*clauses)``.
   These import SQLAlchemy but are only called from the service layer,
   which already depends on it.

The non-obvious invariant: a row retracted on day 10 was still trusted at
day 2, so ``build_as_of_filter`` emits ``t_invalidated_at: ("NULL_OR_GT", as_of)``.
Omitting or simplifying this predicate silently leaks retracted rows into
time-travel queries.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

NULL = None
NULL_OR_FUTURE = "NULL_OR_FUTURE"


def build_current_filter() -> dict[str, Any]:
    """Predicate spec for the current truth of a bi-temporal row."""
    return {
        "t_invalidated_at": NULL,
        "t_valid_to": NULL_OR_FUTURE,
    }


def build_as_of_filter(as_of: datetime.datetime) -> dict[str, Any]:
    """Predicate spec for time-travel reads against a bi-temporal row.

    Includes the non-obvious ``t_invalidated_at: NULL OR > as_of`` clause: a
    row retracted at time T_r was still considered trusted at any time
    ``as_of < T_r``. Removing this predicate silently leaks retracted rows
    into time-travel queries.
    """
    return {
        "t_valid_from": ("LE", as_of),
        "t_valid_to": ("NULL_OR_GT", as_of),
        "t_invalidated_at": ("NULL_OR_GT", as_of),
    }


def close_row(now: datetime.datetime | None = None) -> dict[str, Any]:
    """Mutation spec that closes the open interval of a bi-temporal row.

    ``now`` defaults to None so the caller can stamp it from ``clock.now()``
    when the actual timestamp is needed; passing a value here is the
    typical service-side path.
    """
    return {"t_valid_to": now}


def invalidate_row(now: datetime.datetime | None = None) -> dict[str, Any]:
    """Mutation spec that retracts a row (soft-delete + close interval)."""
    return {"t_valid_to": now, "t_invalidated_at": now}


def normalize_utc(dt: datetime.datetime) -> datetime.datetime:
    """Convert any timezone-aware datetime to UTC; raise on naive input.

    Naive datetimes must surface as bugs at write time, not as silently
    wrong comparisons at read time.
    """
    if dt.tzinfo is None:
        msg = f"naive datetime passed to normalize_utc: {dt!r}. " "All timestamps must be UTC-aware."
        raise ValueError(msg)
    return dt.astimezone(datetime.UTC)


# ---------------------------------------------------------------------------
# SQLAlchemy clause helpers
# ---------------------------------------------------------------------------


def build_as_of_filter_sql(
    model: Any,
    as_of: datetime.datetime,
    *,
    include_valid_to: bool = True,
) -> list[ColumnElement[bool]]:
    """Return a list of SQLAlchemy WHERE clauses for a bi-temporal as-of read.

    Pass the list directly to ``select(...).where(*clauses)`` or spread it
    inside an existing ``.where()`` call alongside tenant/key filters.

    The three-predicate form (default) enforces:
    - ``t_valid_from <= as_of``       — row had started
    - ``t_valid_to IS NULL OR > as_of`` — row had not expired
    - ``t_invalidated_at IS NULL OR > as_of`` — row had not been retracted

    Set ``include_valid_to=False`` for models where the ``t_valid_to``
    column is not part of the bi-temporal contract for a given query
    (e.g. edge tables filtered only by retraction).

    Removing the ``t_invalidated_at`` predicate silently leaks retracted rows
    into time-travel results — this function always emits it.
    """
    from sqlalchemy import or_  # noqa: PLC0415

    clauses: list[ColumnElement[bool]] = [
        model.t_valid_from <= as_of,
        or_(model.t_invalidated_at.is_(None), model.t_invalidated_at > as_of),
    ]
    if include_valid_to:
        clauses.insert(1, or_(model.t_valid_to.is_(None), model.t_valid_to > as_of))
    return clauses


def build_current_filter_sql(
    model: Any,
    *,
    include_valid_to: bool = True,
) -> list[ColumnElement[bool]]:
    """Return SQLAlchemy WHERE clauses for a current-truth bi-temporal read.

    The two-predicate form (default) enforces:
    - ``t_invalidated_at IS NULL`` — row is not retracted
    - ``t_valid_to IS NULL``        — row's interval is still open

    Set ``include_valid_to=False`` for models where only retraction matters
    (e.g. edge tables that track open-interval via ``t_invalidated_at`` alone).
    """
    clauses: list[ColumnElement[bool]] = [
        model.t_invalidated_at.is_(None),
    ]
    if include_valid_to:
        clauses.append(model.t_valid_to.is_(None))
    return clauses


__all__ = [
    "NULL",
    "NULL_OR_FUTURE",
    "build_current_filter",
    "build_as_of_filter",
    "build_as_of_filter_sql",
    "build_current_filter_sql",
    "close_row",
    "invalidate_row",
    "normalize_utc",
]

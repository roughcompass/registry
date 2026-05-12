"""Unit tests for registry.service.temporal — bi-temporal predicate helpers."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock  # used by _make_model for the model wrapper

import pytest

from registry.service.temporal import (
    NULL,
    NULL_OR_FUTURE,
    build_as_of_filter,
    build_as_of_filter_sql,
    build_current_filter,
    build_current_filter_sql,
    close_row,
    invalidate_row,
    normalize_utc,
)


def test_build_current_filter_shape() -> None:
    f = build_current_filter()
    assert f == {"t_invalidated_at": NULL, "t_valid_to": NULL_OR_FUTURE}


def test_build_as_of_filter_has_three_predicates() -> None:
    as_of = datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC)
    f = build_as_of_filter(as_of)
    assert f["t_valid_from"] == ("LE", as_of)
    assert f["t_valid_to"] == ("NULL_OR_GT", as_of)
    # Non-obvious predicate — must be present to exclude retracted rows.
    assert f["t_invalidated_at"] == ("NULL_OR_GT", as_of)


def test_build_as_of_filter_includes_non_obvious_predicate() -> None:
    """Regression guard: removing t_invalidated_at from the predicate set leaks retracted rows."""
    as_of = datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC)
    f = build_as_of_filter(as_of)
    assert "t_invalidated_at" in f, (
        "build_as_of_filter must include t_invalidated_at predicate — "
        "without it a row retracted after the as_of point leaks into results"
    )


def test_close_row_sets_t_valid_to() -> None:
    now = datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC)
    assert close_row(now) == {"t_valid_to": now}


def test_close_row_default_is_none() -> None:
    assert close_row() == {"t_valid_to": None}


def test_invalidate_row_sets_both_columns() -> None:
    now = datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC)
    spec = invalidate_row(now)
    assert spec == {"t_valid_to": now, "t_invalidated_at": now}


def test_normalize_utc_raises_on_naive() -> None:
    naive = datetime.datetime(2026, 5, 6)
    with pytest.raises(ValueError, match="naive"):
        normalize_utc(naive)


def test_normalize_utc_returns_utc_on_aware() -> None:
    aware = datetime.datetime(2026, 5, 6, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
    out = normalize_utc(aware)
    assert out.tzinfo == datetime.UTC
    assert out == datetime.datetime(2026, 5, 6, 10, 0, tzinfo=datetime.UTC)


def test_normalize_utc_passthrough_for_utc() -> None:
    aware = datetime.datetime(2026, 5, 6, 12, 0, tzinfo=datetime.UTC)
    out = normalize_utc(aware)
    assert out == aware


# ---------------------------------------------------------------------------
# SQLAlchemy clause helpers
# ---------------------------------------------------------------------------


def _make_model() -> MagicMock:
    """Minimal model stub with real SQLAlchemy Column objects.

    Using standalone ``sqlalchemy.Column`` instances (not attached to a table)
    produces genuine clause elements that ``or_()`` and other SQLAlchemy
    coercions accept.  The columns are named to match the bi-temporal schema.
    """
    from sqlalchemy import Column, DateTime  # noqa: PLC0415

    model = MagicMock()
    model.t_valid_from = Column("t_valid_from", DateTime(timezone=True))
    model.t_valid_to = Column("t_valid_to", DateTime(timezone=True))
    model.t_invalidated_at = Column("t_invalidated_at", DateTime(timezone=True))
    return model


class TestBuildAsOfFilterSql:
    """build_as_of_filter_sql emits the right number of clauses."""

    def test_default_emits_three_clauses(self) -> None:
        as_of = datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC)
        model = _make_model()
        clauses = build_as_of_filter_sql(model, as_of)
        # t_valid_from <= as_of, t_valid_to IS NULL OR > as_of, t_invalidated_at IS NULL OR > as_of
        assert len(clauses) == 3

    def test_exclude_valid_to_emits_two_clauses(self) -> None:
        """Edge tables that skip t_valid_to still get t_valid_from and t_invalidated_at."""
        as_of = datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC)
        model = _make_model()
        clauses = build_as_of_filter_sql(model, as_of, include_valid_to=False)
        assert len(clauses) == 2

    def test_clauses_are_sqlalchemy_expressions(self) -> None:
        """Clauses must be real SQLAlchemy expressions (not raw Python objects)."""
        from sqlalchemy.sql.elements import ClauseElement  # noqa: PLC0415

        as_of = datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC)
        model = _make_model()
        for clause in build_as_of_filter_sql(model, as_of):
            assert isinstance(clause, ClauseElement), f"not a ClauseElement: {clause!r}"

    def test_t_valid_from_bound_in_clause(self) -> None:
        """The first clause must reference t_valid_from (the row-started predicate)."""
        as_of = datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC)
        model = _make_model()
        clauses = build_as_of_filter_sql(model, as_of, include_valid_to=False)
        first_clause_sql = str(clauses[0])
        assert "t_valid_from" in first_clause_sql

    def test_t_invalidated_at_in_clause_text(self) -> None:
        """t_invalidated_at must appear in the compiled clause set — removing it leaks retracted rows."""
        as_of = datetime.datetime(2026, 5, 6, tzinfo=datetime.UTC)
        model = _make_model()
        clause_text = " ".join(str(c) for c in build_as_of_filter_sql(model, as_of))
        assert "t_invalidated_at" in clause_text


class TestBuildCurrentFilterSql:
    """build_current_filter_sql emits the right number of clauses."""

    def test_default_emits_two_clauses(self) -> None:
        model = _make_model()
        clauses = build_current_filter_sql(model)
        # t_invalidated_at IS NULL, t_valid_to IS NULL
        assert len(clauses) == 2

    def test_exclude_valid_to_emits_one_clause(self) -> None:
        """Edge tables filtered only by retraction get one clause."""
        model = _make_model()
        clauses = build_current_filter_sql(model, include_valid_to=False)
        assert len(clauses) == 1

    def test_clauses_are_sqlalchemy_expressions(self) -> None:
        from sqlalchemy.sql.elements import ClauseElement  # noqa: PLC0415

        model = _make_model()
        for clause in build_current_filter_sql(model):
            assert isinstance(clause, ClauseElement)

    def test_t_invalidated_at_in_clause_text(self) -> None:
        model = _make_model()
        clause_text = " ".join(str(c) for c in build_current_filter_sql(model, include_valid_to=False))
        assert "t_invalidated_at" in clause_text

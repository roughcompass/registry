"""Unit tests for the AnnotationRecord SQLAlchemy mapped class.

Covers column presence and nullability, bi-temporal column naming, and the
_serialize_body() ENC-phase handoff seam.  No database connection required.
"""

from __future__ import annotations

import datetime
import uuid

from registry.storage.models import AnnotationRecord

# ---------------------------------------------------------------------------
# Column inventory
# ---------------------------------------------------------------------------

_EXPECTED_COLUMNS = {
    "annotation_id",
    "tenant_id",
    "capability_id",
    "author_actor_id",
    "author_tenant_id",
    "body",
    "triage_note",
    "category",
    "status",
    "version_target",
    "created_at",
    "updated_at",
    "t_valid_from",
    "t_valid_to",
    "t_ingested_at",
    "t_invalidated_at",
}

_BITEMPORAL_COLUMNS = {
    "t_valid_from",
    "t_valid_to",
    "t_ingested_at",
    "t_invalidated_at",
}


def test_required_column_set_present() -> None:
    """All 16 expected columns must be present on the mapped table."""
    actual = {c.name for c in AnnotationRecord.__table__.columns}
    missing = _EXPECTED_COLUMNS - actual
    assert not missing, f"Missing columns: {missing}"


def test_no_enc_phase_columns_present() -> None:
    """ENC-phase columns must not be added in this phase."""
    forbidden = {
        "body_ciphertext",
        "body_nonce",
        "triage_note_ciphertext",
        "triage_note_nonce",
        "kek_id",
        "wrapped_dek",
        "encryption_tier",
    }
    actual = {c.name for c in AnnotationRecord.__table__.columns}
    present = forbidden & actual
    assert not present, f"ENC-phase columns must not exist yet: {present}"


# ---------------------------------------------------------------------------
# Nullability
# ---------------------------------------------------------------------------


def test_body_column_is_non_nullable() -> None:
    """body is NOT NULL in the AN phase; annotation submissions always require a body."""
    col = AnnotationRecord.__table__.c["body"]
    assert not col.nullable, "body must be NOT NULL"


def test_triage_note_column_is_nullable() -> None:
    """triage_note is optional; it is filled in only during provider triage."""
    col = AnnotationRecord.__table__.c["triage_note"]
    assert col.nullable, "triage_note must be nullable (optional)"


def test_required_not_null_columns() -> None:
    """Core identity and content columns that must never be NULL."""
    non_nullable = {
        "annotation_id",
        "tenant_id",
        "capability_id",
        "author_actor_id",
        "author_tenant_id",
        "body",
        "category",
        "status",
        "created_at",
        "updated_at",
        "t_valid_from",
        "t_ingested_at",
    }
    cols = {c.name: c for c in AnnotationRecord.__table__.columns}
    violations = [name for name in non_nullable if cols[name].nullable]
    assert not violations, f"These columns must be NOT NULL but are nullable: {violations}"


def test_optional_columns_are_nullable() -> None:
    """Columns that are legitimately optional must be nullable."""
    nullable_names = {"triage_note", "version_target", "t_valid_to", "t_invalidated_at"}
    cols = {c.name: c for c in AnnotationRecord.__table__.columns}
    violations = [name for name in nullable_names if not cols[name].nullable]
    assert not violations, f"These columns should be nullable but are NOT NULL: {violations}"


# ---------------------------------------------------------------------------
# Bi-temporal column naming
# ---------------------------------------------------------------------------


def test_bitemporal_column_names_match_registry_standard() -> None:
    """Bi-temporal columns must use the registry standard naming convention."""
    actual = {c.name for c in AnnotationRecord.__table__.columns}
    missing = _BITEMPORAL_COLUMNS - actual
    assert not missing, f"Missing bi-temporal columns: {missing}"


def test_t_valid_from_is_non_nullable() -> None:
    col = AnnotationRecord.__table__.c["t_valid_from"]
    assert not col.nullable


def test_t_valid_to_is_nullable() -> None:
    col = AnnotationRecord.__table__.c["t_valid_to"]
    assert col.nullable


def test_t_ingested_at_is_non_nullable() -> None:
    col = AnnotationRecord.__table__.c["t_ingested_at"]
    assert not col.nullable


def test_t_invalidated_at_is_nullable() -> None:
    col = AnnotationRecord.__table__.c["t_invalidated_at"]
    assert col.nullable


# ---------------------------------------------------------------------------
# _serialize_body() — ENC-phase handoff seam
# ---------------------------------------------------------------------------


def _make_record(body: str) -> AnnotationRecord:
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    r = AnnotationRecord()
    r.annotation_id = uuid.uuid4()
    r.tenant_id = uuid.uuid4()
    r.capability_id = uuid.uuid4()
    r.author_actor_id = uuid.uuid4()
    r.author_tenant_id = uuid.uuid4()
    r.body = body
    r.triage_note = None
    r.category = "feedback"
    r.status = "open"
    r.version_target = None
    r.created_at = now
    r.updated_at = now
    r.t_valid_from = now
    r.t_valid_to = None
    r.t_ingested_at = now
    r.t_invalidated_at = None
    return r


def test_serialize_body_returns_body_string() -> None:
    """_serialize_body() must return the body unchanged in the AN phase."""
    record = _make_record("This is the annotation body.")
    assert record._serialize_body() == "This is the annotation body."


def test_serialize_body_returns_exact_body_value() -> None:
    """_serialize_body() must return the exact body without transformation."""
    body = 'Multi-line\nbody\nwith special chars: <>&"'
    record = _make_record(body)
    assert record._serialize_body() == body


def test_serialize_body_returns_string_type() -> None:
    """_serialize_body() must always return a str in the AN phase."""
    record = _make_record("hello")
    result = record._serialize_body()
    assert isinstance(result, str)


def test_serialize_body_not_same_object_as_literal_reference() -> None:
    """_serialize_body() returns the stored body value (identity check)."""
    body = "abc"
    record = _make_record(body)
    # The returned value must equal the body that was set.
    assert record._serialize_body() == record.body


# ---------------------------------------------------------------------------
# Table name
# ---------------------------------------------------------------------------


def test_tablename() -> None:
    assert AnnotationRecord.__tablename__ == "capability_annotations"


# ---------------------------------------------------------------------------
# Primary key
# ---------------------------------------------------------------------------


def test_annotation_id_is_primary_key() -> None:
    pk_cols = [c.name for c in AnnotationRecord.__table__.primary_key.columns]
    assert pk_cols == ["annotation_id"]

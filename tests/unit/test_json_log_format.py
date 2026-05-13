"""Unit tests for the JSON log envelope shape and trace injection.

Covers: field presence, ISO 8601 timestamp format, lowercase level, logger
name propagation, trace_id/span_id injection inside an OTel span, absence of
trace fields outside a span, exception field rendering, idempotency of
configure_logging, and the stdlib bridge for loggers created at any point.

All tests are pure Python — no DB, no ASGI stack, no testcontainers.
Output is captured via capsys (sys.stdout) because caplog captures records
before they reach structlog's ProcessorFormatter; capsys captures the
fully-formatted JSON string that the handler emits.
"""

from __future__ import annotations

import json
import logging

import pytest
from opentelemetry.sdk.trace import TracerProvider

from registry.config import Settings
from registry.logging_config import configure_logging

# Minimal Settings that avoids the __post_init__ auth_mode guard.
_JSON_SETTINGS = Settings(
    database_url="postgresql://x/y",
    pgbouncer_url="postgresql://x/y",
    scheduler_jobstore_url="postgresql://x/y",
    log_format="json",
    log_level=logging.INFO,
)

_TEXT_SETTINGS = Settings(
    database_url="postgresql://x/y",
    pgbouncer_url="postgresql://x/y",
    scheduler_jobstore_url="postgresql://x/y",
    log_format="text",
    log_level=logging.INFO,
)


@pytest.fixture(autouse=True)
def _restore_root_handlers():
    """Save and restore root logger handlers around every test.

    configure_logging calls root_logger.handlers.clear() on entry.
    Without this fixture, one test's cleanup can silently disarm the next
    test's capsys capture (the handler pointing at sys.stdout is the
    capture hook pytest uses).
    """
    original = logging.root.handlers[:]
    original_level = logging.root.level
    yield
    logging.root.handlers[:] = original
    logging.root.setLevel(original_level)


def _first_json_line(captured: str) -> dict:
    """Parse the first non-empty line from captured stdout as JSON."""
    for line in captured.splitlines():
        line = line.strip()
        if line:
            return json.loads(line)
    raise AssertionError(f"No non-empty line found in captured output: {captured!r}")


# ---------------------------------------------------------------------------
# Envelope field presence and format
# ---------------------------------------------------------------------------


def test_json_envelope_fields_present(capsys):
    """Every JSON log line carries timestamp, level, logger, and event."""
    configure_logging(_JSON_SETTINGS)
    logging.getLogger("test.envelope").info("hello world")
    captured = capsys.readouterr().out
    record = _first_json_line(captured)
    assert "timestamp" in record, f"missing 'timestamp': {record}"
    assert "level" in record, f"missing 'level': {record}"
    assert "logger" in record, f"missing 'logger': {record}"
    assert "event" in record, f"missing 'event': {record}"


def test_timestamp_is_iso8601_utc(capsys):
    """timestamp value is an ISO 8601 string with a Z (UTC) suffix."""
    import datetime

    configure_logging(_JSON_SETTINGS)
    logging.getLogger("test.timestamp").info("ts test")
    captured = capsys.readouterr().out
    record = _first_json_line(captured)
    ts = record["timestamp"]
    # structlog TimeStamper(fmt="iso", utc=True) emits ISO 8601 with Z suffix.
    assert ts.endswith("Z"), f"timestamp does not end with 'Z': {ts!r}"
    # Must also be parseable as a datetime (remove trailing Z, parse as UTC).
    dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert dt.tzinfo == datetime.UTC, f"timestamp not UTC: {dt}"


def test_level_is_lowercase(capsys):
    """level field is lowercase string 'info', not 'INFO' or integer 20."""
    configure_logging(_JSON_SETTINGS)
    logging.getLogger("test.level").info("level check")
    captured = capsys.readouterr().out
    record = _first_json_line(captured)
    assert record["level"] == "info", f"expected 'info', got {record['level']!r}"


def test_logger_field_is_module_name(capsys):
    """logger field matches the name passed to logging.getLogger(name)."""
    logger_name = "registry.service.some_unique_module"
    configure_logging(_JSON_SETTINGS)
    logging.getLogger(logger_name).info("logger name test")
    captured = capsys.readouterr().out
    record = _first_json_line(captured)
    assert record["logger"] == logger_name, (
        f"expected logger={logger_name!r}, got {record['logger']!r}"
    )


def test_event_carries_message(capsys):
    """event field carries the log message string."""
    configure_logging(_JSON_SETTINGS)
    logging.getLogger("test.event").info("the quick brown fox")
    captured = capsys.readouterr().out
    record = _first_json_line(captured)
    assert record["event"] == "the quick brown fox", (
        f"unexpected event: {record['event']!r}"
    )


# ---------------------------------------------------------------------------
# Trace injection — inside and outside an OTel span
# ---------------------------------------------------------------------------


def test_trace_span_injected_inside_span(capsys):
    """Inside a real OTel span, trace_id and span_id are present and non-zero."""
    # Install a real TracerProvider (not the default Noop) so spans have valid IDs.
    provider = TracerProvider()
    from opentelemetry import trace as otel_trace
    otel_trace.set_tracer_provider(provider)
    tracer = provider.get_tracer(__name__)

    configure_logging(_JSON_SETTINGS)
    with tracer.start_as_current_span("test-span"):
        logging.getLogger("test.trace.inside").info("inside span")

    captured = capsys.readouterr().out
    record = _first_json_line(captured)

    assert "trace_id" in record, f"trace_id absent from JSON: {record}"
    assert "span_id" in record, f"span_id absent from JSON: {record}"
    assert record["trace_id"] != "0" * 32, "trace_id is all-zeros (invalid span)"
    assert record["span_id"] != "0" * 16, "span_id is all-zeros (invalid span)"


def test_trace_span_absent_outside_span(capsys):
    """Outside any span, trace_id and span_id are absent — not null, not zero."""
    configure_logging(_JSON_SETTINGS)
    logging.getLogger("test.trace.outside").info("outside span")
    captured = capsys.readouterr().out
    record = _first_json_line(captured)
    assert "trace_id" not in record, (
        f"trace_id should be absent outside a span, got {record.get('trace_id')!r}"
    )
    assert "span_id" not in record, (
        f"span_id should be absent outside a span, got {record.get('span_id')!r}"
    )


def test_trace_id_format(capsys):
    """When present, trace_id is a 32-character lowercase hex string."""
    provider = TracerProvider()
    from opentelemetry import trace as otel_trace
    otel_trace.set_tracer_provider(provider)
    tracer = provider.get_tracer(__name__)

    configure_logging(_JSON_SETTINGS)
    with tracer.start_as_current_span("trace-id-format"):
        logging.getLogger("test.trace_id.format").info("checking trace_id format")

    captured = capsys.readouterr().out
    record = _first_json_line(captured)

    assert "trace_id" in record
    trace_id = record["trace_id"]
    assert len(trace_id) == 32, f"trace_id length {len(trace_id)}, expected 32"
    assert trace_id == trace_id.lower(), f"trace_id not lowercase: {trace_id!r}"
    int(trace_id, 16)  # raises ValueError if not valid hex


def test_span_id_format(capsys):
    """When present, span_id is a 16-character lowercase hex string."""
    provider = TracerProvider()
    from opentelemetry import trace as otel_trace
    otel_trace.set_tracer_provider(provider)
    tracer = provider.get_tracer(__name__)

    configure_logging(_JSON_SETTINGS)
    with tracer.start_as_current_span("span-id-format"):
        logging.getLogger("test.span_id.format").info("checking span_id format")

    captured = capsys.readouterr().out
    record = _first_json_line(captured)

    assert "span_id" in record
    span_id = record["span_id"]
    assert len(span_id) == 16, f"span_id length {len(span_id)}, expected 16"
    assert span_id == span_id.lower(), f"span_id not lowercase: {span_id!r}"
    int(span_id, 16)  # raises ValueError if not valid hex


# ---------------------------------------------------------------------------
# Exception field
# ---------------------------------------------------------------------------


def test_exception_field_on_log_exception(capsys):
    """logging.exception() produces an 'exception' key with the traceback string.

    The full output must be a single parseable JSON object — no unescaped
    newlines break JSON parsing (structlog's JSONRenderer serializes them as \\n).
    The exception type and message must appear in the traceback string.
    """
    configure_logging(_JSON_SETTINGS)
    log = logging.getLogger("test.exception.present")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("uh oh")

    captured = capsys.readouterr().out
    # The full line must parse as a single JSON object (the critical guarantee).
    record = _first_json_line(captured)
    assert "exception" in record, f"'exception' key missing from JSON: {record}"
    exc_text = record["exception"]
    assert isinstance(exc_text, str), f"exception field is not a string: {exc_text!r}"
    assert "ValueError" in exc_text, f"exception type missing from traceback: {exc_text!r}"
    assert "boom" in exc_text, f"exception message missing from traceback: {exc_text!r}"


def test_exception_field_absent_on_regular_log(capsys):
    """A non-exception log call produces no 'exception' key."""
    configure_logging(_JSON_SETTINGS)
    logging.getLogger("test.exception.absent").info("just a regular message")
    captured = capsys.readouterr().out
    record = _first_json_line(captured)
    assert "exception" not in record, (
        f"'exception' key should be absent for a normal log call: {record}"
    )


# ---------------------------------------------------------------------------
# Format switching: json vs text
# ---------------------------------------------------------------------------


def test_json_format_is_parseable(capsys):
    """Settings(log_format='json') produces output parseable as JSON."""
    configure_logging(_JSON_SETTINGS)
    logging.getLogger("test.format.json").info("json mode")
    captured = capsys.readouterr().out
    # _first_json_line will raise json.JSONDecodeError if not parseable.
    record = _first_json_line(captured)
    assert isinstance(record, dict)


def test_log_format_text_produces_text(capsys):
    """Settings(log_format='text') produces human-readable (non-JSON) output."""
    configure_logging(_TEXT_SETTINGS)
    logging.getLogger("test.format.text").info("text mode message")
    captured = capsys.readouterr().out
    # The text renderer emits colorized or plain-text console lines,
    # which are not valid JSON (they contain unquoted keys and ANSI codes).
    non_empty_lines = [ln.strip() for ln in captured.splitlines() if ln.strip()]
    assert non_empty_lines, "No output produced in text mode"
    for line in non_empty_lines:
        try:
            json.loads(line)
            raise AssertionError(
                f"text-mode output parsed as JSON — expected human-readable: {line!r}"
            )
        except json.JSONDecodeError:
            pass  # expected


# ---------------------------------------------------------------------------
# Positional argument interpolation
# ---------------------------------------------------------------------------


def test_positional_args_rendered_in_event(capsys):
    """%-style positional args are interpolated into the event string.

    The stdlib bridge calls record.getMessage() before structlog sees the
    record, so %s substitution happens transparently.
    """
    configure_logging(_JSON_SETTINGS)
    logging.getLogger("test.positional").info("x=%s y=%s", 1, 2)
    captured = capsys.readouterr().out
    record = _first_json_line(captured)
    assert record["event"] == "x=1 y=2", (
        f"expected 'x=1 y=2', got {record['event']!r}"
    )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_configure_logging_idempotent(capsys):
    """Calling configure_logging twice does not raise and still produces valid JSON.

    The second call clears and re-attaches the handler; the output after the
    second call must still be parseable JSON (not a broken intermediate state).
    """
    configure_logging(_JSON_SETTINGS)
    configure_logging(_JSON_SETTINGS)  # second call — must not raise
    logging.getLogger("test.idempotent").info("after second configure")
    captured = capsys.readouterr().out
    record = _first_json_line(captured)
    assert "event" in record, f"second configure_logging broke output: {record}"
    assert record["event"] == "after second configure"


# ---------------------------------------------------------------------------
# Child logger and stdlib bridge reach
# ---------------------------------------------------------------------------


def test_worker_logger_emits_json(capsys):
    """A logger named after a worker module emits valid JSON through the root handler.

    Root logger configuration reaches child loggers without per-module setup.
    """
    configure_logging(_JSON_SETTINGS)
    logging.getLogger("registry.workers.webhook_delivery").info("fan-out complete")
    captured = capsys.readouterr().out
    record = _first_json_line(captured)
    assert record["logger"] == "registry.workers.webhook_delivery"
    assert record["event"] == "fan-out complete"


def test_stdlib_bridge_via_foreign_logger(capsys):
    """A stdlib logger created *after* configure_logging runs emits valid JSON.

    This confirms the ProcessorFormatter is attached to the root handler and
    the bridge is active for loggers created at any point in the process lifecycle.
    """
    configure_logging(_JSON_SETTINGS)
    # Create the logger AFTER configure_logging — the bridge must still fire.
    late_logger = logging.getLogger("registry.service.created_after_configure")
    late_logger.info("late logger message")
    captured = capsys.readouterr().out
    record = _first_json_line(captured)
    assert "timestamp" in record
    assert "level" in record
    assert record["event"] == "late logger message"

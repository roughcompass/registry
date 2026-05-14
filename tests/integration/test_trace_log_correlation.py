"""Integration test: request-scope trace correlation.

Verifies that a log line emitted inside a FastAPI request handler carries
valid ``trace_id`` and ``span_id`` fields injected by the structlog processor
chain. This cannot be unit-tested because the span is created by
``FastAPIInstrumentor`` only when a real HTTP request flows through the ASGI
stack.
"""

from __future__ import annotations

import json
import logging
import re

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from registry.config import Settings

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")

_log_probe = logging.getLogger("test.trace_correlation")


async def test_request_log_carries_trace_id(capsys: pytest.CaptureFixture[str]) -> None:
    """Log lines emitted inside a request handler carry the active span's trace_id.

    Setup installs an InMemorySpanExporter-backed TracerProvider before
    create_app() is called, so FastAPIInstrumentor picks up the real SDK
    provider and creates genuine spans. A NoopTracerProvider would produce no
    spans and make the trace_id assertion vacuously pass.

    The healthz route is patched to emit one log line. Without that patch the
    handler returns silently and no log line is captured in scope.
    """
    # Install the real SDK provider before create_app() so that
    # FastAPIInstrumentor.instrument_app() picks it up at instrument time.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    settings = Settings(
        database_url="postgresql+asyncpg://user:pass@localhost:9999/db",
        pgbouncer_url="postgresql+asyncpg://user:pass@localhost:9999/db",
        scheduler_jobstore_url="postgresql+asyncpg://user:pass@localhost:9999/db",
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
        # otlp_endpoint=None means _init_otel is a no-op — the SDK provider
        # installed above is not overwritten by create_app().
        otlp_endpoint=None,
        log_format="json",
    )

    # Import here so configure_logging() runs inside this test's capsys scope.
    # pytest's capsys replaces sys.stdout before each test runs, so
    # configure_logging()'s StreamHandler(sys.stdout) points at the capture
    # buffer. readouterr() below returns everything written through that handler.
    from registry.main import create_app  # noqa: PLC0415

    app = create_app(settings)

    # Patch the /healthz route to emit one log line during the request.
    # Without this the handler returns {"status": "ok"} silently and no log
    # line is captured in scope of the span. The patch targets
    # route.dependant.call, which is what FastAPI's dependency resolver
    # invokes at request time.
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == "/healthz":

            async def _healthz_with_probe() -> dict[str, str]:
                _log_probe.info("healthz_probe_in_span")
                return {"status": "ok"}

            route.endpoint = _healthz_with_probe
            route.dependant.call = _healthz_with_probe
            break

    # Send the request. FastAPIInstrumentor creates a real span that wraps
    # the entire request handler. The probe log line is emitted inside that
    # span, so _add_otel_context injects trace_id/span_id.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200

    # Collect all JSON lines that contain a trace_id key. Non-JSON lines
    # (e.g. pre-structlog startup noise) are skipped silently.
    captured = capsys.readouterr().out
    log_lines_with_trace: list[dict] = []
    for raw_line in captured.splitlines():
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if "trace_id" in obj:
            log_lines_with_trace.append(obj)

    # Assertion 1: at least one log line was emitted while a span was active.
    # A missing key here means the span was not active when the log fired —
    # check that FastAPIInstrumentor.instrument_app() ran after the SDK
    # provider was installed and that the handler patch is in effect.
    assert log_lines_with_trace, (
        "No JSON log line contained 'trace_id'. "
        "The span was not active during logging — verify FastAPIInstrumentor "
        "is wired and the healthz route patch fired."
    )

    line = log_lines_with_trace[0]

    # Assertion 2: trace_id is a 32-character lowercase hex string.
    trace_id = line["trace_id"]
    assert _TRACE_ID_RE.match(trace_id), f"trace_id {trace_id!r} is not a 32-character lowercase hex string"

    # Assertion 3: span_id is a 16-character lowercase hex string.
    span_id = line["span_id"]
    assert _SPAN_ID_RE.match(span_id), f"span_id {span_id!r} is not a 16-character lowercase hex string"

    # Assertion 4: neither value is the OTel invalid-context sentinel (all zeros).
    # A zero trace_id means get_current_span() returned INVALID_SPAN_CONTEXT,
    # which would indicate the span was not propagated into the log processor.
    assert trace_id != "0" * 32, "trace_id is the OTel invalid-sentinel (all zeros)"
    assert span_id != "0" * 16, "span_id is the OTel invalid-sentinel (all zeros)"

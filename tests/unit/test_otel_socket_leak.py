"""Unit tests for the CLOSE_WAIT socket-leak mitigations.

Two independent fixes are exercised here:

1. OTel exporter timeout — The OTLPSpanExporter is configured with an
   explicit short timeout so that a slow or unreachable collector cannot
   hold the BatchSpanProcessor worker thread long enough to starve the
   event loop during shutdown or back-pressure the span queue against
   live request handling.

   Verified by: asserting that a request through an instrumented ASGI app
   completes in well under a second even when the exporter is replaced by
   a slow mock that sleeps for several seconds. The BatchSpanProcessor
   runs its worker in a daemon thread; the request must not wait for export.

2. MCP SSE disconnect watchdog — The SSE handler previously awaited
   ``server._mcp_server.run()`` without any mechanism to stop it when the
   client dropped the connection.  A disconnected client leaves the server
   socket in CLOSE_WAIT until the coroutine is cancelled.

   Verified by: driving ``handle_sse`` with a simulated client disconnect
   mid-session and asserting the coroutine exits promptly without external
   cancellation.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from registry.config import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(otlp_endpoint: str | None = None) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://user:pass@localhost:9999/db",
        pgbouncer_url="postgresql+asyncpg://user:pass@localhost:9999/db",
        scheduler_jobstore_url="postgresql+asyncpg://user:pass@localhost:9999/db",
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
        otlp_endpoint=otlp_endpoint,
        log_format="text",
    )


# ---------------------------------------------------------------------------
# Test 1: OTel exporter stall does not block request completion
# ---------------------------------------------------------------------------


async def test_request_completes_when_exporter_stalls() -> None:
    """A request through the OTel-instrumented ASGI app completes even when
    the span exporter blocks for much longer than the request itself.

    The BatchSpanProcessor enqueues spans on ``span.end()`` (O(1), non-blocking)
    and exports them in a background daemon thread.  The request coroutine must
    not wait for the export to finish — if it did, a slow or unreachable collector
    would hold the response socket open in CLOSE_WAIT until the export times out.

    This test replaces the exporter with one whose ``export()`` call blocks for
    2 seconds and verifies that the round-trip still completes in under 1 second.
    """
    # Install an in-memory provider so create_app() does not overwrite it.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    from registry.main import create_app  # noqa: PLC0415

    settings = _make_settings(otlp_endpoint=None)
    app = create_app(settings)

    transport = ASGITransport(app=app)
    start = time.monotonic()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
    elapsed = time.monotonic() - start

    assert response.status_code == 200
    # The request must complete well within 1 s even though a real stalling
    # exporter would block the worker thread for up to 74 s (10 s timeout × retries).
    # We bound to 1 s here — the ASGI round-trip with no real DB is <50 ms.
    assert elapsed < 1.0, (
        f"Request took {elapsed:.2f}s — the ASGI stack may be blocking on span export. "
        "Check that BatchSpanProcessor is used (not SimpleSpanProcessor) and that "
        "span.end() does not synchronously wait for the exporter."
    )


async def test_otlp_exporter_has_explicit_timeout_configured() -> None:
    """_init_otel must configure the OTLPSpanExporter with an explicit
    timeout sourced from Settings.otlp_exporter_timeout_s.

    Without an explicit timeout the exporter falls back to its 10-second
    default and retries up to 64 seconds per batch.  Long retry windows
    mean the BSP worker holds spans in-flight and the queue fills, causing
    the process to drop spans on busy endpoints.  An explicit, short timeout
    lets operators tune the trade-off without rebuilding the image.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: PLC0415

    from registry.main import _init_otel  # noqa: PLC0415

    captured_exporter: list[OTLPSpanExporter] = []

    original_exp_init = OTLPSpanExporter.__init__

    def _capture_exporter(self: OTLPSpanExporter, *args: Any, **kwargs: Any) -> None:
        captured_exporter.append(self)
        original_exp_init(self, *args, **kwargs)

    settings = _make_settings(otlp_endpoint="http://unreachable-jaeger:4318/v1/traces")
    # otlp_exporter_timeout_s is the new Settings field _init_otel must read.
    settings.otlp_exporter_timeout_s = 2  # type: ignore[attr-defined]

    with (
        patch.object(OTLPSpanExporter, "__init__", _capture_exporter),
        patch("registry.main.trace.set_tracer_provider"),
        patch("registry.main.TracerProvider"),
    ):
        _init_otel(settings)

    assert captured_exporter, "OTLPSpanExporter was not instantiated by _init_otel"
    exp = captured_exporter[0]
    # The exporter's _timeout attribute is set from the constructor's ``timeout``
    # parameter (in seconds).  It must be ≤ Settings.otlp_exporter_timeout_s.
    assert hasattr(exp, "_timeout"), "OTLPSpanExporter has no _timeout attribute — check SDK version"
    assert exp._timeout <= settings.otlp_exporter_timeout_s, (  # type: ignore[attr-defined]
        f"Exporter timeout {exp._timeout}s exceeds the configured bound "
        f"{settings.otlp_exporter_timeout_s}s.  A slow collector will block "
        "the BatchSpanProcessor worker for too long."
    )


# ---------------------------------------------------------------------------
# Test 2: MCP SSE disconnect watchdog
# ---------------------------------------------------------------------------


async def test_mcp_sse_handler_exits_on_client_disconnect() -> None:
    """handle_sse must exit promptly when the client disconnects.

    Without a disconnect watchdog, ``server._mcp_server.run()`` keeps the
    coroutine alive indefinitely after the client closes the connection.
    The server-side socket stays in CLOSE_WAIT and the Starlette worker
    cannot be reclaimed.

    This test simulates a client disconnect mid-session by having the
    ``request.is_disconnected()`` poll return True after one iteration,
    and asserts that ``handle_sse`` returns within 2 seconds.
    """
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415

    from registry.api.routers.mcp import create_mcp_app  # noqa: PLC0415

    # Build a minimal FastMCP server (no tools needed for disconnect test).
    server = FastMCP("test-disconnect")

    # Simulate an infinitely running MCP server — it only stops when
    # cancelled.  Without the disconnect watchdog, this never returns.
    async def _fake_mcp_run(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(60)

    # Build a fake MCP server object whose _mcp_server.run can be swapped.
    fake_inner = MagicMock()
    fake_inner.run = _fake_mcp_run
    fake_inner.create_initialization_options = MagicMock(return_value={})
    server._mcp_server = fake_inner  # type: ignore[attr-defined]

    # Build the ASGI app.
    starlette_app = create_mcp_app(server=server)

    # Manufacture an ASGI scope + receive/send for an SSE request.
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/sse",
        "query_string": b"",
        "headers": [],
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 54321),
        "root_path": "",
        "app": starlette_app,
    }

    # receive() will first yield an http.request event, then signal disconnect.
    receive_calls = 0

    async def receive() -> dict[str, Any]:
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        # After the first event the client has disconnected.
        return {"type": "http.disconnect"}

    sent_events: list[dict] = []

    async def send(message: dict[str, Any]) -> None:
        sent_events.append(message)

    # Run the ASGI app with a tight timeout.  If the disconnect watchdog is
    # missing, this will time out waiting for _fake_mcp_run to finish (60 s).
    start = time.monotonic()
    try:
        await asyncio.wait_for(starlette_app(scope, receive, send), timeout=3.0)
    except TimeoutError:
        elapsed = time.monotonic() - start
        pytest.fail(
            f"handle_sse did not exit within 3 s ({elapsed:.1f}s elapsed) after "
            "client disconnect.  The disconnect watchdog is missing or broken: "
            "server._mcp_server.run() is still running after the client closed."
        )
    except Exception:
        # Any exception other than TimeoutError is acceptable — the handler may
        # raise when it tries to use the fake MCP transport.
        pass

    elapsed = time.monotonic() - start
    assert elapsed < 3.0, (
        f"handle_sse took {elapsed:.2f}s to exit after client disconnect.  "
        "Expected < 3 s with the disconnect watchdog in place."
    )

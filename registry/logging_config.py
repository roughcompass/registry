"""Logging configuration for the registry service.

Call ``configure_logging(settings)`` exactly once from application startup,
before any request handlers or background jobs run. All subsequent log lines
emitted via ``logging.getLogger(__name__)`` will pass through structlog's
processor chain and emit structured output (JSON or human-readable text,
depending on ``settings.log_format``).

The module is intentionally not imported by anything except ``main.py``.
Structlog's ``configure()`` call mutates global process-wide state; wiring
it from two call sites would create subtle ordering bugs.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

import structlog
from opentelemetry import trace as otel_trace

if TYPE_CHECKING:
    from registry.config import Settings


def _add_otel_context(
    logger: object,
    method: str,
    event_dict: dict,
) -> dict:
    """Inject trace_id and span_id into the event dict when an active OTel span exists.

    Reads the current span from the OTel context at log-emit time. This is
    safe in asyncio: OTel's Python SDK uses contextvars for span context, which
    propagates correctly into asyncio tasks and FastAPI request handlers.

    When no span is active (e.g. startup, background jobs outside a traced
    request), the fields are absent — not null, not zero.
    """
    span = otel_trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Configure stdlib logging + structlog. Call once from create_app().

    Sets up structlog's stdlib bridge so that all existing
    ``logging.getLogger(__name__)`` call sites continue to work and emit
    structured JSON (or human-readable text, depending on ``settings.log_format``).

    The function is idempotent with respect to its own setup: each call
    clears root handlers before re-attaching its own, so multiple calls
    do not double-attach handlers. However, any external structlog
    configuration applied after this call (e.g. test fixtures that install
    custom processors) will be overwritten if this function is called again.
    Test suites should save and restore ``logging.root.handlers`` around any
    test that calls ``configure_logging`` directly.
    """
    shared_processors = [
        structlog.stdlib.add_log_level,  # adds "level" key
        structlog.stdlib.add_logger_name,  # adds "logger" key
        _add_otel_context,  # adds "trace_id"/"span_id" when span active
        structlog.processors.TimeStamper(fmt="iso", utc=True),  # ISO8601+UTC "timestamp"
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.log_format == "json":
        final_renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        final_renderer = structlog.dev.ConsoleRenderer()

    # Build the foreign_pre_chain — processors that run on records arriving
    # from stdlib logging (i.e. all existing _log.info(...) call sites).
    # ExceptionRenderer is only included in JSON mode. In text mode,
    # ConsoleRenderer handles exc_info natively and produces a human-readable
    # multi-line traceback. Running ExceptionRenderer first in text mode would
    # convert exc_info to a pre-stringified "exception" key before ConsoleRenderer
    # runs, interfering with ConsoleRenderer's own traceback rendering.
    foreign_pre_chain_procs = list(shared_processors)
    if settings.log_format == "json":
        foreign_pre_chain_procs.append(structlog.processors.ExceptionRenderer())

    # Step 1: build the ProcessorFormatter — this is the bridge that routes
    # stdlib log records through structlog's processor chain.
    # foreign_pre_chain: processors that run on records arriving from stdlib
    #   logging (i.e. all existing _log.info(...) call sites).
    # processors: [remove_processors_meta, final_renderer] — the terminal chain
    #   after the foreign_pre_chain has enriched the event dict.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=foreign_pre_chain_procs,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            final_renderer,
        ],
    )

    # Step 2: attach the formatter to a StreamHandler and configure the root logger.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Remove any handlers added by earlier basicConfig calls or test fixtures
    # to prevent double-emission on a second configure_logging call.
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(settings.log_level)  # resolved from LOG_LEVEL env var; defaults to INFO

    # Step 3: configure structlog for structlog-native callers (if any are
    # added in future work). This also sets up the stdlib wrapper so
    # structlog.get_logger() calls funnel through the same chain.
    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

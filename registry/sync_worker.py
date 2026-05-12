"""Standalone sync-worker entrypoint.

Run via ``python -m catalog.sync_worker``.

This module starts the APScheduler-based sync loop without launching the
FastAPI HTTP server.  It is the entrypoint used by the Helm sync-worker
Deployment (``helm/templates/deployment-sync.yaml``).

Usage::

    python -m catalog.sync_worker

Environment variables are the same as the API server (DATABASE_URL, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

log = logging.getLogger(__name__)


async def _run() -> None:
    # Import here so startup errors surface with a clear traceback.
    from registry.config import get_settings
    from sync.runner import create_scheduler  # type: ignore[import-untyped]

    settings = get_settings()
    scheduler = await create_scheduler(settings)

    log.info("sync-worker: scheduler started (interval=%ss)", settings.sync_interval_seconds)

    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def _handle_signal() -> None:
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        await stop
    finally:
        log.info("sync-worker: shutting down scheduler")
        scheduler.shutdown(wait=True)
        log.info("sync-worker: stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
